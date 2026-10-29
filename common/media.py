"""The media store: files are addressed by key, never by absolute path (NFR-12).

`LocalStore` keeps them under one folder. An S3-compatible store (R2) can implement the same
interface later, which is what lets services run on different hosts.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Protocol

SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


class MediaKeyError(ValueError):
    """The key is not a safe relative key."""


class MediaStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def put_file(self, key: str, source: Path) -> None: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        """The file for a key. Keys are `a/b/c.ext`: no empty, dotted-leading or odd segments, no escaping."""
        if not key or not all(SEGMENT.fullmatch(part) for part in key.split("/")):
            raise MediaKeyError(f"not a valid media key: {key!r}")
        resolved = (self.root / key).resolve()
        if not resolved.is_relative_to(self.root):  # a symlink inside the store pointing out of it
            raise MediaKeyError(f"media key leaves the store: {key!r}")
        return resolved

    def put_bytes(self, key: str, data: bytes) -> None:
        target = self._prepare(key)
        temp = target.with_name(f".tmp-{uuid.uuid4().hex}")
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def put_file(self, key: str, source: Path) -> None:
        target = self._prepare(key)
        temp = target.with_name(f".tmp-{uuid.uuid4().hex}")
        try:
            shutil.copyfile(source, temp)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def _prepare(self, key: str) -> Path:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
