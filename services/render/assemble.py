"""Join pieces of several recording files into one source, for a recap. Each piece is cut from its file by a process of
its own and scaled to the size and rate of the first piece (a restarted recording can come back at another
resolution), encoded once at high quality; the finished pieces are then joined WITHOUT decoding (concat demuxer, stream
copy). One piece in memory at a time matters: a single process with a concat filter opens every piece at once and
queues the frames of the later ones while it plays the first, which at 1080p60 filled a 1.5 GB container and was killed.
The usual render then runs on this joined file, so layout, captions and loudness work exactly as for any other clip.
Argument lists only, local files only, and a half-written result never has the final name."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from common.media import MediaStore
from services.render import ffmpeg as ff
from services.render import seek
from services.render.errors import RenderError

TIMEOUT_S = 900
SCRATCH_KEY = "render-tmp"  # on the media disk, not in /tmp: the container's /tmp is a 256 MB folder held in memory
STALE_S = 6 * 3600  # a work folder this old belongs to a process that was killed
SLACK_S = 0.5  # how far a piece may reach past the end of its file (the cut falls inside a frame)


class AssembleError(RenderError):
    pass


class PieceIn(BaseModel):
    """Part of one recording file, on that file's own clock."""

    model_config = ConfigDict(extra="forbid")
    source_key: str
    start_s: float = Field(ge=0)
    end_s: float

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s


def piece_command(ffmpeg: str, file: Path, piece: PieceIn, width: int, height: int, fps: float, out: Path) -> list[str]:
    """Cut one piece to exactly its length, at the common size and rate, as MPEG-TS (joins cleanly by copy)."""
    coarse, trim = seek.split(piece.start_s)  # seek well before the piece, then cut its exact start
    cut_v = f"trim=start={trim:g},setpts=PTS-STARTPTS," if trim > 0 else ""
    cut_a = f"atrim=start={trim:g},asetpts=PTS-STARTPTS," if trim > 0 else ""
    chains = (
        f"[0:v]{cut_v}scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.3f},setpts=PTS-STARTPTS[v];"
        f"[0:a]{cut_a}aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS,apad[a]"
    )
    return [
        ffmpeg, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file",
        "-ss", f"{coarse:.3f}", "-t", f"{trim + piece.length_s:.3f}", "-i", str(file),
        "-filter_complex", chains, "-map", "[v]", "-map", "[a]",
        "-t", f"{piece.length_s:.3f}", "-r", f"{fps:.3f}",  # the exact length for picture and sound; the rate stated
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-f", "mpegts", str(out),
    ]  # fmt: skip


def concat_command(ffmpeg: str, list_file: Path, part: Path) -> list[str]:
    return [
        ffmpeg, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(part),
    ]  # fmt: skip


def join(store: MediaStore, pieces: list[PieceIn], out: Path) -> Path:
    files = []
    for piece in pieces:
        path = store.path(piece.source_key)  # type: ignore[attr-defined]
        if not path.is_file():
            raise AssembleError(f"source {piece.source_key!r} is not in the media store")
        info = ff.probe(path)
        if not info.has_audio:
            raise AssembleError(f"source {piece.source_key!r} has no audio")
        if piece.end_s <= piece.start_s or piece.end_s > info.duration_s + SLACK_S:
            raise AssembleError(
                f"the piece {piece.start_s:g}-{piece.end_s:g} s is outside {piece.source_key!r} "
                f"({info.duration_s:.1f} s long)"
            )
        files.append(path)
    first = ff.probe(files[0])
    part = out.with_name(out.name + ".part")
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = ff._binary("ffmpeg")
    scratch = Path(store.path(SCRATCH_KEY))  # type: ignore[attr-defined]
    scratch.mkdir(parents=True, exist_ok=True)
    _sweep(scratch)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="assemble-") as work:
        made = []
        for i, (file, piece) in enumerate(zip(files, pieces, strict=True)):
            target = Path(work) / f"{i:03d}.ts"
            _run(piece_command(ffmpeg, file, piece, first.width, first.height, first.fps, target), f"piece {i + 1}")
            made.append(target)
        listing = Path(work) / "pieces.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in made), encoding="utf-8")  # our own paths: no quotes in them
        try:
            _run(concat_command(ffmpeg, listing, part), "the join")
        except AssembleError:
            part.unlink(missing_ok=True)
            raise
    part.replace(out)
    return out


def _sweep(scratch: Path) -> None:
    """Remove work folders left by a join that was killed (they hold whole video pieces)."""
    for old in scratch.glob("assemble-*"):
        if old.is_dir() and time.time() - old.stat().st_mtime > STALE_S:
            shutil.rmtree(old, ignore_errors=True)


def _run(cmd: list[str], what: str) -> None:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)  # noqa: S603  # nosec B603
    except subprocess.TimeoutExpired as exc:
        raise AssembleError(f"ffmpeg timed out on {what}") from exc
    if done.returncode != 0:
        raise AssembleError(f"ffmpeg could not make {what}: {ff._tail(done.stderr)}")
