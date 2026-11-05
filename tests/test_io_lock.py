from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from dossier.core.errors import LockedError
from dossier.core.io import (
    atomic_write_json,
    atomic_write_text,
    read_json,
    sha256_file,
    sha256_text,
)
from dossier.core.lock import LockHolder, WorkspaceLock
from dossier.core.paths import canonical_relpath, casefold_key, nfc
from dossier.platform.fake import FakeState


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "file.txt"
    atomic_write_text(target, "línea uno\nline two\n")
    assert target.read_bytes() == "línea uno\nline two\n".encode()
    assert [p.name for p in target.parent.iterdir()] == ["file.txt"]


def test_json_roundtrip_and_hashes(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_json(target, {"b": 1, "a": "ñ"})
    assert read_json(target) == {"a": "ñ", "b": 1}
    assert sha256_file(target) == sha256_text(target.read_text(encoding="utf-8"))


def test_canonical_relpath_is_nfc_posix(tmp_path: Path) -> None:
    decomposed = "Reunión"  # macOS-style decomposed accent
    path = tmp_path / "a" / decomposed / "x.md"
    key = canonical_relpath(path, tmp_path)
    assert key == "a/Reunión/x.md"
    assert key == nfc(key)
    assert casefold_key("A/Reunión/X.md") == casefold_key(key)


def test_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    with WorkspaceLock(tmp_path, "sync", run_id="r1") as lock:
        assert lock.path.exists()
        with pytest.raises(LockedError), WorkspaceLock(tmp_path, "sync"):
            pass
    assert not (tmp_path / "sync.lock").exists()


def _write_holder(path: Path, pid: int, hostname: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(LockHolder(pid=pid, hostname=hostname).model_dump_json(), encoding="utf-8")


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path: Path, fake_platform: FakeState) -> None:
    fake_platform.dead_pids.add(424242)
    _write_holder(tmp_path / "sync.lock", 424242, socket.gethostname())
    with WorkspaceLock(tmp_path, "sync") as lock:
        holder = json.loads(lock.path.read_text(encoding="utf-8"))
        assert holder["pid"] == os.getpid()


def test_lock_from_other_host_counts_as_live(tmp_path: Path, fake_platform: FakeState) -> None:
    fake_platform.dead_pids.add(1)
    _write_holder(tmp_path / "sync.lock", 1, "some-other-host")
    with pytest.raises(LockedError), WorkspaceLock(tmp_path, "sync"):
        pass


def test_unreadable_lock_is_stale(tmp_path: Path) -> None:
    (tmp_path / "sync.lock").write_text("not json", encoding="utf-8")
    with WorkspaceLock(tmp_path, "sync"):
        pass
