"""Feeding a public recording through the pipeline as if it had been captured live: a test tool, so what it makes can
never be posted and can be removed again."""

from __future__ import annotations

import shutil

import pytest
import sqlalchemy as sa
from media_helpers import make_clip

import config_schema as cs
from common import sources
from common.media import LocalStore
from services.replay import replay as rp

pytestmark = pytest.mark.integration


@pytest.fixture
def world(db, tmp_path):
    store = LocalStore(tmp_path / "media")
    video = make_clip(tmp_path / "vod.mp4", seconds=25)
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
    return store, video, tmp_path


def run(db, world, video_id="IPDV6wpGoUM", **kw):
    store, video, tmp = world
    published: list = []
    options = {"video_id": video_id, "like": "leokabuki", "config_dir": cs.ROOT, "top_k": 1, "segment_s": 10}
    result = rp.replay(db, store, video, publish_job=published.append, workdir=tmp / "work", **(options | kw))
    return result, published


def rows(db, sql):
    with db.connect() as conn:
        return conn.execute(sa.text(sql)).mappings().all()


@pytest.mark.parametrize(
    "good",
    [
        "https://www.youtube.com/watch?v=IPDV6wpGoUM",
        "https://youtu.be/IPDV6wpGoUM",
        "https://www.youtube.com/live/IPDV6wpGoUM",
    ],
)
def test_a_youtube_link_gives_its_video_id(good):
    assert rp.video_id_of(good) == "IPDV6wpGoUM"


@pytest.mark.parametrize(
    "bad",
    ["--help", "file:///etc/passwd", "https://evil.example/watch?v=IPDV6wpGoUM", "https://www.youtube.com/@x", ""],
)
def test_anything_that_is_not_a_single_youtube_video_is_refused(bad):
    with pytest.raises(ValueError, match="video"):
        rp.video_id_of(bad)


def test_the_download_command_asks_for_one_range_at_a_capped_quality_as_an_argument_list():
    cmd = rp.download_command(
        "/bin/yt-dlp", "IPDV6wpGoUM", start_s=120, length_s=3000, height=720, out_template="/w/vod.%(ext)s"
    )
    assert (
        cmd[0] == "/bin/yt-dlp" and "--no-playlist" in cmd and cmd[cmd.index("--download-sections") + 1] == "*120-3120"
    )
    assert "height<=720" in cmd[cmd.index("-f") + 1] and cmd[-1] == "https://www.youtube.com/watch?v=IPDV6wpGoUM"


def test_the_recording_is_cut_into_segments_and_each_becomes_a_stored_file_a_row_and_an_understand_job(db, world):
    result, published = run(db, world)
    store, _, _ = world
    segs = rows(db, "SELECT idx, key, start_s, end_s FROM segments ORDER BY idx")
    assert len(segs) == result.segments == 3 and [s["idx"] for s in segs] == [0, 1, 2]
    assert all(store.exists(s["key"]) for s in segs)
    assert float(segs[0]["start_s"]) == 0 and float(segs[1]["start_s"]) == pytest.approx(
        float(segs[0]["end_s"]), abs=0.01
    )
    assert float(segs[2]["end_s"]) == pytest.approx(25.0, abs=1.0)  # the last one is the shorter remainder
    jobs_ = rows(db, "SELECT stage, status, media_key, data FROM jobs ORDER BY (data->>'segment')::int")
    assert [j["stage"] for j in jobs_] == ["understand"] * 3 and len(published) == 3
    assert (
        jobs_[1]["data"]["live"] is True
        and jobs_[1]["data"]["video_id"] == "IPDV6wpGoUM"
        and jobs_[1]["data"]["segment"] == 1
    )
    assert jobs_[1]["data"]["start_s"] == pytest.approx(float(segs[1]["start_s"]), abs=0.01)


