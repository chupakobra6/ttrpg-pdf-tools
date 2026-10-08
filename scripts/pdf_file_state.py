"""Stable PDF snapshots and conditional publication for this tool's writers."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
from typing import Iterator


class FileConflict(RuntimeError):
    """The file no longer matches the snapshot used to prepare a change."""


@dataclass(frozen=True)
class FileSnapshot:
    data: bytes
    version: str


def _identity(stat: os.stat_result) -> tuple[int, ...]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def read_snapshot(path: Path) -> FileSnapshot:
    # Read bytes and identity from the same descriptor, then detect replacement.
    with path.open('rb') as stream:
        before = _identity(os.fstat(stream.fileno()))
        data = stream.read()
        after = _identity(os.fstat(stream.fileno()))
    if before != after or after != _identity(path.stat()):
        raise FileConflict(f"Файл изменился во время чтения: {path}. Откройте его заново.")
    version = hashlib.sha256(repr(after[:2]).encode() + b'\0' + data).hexdigest()
    return FileSnapshot(data, version)


def file_version(path: Path) -> str | None:
    try:
        return read_snapshot(path).version
    except FileNotFoundError:
        return None


def require_version(path: Path, expected: str | None) -> None:
    if file_version(path) != expected:
        raise FileConflict(f"Файл изменён или удалён: {path}. Сохранение отменено; откройте свежую версию и сравните правки.")


@contextmanager
def locked_files(*paths: Path) -> Iterator[None]:
    # A stable sidecar survives os.replace. Never unlink it: waiters may hold its inode.
    lock_dir = Path.home() / '.cache' / 'ttrpg-pdf-tools' / 'locks'
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ExitStack() as stack:
        for path in sorted({p.resolve() for p in paths}):
            name = hashlib.sha256(os.fsencode(path)).hexdigest() + '.lock'
            lock = stack.enter_context((lock_dir / name).open('a+b'))
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def publish(temporary: Path, target: Path, expected: dict[Path, str | None]) -> str:
    """Keep the old target until preparation succeeds and every input still matches.

    All tool writers take the same locks. External non-cooperating writers can
    still race the last check and rename; advisory locking cannot prevent that.
    """
    with locked_files(*expected, target):
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        if target.exists():
            os.chmod(temporary, target.stat().st_mode & 0o777)
        published_version = read_snapshot(temporary).version
        for path, version in expected.items():
            require_version(path, version)
        os.replace(temporary, target)
        return published_version
