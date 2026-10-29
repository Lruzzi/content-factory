"""Replay a public recording through the pipeline (a test tool): `python -m services.replay <youtube link> ...`.

  replay <link> [--from-min 0] [--minutes 50] [--like leokabuki] [--segment-min 10] [--top-k 1]
                [--layout blur_fill] [--height 1080] [--name "Channel"]
  replay --forget <link>        remove what a replay stored (rows, jobs, files)

Needs DATABASE_URL and REDIS_URL, and yt-dlp and ffmpeg. What it stores can never be posted (clip_policy: blocked)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

from common import pipeline, streams
from common.db import make_engine
from common.media import LocalStore
from config_schema import ROOT
from services.download.proc import DownloadError
from services.replay import replay as rp

log = logging.getLogger("replay")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="replay", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("link")
    parser.add_argument("--forget", action="store_true")
    parser.add_argument("--from-min", type=int, default=0)
    parser.add_argument("--minutes", type=int, default=50)
    parser.add_argument("--like", default="leokabuki")
    parser.add_argument("--segment-min", type=int, default=10)
    parser.add_argument("--top-k", type=int, default=1)
    parser.add_argument("--layout", choices=["center", "blur_fill"])
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--name", help="the channel's real name, for the credit on the video")
    args = parser.parse_args(argv)
    media_root = Path(os.environ.get("MEDIA_ROOT", "/media"))
    store = LocalStore(media_root)
    scratch = media_root / "replay-tmp"  # the range is large: keep it on the volume, not in memory
    scratch.mkdir(exist_ok=True)
    tempfile.tempdir = str(scratch)
    engine = make_engine()
    try:
        video_id = rp.video_id_of(args.link)
        if args.forget:
            log.info("forgot %d stream(s) of %s", rp.forget(engine, store, video_id), video_id)
            return 0
        client = streams.connect(os.environ["REDIS_URL"])

        def publish(job_id: object) -> None:
            streams.publish(client, pipeline.stream_for("understand"), str(job_id))

        with tempfile.TemporaryDirectory(prefix="replay-dl-") as tmp:
            video = rp.download(
                video_id, Path(tmp), start_s=args.from_min * 60, length_s=args.minutes * 60, height=args.height
            )
            done = rp.replay(
                engine, store, video, video_id=video_id, like=args.like, config_dir=ROOT, top_k=args.top_k,
                layout=args.layout, name=args.name, segment_s=args.segment_min * 60, publish_job=publish,
            )  # fmt: skip
        log.info("stored %d segments of %s as stream %s and queued them", done.segments, video_id, done.stream_id)
        return 0
    except (ValueError, rp.ReplayError, DownloadError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