def test_the_stream_is_recorded_as_already_ended_so_the_recorder_never_touches_it(db, world):
    run(db, world)
    (stream,) = rows(db, "SELECT status, ended_at, video_id FROM live_streams")
    assert stream["status"] == "ended" and stream["ended_at"] is not None and stream["video_id"] == "IPDV6wpGoUM"


def test_nothing_made_by_a_replay_can_be_posted_and_it_makes_few_previews(db, world):
    run(db, world, top_k=1)
    spec = rows(db, "SELECT spec FROM jobs LIMIT 1")[0]["spec"]
    assert spec["source"]["clip_policy"] == "blocked" and spec["selection"]["top_k"] == 1
    assert cs.Profile.model_validate(spec).upload_accounts() == []


def test_a_replay_can_use_another_layout_than_its_source_and_still_cannot_be_posted(db, world):
    run(db, world, like="pwk", layout="blur_fill")
    spec = rows(db, "SELECT spec FROM jobs LIMIT 1")[0]["spec"]
    assert spec["layout"]["type"] == "blur_fill" and spec["niche"] == "podcast"
    # a replay has no chat, heatmap or loudness to go by: the transcript is read in full and selection follows it
    assert spec["selection"]["mode"] == "transcript" and spec["signals"]["transcript"]["mode"] == "full"
    assert spec["compilation"]["enabled"] is False
    assert cs.Profile.model_validate(spec).upload_accounts() == []


def test_a_replay_can_name_the_real_channel_so_the_credit_is_not_the_copied_sources(db, world):
    run(db, world, like="pwk", name="Raditya Dika")
    spec = rows(db, "SELECT spec FROM jobs LIMIT 1")[0]["spec"]
    assert spec["source"]["name"] == "Raditya Dika" and spec["source"]["clip_policy"] == "blocked"


def test_replaying_the_same_recording_twice_is_refused_and_leaves_nothing_doubled(db, world):
    run(db, world)
    with pytest.raises(rp.ReplayError, match="already"):
        run(db, world)
    assert len(rows(db, "SELECT 1 FROM segments")) == 3 and len(rows(db, "SELECT 1 FROM jobs")) == 3


def test_a_source_that_does_not_exist_is_a_clear_error_and_nothing_is_stored(db, world):
    with pytest.raises(rp.ReplayError, match="no source"):
        run(db, world, like="nobody")
    assert rows(db, "SELECT 1 FROM live_streams") == []


def test_forgetting_a_replay_removes_its_rows_jobs_and_files_and_only_those(db, world, tmp_path):
    store, _, _ = world
    run(db, world, video_id="IPDV6wpGoUM")
    other = make_clip(tmp_path / "other.mp4", seconds=12)
    rp.replay(
        db, store, other, video_id="x_oyvxxF6O0", like="leokabuki", config_dir=cs.ROOT, top_k=1, segment_s=10,
        publish_job=None, workdir=tmp_path / "work2",
    )  # fmt: skip
    clip = store.path("clips/render-of-first.mp4")
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"clip")
    with db.begin() as conn:  # a render job of the first stream, with its output file
        conn.execute(
            sa.text(
                "UPDATE jobs SET data = data || CAST(:d AS jsonb) "
                "WHERE (data->>'segment')::int = 0 AND data->>'video_id' = 'IPDV6wpGoUM'"
            ),
            {"d": '{"render": {"output": {"key": "clips/render-of-first.mp4"}}}'},
        )
    assert rp.forget(db, store, "IPDV6wpGoUM") == 1
    assert [r["video_id"] for r in rows(db, "SELECT video_id FROM live_streams")] == ["x_oyvxxF6O0"]
    assert len(rows(db, "SELECT 1 FROM jobs")) == 2 and not clip.exists()
    assert len(rows(db, "SELECT 1 FROM segments")) == 2
    assert rp.forget(db, store, "IPDV6wpGoUM") == 0  # nothing left to forget


def test_the_workdir_is_cleaned_up_after_a_replay(db, world):
    run(db, world)
    assert not (world[2] / "work").exists() or not any((world[2] / "work").iterdir())


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
