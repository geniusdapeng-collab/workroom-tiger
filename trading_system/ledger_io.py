"""Durable JSON writes for the paper-trading accounting ledgers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_json_atomic(path: str | Path, value: object, *, indent: int = 2) -> None:
    """Write beside the ledger, then replace it only after a complete flush.

    A failed serialization, write, or replacement leaves the previous ledger
    intact. The temporary file is created in the same directory so replace is
    atomic on the supported local filesystems.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
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


def read_json_strict(path: str | Path):
    """Read a complete standard JSON value; duplicate/nonfinite values fail."""
    import math

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("Duplicate JSON key")
            out[key] = value
        return out

    def number(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return value

    def invalid(raw):
        raise ValueError("Non-finite JSON constant")

    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=pairs, parse_float=number,
                      parse_constant=invalid)


def write_text_atomic(path: str | Path, value: str) -> None:
    """Publish complete UTF-8 report bytes and propagate all storage failures."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
