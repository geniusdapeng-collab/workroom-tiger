"""Local PDF companions for report delivery, using only the standard library.

These checks cover paths and basic PDF transport structure (header, final EOF
and cross-reference location). They do not replace a full PDF content parser or
certify the document's trading claims. Strict runs do not read companions.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from urllib.parse import unquote, urlsplit


WHITEPAPER_FILENAME = "AI短线美股交易白皮书_20260730.pdf"


class ReportAssetError(ValueError):
    """A declared report companion cannot be safely delivered."""


def safe_relative_target(directory: str | os.PathLike, relative: str | Path) -> Path:
    """Resolve within an explicitly selected directory, rejecting symlink parts."""
    root = Path(directory)
    if root.is_symlink():
        raise ReportAssetError("Report asset directory must not be a symlink")
    root = root.resolve(strict=True)
    name = str(relative)
    parts = PurePosixPath(name)
    if (not name or parts.is_absolute() or ".." in parts.parts or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ReportAssetError("Report asset path must be a safe relative path")
    target = root
    for component in parts.parts:
        target = target / component
        if target.is_symlink():
            raise ReportAssetError("Report asset path must not contain symlinks")
    if not target.resolve().is_relative_to(root):
        raise ReportAssetError("Report asset path escapes its directory")
    return target


def _validate_pdf(data: bytes) -> None:
    if not re.match(rb"%PDF-\d\.\d(?:\r\n|\r|\n)", data):
        raise ReportAssetError("Report attachment has no valid PDF header")
    ending = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*\Z", data)
    if ending is None:
        raise ReportAssetError("Report PDF is incomplete (cross-reference/EOF missing)")
    offset = int(ending.group(1))
    if offset >= ending.start():
        raise ReportAssetError("Report PDF cross-reference offset is invalid")
    reference = data[offset:ending.start()]
    table = re.match(rb"xref\s+\d+\s+\d+\s", reference)
    # PDF cross-reference streams start at an indirect object instead of 'xref'.
    stream = (re.match(rb"\d+\s+\d+\s+obj\b", reference)
              and re.search(rb"/Type\s*/XRef\b", reference.split(b"stream", 1)[0]))
    if not table and not stream:
        raise ReportAssetError("Report PDF cross-reference target is invalid")


def read_pdf_asset(directory: str | os.PathLike, relative: str | Path) -> bytes:
    """Read one regular local PDF; missing, unreadable and unsafe files raise."""
    path = safe_relative_target(directory, relative)
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReportAssetError("Report attachment must be a regular PDF file")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    _validate_pdf(data)
    return data


def declared_pdf_paths(markup: str) -> set[Path]:
    """Collect declared PDF downloads without changing a report's HTML/URLs."""
    class Links(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.paths: set[Path] = set()
            self.has_base = False

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "base" and "href" in values:
                self.has_base = True
            if tag != "a":
                return
            href = values.get("href") or ""
            try:
                uri = urlsplit(href)
                name = unquote(uri.path, errors="strict")
            except ValueError as exc:
                if "download" in values:
                    raise ReportAssetError("Report download URL is invalid") from exc
                return
            if "download" not in values and not name.lower().endswith(".pdf"):
                return
            if (uri.scheme or uri.netloc or href != href.strip() or "\\" in href
                    or any(ord(char) < 32 or ord(char) == 127 for char in href)
                    or not name.lower().endswith(".pdf")):
                raise ReportAssetError("Report PDF download must use a local relative URL")
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ReportAssetError("Report PDF download escapes its directory")
            self.paths.add(Path(*relative.parts))

    parser = Links()
    parser.feed(markup)
    parser.close()
    if parser.paths and parser.has_base:
        raise ReportAssetError("Report PDF downloads cannot use a base URL")
    return parser.paths


def write_pdf_asset(directory: str | os.PathLike, relative: str | Path, data: bytes) -> None:
    """Publish verified bytes without following destination symlinks/hardlinks."""
    _validate_pdf(data)
    target = safe_relative_target(directory, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = safe_relative_target(directory, relative)
    descriptor, temporary = tempfile.mkstemp(prefix=".report-pdf-", dir=target.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), 0o644)
        # Validate the bytes actually written before replacing an existing asset.
        actual = read_pdf_asset(temporary.parent, temporary.name)
        expected_hash = hashlib.sha256(data).digest()
        if hashlib.sha256(actual).digest() != expected_hash:
            raise OSError("Report PDF temporary-file readback differs")
        safe_relative_target(directory, relative)
        os.replace(temporary, target)
        if hashlib.sha256(read_pdf_asset(directory, relative)).digest() != expected_hash:
            raise OSError("Published report PDF readback differs")
    finally:
        temporary.unlink(missing_ok=True)
