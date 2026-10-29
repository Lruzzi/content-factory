"""The media store addresses files by key, never by absolute path (NFR-12)."""

from __future__ import annotations

import pytest

from common.media import LocalStore, MediaKeyError


def test_bytes_put_under_a_key_can_be_read_back(tmp_path):
    store = LocalStore(tmp_path)
    store.put_bytes("segments/abc/0001.ts", b"video")
    assert store.exists("segments/abc/0001.ts")
    assert store.path("segments/abc/0001.ts").read_bytes() == b"video"


def test_a_file_is_copied_in_and_the_source_is_left_alone(tmp_path):
    source = tmp_path / "render.mp4"
    source.write_bytes(b"clip")
    store = LocalStore(tmp_path / "media")
    store.put_file("clips/one.mp4", source)
    assert store.path("clips/one.mp4").read_bytes() == b"clip"
    assert source.exists()


def test_a_finished_put_leaves_no_temporary_file_behind(tmp_path):
    store = LocalStore(tmp_path)
    store.put_bytes("clips/one.mp4", b"clip")
    assert [p.name for p in (tmp_path / "clips").iterdir()] == ["one.mp4"]


def test_putting_the_same_key_again_replaces_the_file(tmp_path):
    store = LocalStore(tmp_path)
    store.put_bytes("a.bin", b"one")
    store.put_bytes("a.bin", b"two")
    assert store.path("a.bin").read_bytes() == b"two"


def test_delete_removes_the_file_and_is_safe_to_repeat(tmp_path):
    store = LocalStore(tmp_path)
    store.put_bytes("a.bin", b"x")
    store.delete("a.bin")
    store.delete("a.bin")
    assert not store.exists("a.bin")


@pytest.mark.parametrize(
    "bad",
    ["", ".", "..", "../x", "/etc/passwd", "a/../../b", "a//b", "a/./b", "a/", "a\\b", "x\x00y", ".hidden", "a/.tmp"],
)
def test_keys_that_could_leave_the_store_are_rejected(tmp_path, bad):
    with pytest.raises(MediaKeyError):
        LocalStore(tmp_path).path(bad)


def test_a_symlink_inside_the_store_cannot_lead_outside_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(MediaKeyError):
        LocalStore(root).path("link/secret.txt")
