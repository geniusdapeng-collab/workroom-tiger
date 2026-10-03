"""Durable JSON writes for the paper-trading accounting ledgers."""

from __future__ import annotations

import json
import os
import tempfile
import errno
import hashlib
import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class ConcurrentUpdateError(ValueError):
    """A caller attempted to save a stale accounting snapshot."""


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def file_digest(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def validate_finite_json(value: object) -> None:
    """Reject non-standard numeric JSON, including overflowing exponents."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Out of range float values are not JSON compliant")
    if isinstance(value, dict):
        for item in value.values():
            validate_finite_json(item)
    elif isinstance(value, list):
        for item in value:
            validate_finite_json(item)


def read_json_strict(path: str | Path) -> object:
    def invalid_constant(token: str):
        raise ValueError(f"Non-finite JSON constant: {token}")
    value = json.loads(Path(path).read_text(encoding="utf-8"),
                       parse_constant=invalid_constant)
    validate_finite_json(value)
    return value


@contextmanager
def file_transaction(path: str | Path, *, timeout: float = 60.0):
    """Serialize an entire RMW transaction on Windows and POSIX.

    The sidecar is deliberately persistent. Removing it after unlock could let
    a waiting process lock the old inode while a new process locks a new inode.
    RLock also serializes threads, for which flock alone is insufficient.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"Refusing a symlink accounting target: {target}")
    lock_path = target.with_name(target.name + ".lock")
    if lock_path.is_symlink():
        raise ValueError(f"Refusing a symlink lock: {lock_path}")
    key = str(lock_path.resolve())
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        acquired = False
        try:
            # msvcrt byte locks require a stable byte and a cursor at offset 0.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Accounting transaction lock timeout: {target}") from exc
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def write_json_atomic(path: str | Path, value: object, *, indent: int = 2) -> None:
    """Write beside the ledger, then replace it only after a complete flush.

    A failed serialization, write, or replacement leaves the previous ledger
    intact. The temporary file is created in the same directory so replace is
    atomic on the supported local filesystems.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"Refusing a symlink accounting target: {target}")
    validate_finite_json(value)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=indent,
                      allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_bytes_atomic(path: str | Path, value: bytes) -> None:
    """Restore an exact approval preimage without changing its snapshot hash."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"Refusing a symlink accounting target: {target}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.",
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
