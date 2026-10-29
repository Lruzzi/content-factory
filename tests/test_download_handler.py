"""The download stage: fetch the stretch a job asks for, hand it to the pipeline as segments (an engine service)."""

from __future__ import annotations

import ast
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, sources
from common.media import LocalStore
from services.download import handler as dh
from services.download.proc import DownloadError

ROOT = Path(__file__).resolve().parent.parent
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def video_with_keyframes(path: Path, seconds: int = 30) -> Path:
    subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", str(seconds), "-c:v", "libx264",
            "-g", "25", "-keyint_min", "25", "-sc_threshold", "0", "-c:a", "aac", str(path),
        ],
        check=True, timeout=60,
    )  # fmt: skip
    return path


class Fetch:
    """Stands in for yt-dlp: copies a prepared video into the folder it is given, and remembers what it was asked."""

    def __init__(self, video: Path, error: Exception | None = None):
        self.video, self.error = video, error
        self.calls: list[dict[str, object]] = []

    def __call__(self, video_id, directory, *, start_s, length_s, height):
        self.calls.append({"video_id": video_id, "start_s": start_s, "length_s": length_s, "height": height})
        if self.error:
            raise self.error
        target = Path(directory) / "vod.mp4"
        shutil.copyfile(self.video, target)
        return target


@pytest.fixture
def world(db, tmp_path):
    stream_id = uuid.uuid4()
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO live_streams (id, source_id, video_id, status) VALUES (:i, :s, 'video000001', 'ended')"
            ),
            {"i": stream_id, "s": source_id},
        )
        spec = profile.model_dump(mode="json")
        spec["live"]["segment_min"] = 2  # segments of 120 s: three of them in a 5-minute test video
        spec["live"]["segment_overlap_s"] = 30
        spec["download"]["max_height"] = 1080
        profile = cs.Profile.model_validate(spec)
        job_id = jobs.create_job(
            conn, profile, stage="download", source_id=source_id,
            data={"video_id": "video000001", "stream_id": str(stream_id), "start_s": 600, "length_s": 3600},
        )  # fmt: skip
        job = jobs.get(conn, job_id)
    return LocalStore(tmp_path / "media"), job, stream_id, tmp_path


def rows(db, sql):
    with db.connect() as conn:
        return conn.execute(sa.text(sql)).mappings().all()


@pytest.mark.integration
@needs_ffmpeg
def test_the_job_asks_for_its_own_stretch_at_the_height_its_profile_snapshot_allows(db, world):
    store, job, _, tmp = world
    fetch = Fetch(video_with_keyframes(tmp / "v.mp4"))
    dh.make_handler(db, store, fetch=fetch)(job)
    assert fetch.calls == [{"video_id": "video000001", "start_s": 600, "length_s": 3600, "height": 1080}]


@pytest.mark.integration
@needs_ffmpeg
def test_the_fetched_video_becomes_segments_and_one_understand_job_each_and_the_download_is_done(db, world):
    store, job, stream_id, tmp = world
    result = dh.make_handler(db, store, fetch=Fetch(video_with_keyframes(tmp / "v.mp4", 300)))(job)
    assert result is None  # the job itself is finished: its work went on as new jobs
    segs = rows(db, "SELECT idx, key FROM segments ORDER BY idx")
    assert [s["idx"] for s in segs] == [0, 1, 2] and all(store.exists(s["key"]) for s in segs)
    made = rows(db, "SELECT stage, status, data FROM jobs WHERE stage = 'understand' ORDER BY (data->>'segment')::int")
    assert len(made) == 3 and made[0]["data"]["stream_id"] == str(stream_id) and made[0]["data"]["live"] is True
    assert made[0]["data"]["video_id"] == "video000001"


@pytest.mark.integration
@needs_ffmpeg
def test_the_new_jobs_are_sent_on_to_understand(db, world):
    store, job, _, tmp = world
    sent: list[uuid.UUID] = []
    dh.make_handler(db, store, fetch=Fetch(video_with_keyframes(tmp / "v.mp4", 300)), publish_job=sent.append)(job)
    assert sorted(sent) == sorted(r["id"] for r in rows(db, "SELECT id FROM jobs WHERE stage = 'understand'"))


@pytest.mark.integration
@needs_ffmpeg
def test_running_the_job_again_makes_no_second_set_but_sends_the_waiting_ones_on_again(db, world):
    store, job, _, tmp = world
    fetch = Fetch(video_with_keyframes(tmp / "v.mp4", 300))
    first: list[uuid.UUID] = []
    again: list[uuid.UUID] = []
    dh.make_handler(db, store, fetch=fetch, publish_job=first.append)(job)
    dh.make_handler(db, store, fetch=fetch, publish_job=again.append)(job)
    assert len(rows(db, "SELECT 1 FROM jobs WHERE stage = 'understand'")) == 3
    assert sorted(again) == sorted(first)  # the queue may have lost them: they are offered again, not duplicated


@pytest.mark.integration
def test_a_failed_download_is_an_error_for_the_worker_to_retry_and_stores_nothing(db, world):
    store, job, _, tmp = world
    handler = dh.make_handler(db, store, fetch=Fetch(tmp / "none.mp4", error=DownloadError("yt-dlp failed: HTTP 403")))
    with pytest.raises(DownloadError, match="403"):
        handler(job)
    assert rows(db, "SELECT 1 FROM segments") == [] and rows(db, "SELECT 1 FROM jobs WHERE stage = 'understand'") == []


@pytest.mark.integration
@pytest.mark.parametrize("drop", ["video_id", "stream_id", "start_s", "length_s"])
def test_a_job_missing_what_it_needs_says_which_field(db, world, drop):
    store, job, _, tmp = world
    job["data"].pop(drop)
    with pytest.raises(ValueError, match=drop):
        dh.make_handler(db, store, fetch=Fetch(tmp / "none.mp4"))(job)


@pytest.mark.parametrize("bad", ["../x", "a b", "--help", ""])
def test_a_video_id_that_is_not_one_is_refused_before_anything_runs(bad):
    with pytest.raises(ValueError, match="video"):
        dh.check_video_id(bad)


# ---------- the engine boundary -------------------------------------------------------------------------------
ENGINE = ["download", "understand", "select", "render", "package"]
PERSONAL = ["publisher", "review", "recorder", "live_watcher", "discover", "scout", "hermes", "replay", "scaler"]


@pytest.mark.parametrize("service", ENGINE)
def test_an_engine_service_never_imports_a_personal_only_one(service):
    for path in (ROOT / "services" / service).glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert not any(name.startswith(f"services.{p}") for p in PERSONAL), f"{path.name} imports {name}"
