"""A video file becomes the segments, rows and understand jobs a live capture would have made."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import sources
from common.media import LocalStore
from services.download import ingest
from services.download.proc import DownloadError
from services.render import ffmpeg as ff

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg"),
]


def video_with_keyframes(path: Path, seconds: int = 30, every_s: int = 1) -> Path:
    subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", str(seconds), "-c:v", "libx264",
            "-g", str(25 * every_s), "-keyint_min", str(25 * every_s), "-sc_threshold", "0", "-c:a", "aac", str(path),
        ],
        check=True, timeout=60,
    )  # fmt: skip
    return path


@pytest.fixture
def world(db, tmp_path):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        stream_id = uuid.uuid4()
        conn.execute(
            sa.text("INSERT INTO live_streams (id, source_id, video_id, status) VALUES (:i, :s, 'vid00001', 'ended')"),
            {"i": stream_id, "s": source_id},
        )
    return LocalStore(tmp_path / "media"), profile, source_id, stream_id, tmp_path


def run(db, world, video, **kw):
    store, profile, source_id, stream_id, tmp = world
    return ingest.ingest(
        db, store, video, stream_id=stream_id, video_id="vid00001", profile=profile, source_id=source_id,
        segment_s=10, workdir=tmp / "work", **kw,
    )  # fmt: skip


def rows(db, sql):
    with db.connect() as conn:
        return conn.execute(sa.text(sql)).mappings().all()


def test_a_video_with_regular_keyframes_is_cut_by_copying_the_streams_not_re_encoding_them(db, world):
    video = video_with_keyframes(world[4] / "v.mp4")
    ids = run(db, world, video)
    assert len(ids) == 3
    segs = rows(db, "SELECT idx, key, start_s, end_s FROM segments ORDER BY idx")
    assert [round(float(s["end_s"]) - float(s["start_s"])) for s in segs] == [10, 10, 10]
    assert float(segs[1]["start_s"]) == pytest.approx(float(segs[0]["end_s"]), abs=0.01)
    first = ff.probe(world[0].path(segs[0]["key"]))
    assert (first.video_codec, first.width, first.height) == ("h264", 320, 180)


def test_a_video_whose_keyframes_are_too_far_apart_is_re_encoded_so_the_cuts_still_land(db, world):
    video = video_with_keyframes(world[4] / "v.mp4", every_s=30)  # one keyframe: copying could only make one piece
    assert len(run(db, world, video)) == 3


def test_every_segment_gets_an_understand_job_on_the_streams_own_clock(db, world):
    run(db, world, video_with_keyframes(world[4] / "v.mp4"))
    jobs_ = rows(db, "SELECT stage, status, data FROM jobs ORDER BY (data->>'segment')::int")
    assert [j["stage"] for j in jobs_] == ["understand"] * 3
    assert jobs_[1]["data"]["live"] is True and jobs_[1]["data"]["video_id"] == "vid00001"
    assert jobs_[1]["data"]["stream_id"] == str(world[3]) and jobs_[1]["data"]["start_s"] == pytest.approx(10, abs=0.2)


def test_running_it_twice_makes_no_second_set_of_jobs(db, world):
    video = video_with_keyframes(world[4] / "v.mp4")
    assert len(run(db, world, video)) == 3
    assert run(db, world, video) == []
    assert len(rows(db, "SELECT 1 FROM jobs")) == 3 and len(rows(db, "SELECT 1 FROM segments")) == 3


def test_a_file_that_is_not_a_video_is_a_clear_error_and_stores_nothing(db, world):
    bad = world[4] / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(DownloadError, match="cannot be read"):
        run(db, world, bad)
    assert rows(db, "SELECT 1 FROM segments") == [] and rows(db, "SELECT 1 FROM jobs") == []
    assert not (world[4] / "work").exists()


def test_the_work_folder_is_removed_afterwards(db, world):
    run(db, world, video_with_keyframes(world[4] / "v.mp4"))
    assert not (world[4] / "work").exists()
