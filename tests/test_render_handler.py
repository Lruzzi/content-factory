"""The render job handler: a job in, a clip in the media store, the job moved to packaging (FR-6, FR-18)."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, pipeline
from common.media import LocalStore
from common.streams import Consumer, publish
from common.worker import Worker
from services.render import ffmpeg as ff
from services.render.handler import make_handler

FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe") and FONT.exists()),
        reason="needs ffmpeg and DejaVu Sans Bold",
    ),
]

RETRY = jobs.Retry(max_attempts=3, backoff_s=(60, 300, 1800))
SOURCE_KEY = "segments/stream1/0001.mp4"
WORDS = [
    {"text": "savage", "start_s": 1.2, "end_s": 1.7},
    {"text": "lord", "start_s": 1.8, "end_s": 2.2},
]


@pytest.fixture(scope="module")
def source_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("handler-media") / "segment.mp4"
    subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=1280x720:rate=30:duration=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True, timeout=120,
    )  # fmt: skip
    return path


@pytest.fixture
def store(tmp_path, source_file) -> LocalStore:
    media = LocalStore(tmp_path / "media")
    media.put_file(SOURCE_KEY, source_file)
    return media


def profile() -> cs.Profile:
    """The MLBB profile with the test font and no logo file (the brand assets do not exist yet)."""
    base = next(p for p in cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml") if p.source.id == "leokabuki")
    spec = base.model_dump(mode="json")
    spec["captions"]["font"] = str(FONT)
    spec["branding"]["logo"]["enabled"] = False
    return cs.Profile.model_validate(spec)


def render_data(**overrides):
    base = {"source_key": SOURCE_KEY, "start_s": 1.0, "end_s": 5.0, "hook": "Watch this", "words": WORDS}
    return {"render": base | overrides}


def make_worker(db, redis_client, prefix, store, alerts=None):
    consumer = Consumer(
        redis_client,
        pipeline.stream_for("render", prefix),
        "workers",
        "w1",
        visibility_timeout_s=30,
        dlq=f"{prefix}.dlq",
    )
    return Worker(
        engine=db,
        r=redis_client,
        stage="render",
        consumer=consumer,
        handler=make_handler(db, store),
        retry=RETRY,
        alert=alerts.append if alerts is not None else None,
        prefix=prefix,
    )


def queue(db, redis_client, prefix, data):
    with db.begin() as conn:
        job_id = jobs.create_job(conn, profile(), stage="render", data=data)
    publish(redis_client, pipeline.stream_for("render", prefix), str(job_id))
    return job_id


def state(db, job_id):
    with db.connect() as conn:
        job = jobs.get(conn, job_id)
    assert job is not None
    return job


def test_a_render_job_becomes_a_clip_and_moves_on_to_packaging(db, redis_client, stream_prefix, store):
    job_id = queue(db, redis_client, stream_prefix, render_data())
    assert make_worker(db, redis_client, stream_prefix, store).run_once()
    job = state(db, job_id)
    assert (job["stage"], job["status"]) == ("package", "queued")
    assert job["media_key"] == f"clips/{job_id}.mp4"
    info = ff.probe(store.path(job["media_key"]))
    assert (info.width, info.height, info.video_codec, info.audio_codec) == (1080, 1920, "h264", "aac")
    assert abs(info.duration_s - 4.0) < 0.2
    assert redis_client.xlen(pipeline.stream_for("package", stream_prefix)) == 1


def test_the_job_records_what_was_rendered_and_keeps_the_input(db, redis_client, stream_prefix, store):
    job_id = queue(db, redis_client, stream_prefix, render_data())
    make_worker(db, redis_client, stream_prefix, store).run_once()
    rendered = state(db, job_id)["data"]["render"]
    assert rendered["source_key"] == SOURCE_KEY
    assert rendered["words"][0]["text"] == "savage"
    out = rendered["output"]
    assert (out["key"], out["width"], out["height"]) == (f"clips/{job_id}.mp4", 1080, 1920)
    assert out["bytes"] == store.path(out["key"]).stat().st_size


def test_rendering_twice_replaces_the_clip_instead_of_adding_another(db, redis_client, stream_prefix, store):
    job_id = queue(db, redis_client, stream_prefix, render_data())
    make_worker(db, redis_client, stream_prefix, store).run_once()
    with db.begin() as conn:  # a reconciler retry puts the job back at render
        conn.execute(sa.text("UPDATE jobs SET stage = 'render', status = 'queued' WHERE id = :i"), {"i": job_id})
    publish(redis_client, pipeline.stream_for("render", stream_prefix), str(job_id))
    make_worker(db, redis_client, stream_prefix, store).run_once()
    assert [p.name for p in (store.root / "clips").iterdir()] == [f"{job_id}.mp4"]


def test_a_job_with_no_render_data_fails_naming_what_is_missing(db, redis_client, stream_prefix, store):
    job_id = queue(db, redis_client, stream_prefix, {})
    make_worker(db, redis_client, stream_prefix, store).run_once()
    job = state(db, job_id)
    assert job["status"] == "failed"
    assert "source_key" in job["last_error"]


def test_a_source_that_is_not_in_the_store_fails_clearly(db, redis_client, stream_prefix, store):
    job_id = queue(db, redis_client, stream_prefix, render_data(source_key="segments/gone/0009.mp4"))
    make_worker(db, redis_client, stream_prefix, store).run_once()
    job = state(db, job_id)
    assert job["status"] == "failed"
    assert "not in the media store" in job["last_error"]


@pytest.mark.parametrize("key", ["../../etc/passwd", "/etc/passwd", "segments/../../x.mp4"])
def test_a_source_key_that_tries_to_leave_the_store_fails_and_reads_nothing(
    db, redis_client, stream_prefix, store, key
):
    job_id = queue(db, redis_client, stream_prefix, render_data(source_key=key))
    make_worker(db, redis_client, stream_prefix, store).run_once()
    job = state(db, job_id)
    assert job["status"] == "failed"
    assert "media key" in job["last_error"]


@pytest.mark.parametrize(("start", "end"), [(5.0, 5.0), (3.0, 1.0), (6.0, 30.0)])
def test_an_impossible_window_fails_before_any_render(db, redis_client, stream_prefix, store, start, end):
    job_id = queue(db, redis_client, stream_prefix, render_data(start_s=start, end_s=end))
    make_worker(db, redis_client, stream_prefix, store).run_once()
    job = state(db, job_id)
    assert job["status"] == "failed"
    assert not (store.root / "clips").exists() or list((store.root / "clips").iterdir()) == []


def test_an_unknown_field_in_the_render_data_is_refused(db, redis_client, stream_prefix, store):
    job_id = queue(db, redis_client, stream_prefix, render_data(surprise=1))
    make_worker(db, redis_client, stream_prefix, store).run_once()
    assert state(db, job_id)["status"] == "failed"


def test_the_third_failure_kills_the_job_and_raises_the_alert(db, redis_client, stream_prefix, store):
    alerts: list[str] = []
    job_id = queue(db, redis_client, stream_prefix, {})
    for _ in range(3):
        make_worker(db, redis_client, stream_prefix, store, alerts).run_once()
        publish(redis_client, pipeline.stream_for("render", stream_prefix), str(job_id))
    make_worker(db, redis_client, stream_prefix, store, alerts).run_once()
    assert state(db, job_id)["status"] == "dead"
    assert len(alerts) == 1
    assert str(job_id) in alerts[0]


def test_a_job_uses_its_own_settings_snapshot_not_the_current_yaml(db, redis_client, stream_prefix, store):
    job_id = queue(db, redis_client, stream_prefix, render_data())
    spec = profile().model_dump(mode="json")
    spec["layout"]["cam"] = {"x": 1900, "y": 0, "w": 400, "h": 300}  # outside the 1280x720 source
    with db.begin() as conn:  # the YAML's own cam box is fine, so only a handler reading the snapshot can fail
        conn.execute(
            sa.text("UPDATE jobs SET spec = CAST(:s AS jsonb) WHERE id = :i"), {"s": json.dumps(spec), "i": job_id}
        )
    make_worker(db, redis_client, stream_prefix, store).run_once()
    job = state(db, job_id)
    assert job["status"] == "failed"
    assert "outside" in job["last_error"]


def test_the_clip_key_is_the_job_id_so_two_jobs_never_collide(db, redis_client, stream_prefix, store):
    a = queue(db, redis_client, stream_prefix, render_data())
    b = queue(db, redis_client, stream_prefix, render_data())
    worker = make_worker(db, redis_client, stream_prefix, store)
    worker.run_once()
    worker.run_once()
    assert {state(db, a)["media_key"], state(db, b)["media_key"]} == {f"clips/{a}.mp4", f"clips/{b}.mp4"}
    assert uuid.UUID(str(a)) != uuid.UUID(str(b))


# ---------- a recap: pieces from several files joined into one video --------------------
def recap_data(**overrides):
    base = {
        "pieces": [
            {"source_key": SOURCE_KEY, "start_s": 1.0, "end_s": 3.0},
            {"source_key": SOURCE_KEY, "start_s": 5.0, "end_s": 7.0},
        ],
        "hook": "Recap",
        "words": [{"text": "satu", "start_s": 0.5, "end_s": 0.9}, {"text": "dua", "start_s": 2.5, "end_s": 2.9}],
    }
    return {"render": base | overrides}


def test_pieces_become_one_clip_as_long_as_the_pieces_together(db, store):
    with db.begin() as conn:
        job_id = jobs.create_job(conn, profile(), stage="render", data=recap_data())
        job = jobs.get(conn, job_id)
    assert job is not None and make_handler(db, store)(job) == "package"
    with db.connect() as conn:
        finished = jobs.get(conn, job_id)
    assert finished is not None
    record = finished["data"]["render"]
    assert record["output"]["duration_s"] == pytest.approx(4.0, abs=0.5)
    assert (record["output"]["width"], record["output"]["height"]) == (1080, 1920) and store.exists(
        record["output"]["key"]
    )


def test_a_piece_from_a_file_that_is_gone_fails_the_job_with_a_clear_message(db, store):
    bad = recap_data(pieces=[{"source_key": "segments/gone.ts", "start_s": 0.0, "end_s": 2.0}])
    with db.begin() as conn:
        job = jobs.get(conn, jobs.create_job(conn, profile(), stage="render", data=bad))
    assert job is not None
    with pytest.raises(ff.RenderError, match="not in the media store"):
        make_handler(db, store)(job)


@pytest.mark.parametrize(
    "data",
    [
        {
            "pieces": [{"source_key": "a", "start_s": 0, "end_s": 2}],
            "source_key": "b",
            "start_s": 0,
            "end_s": 2,
        },  # both forms
        {},  # neither form
        {"pieces": []},
        {"pieces": [{"source_key": "a", "start_s": 5, "end_s": 2}]},
        {"pieces": [{"source_key": "a", "start_s": 0, "end_s": 200}, {"source_key": "a", "start_s": 0, "end_s": 200}]},
    ],
)
def test_the_render_input_takes_one_window_or_a_list_of_pieces_never_both_or_neither_or_too_much(data):
    from services.render.handler import RenderInput

    with pytest.raises(ValueError):
        RenderInput.model_validate(data)
