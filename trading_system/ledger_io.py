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
