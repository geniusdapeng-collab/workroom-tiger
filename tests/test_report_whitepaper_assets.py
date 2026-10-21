"""Offline report/PDF delivery contracts; no live provider or account is used.

The default renderer and exporter cases reproduce delivery defects on the old
source. Calls using asset_dir and the new helper additionally specify the new
availability API; a missing API on old source is not labelled an old defect.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit

import pytest

from trading_system.data_models import PipelineResult
from trading_system.report_html import render_html


REPOSITORY = Path(__file__).resolve().parents[1]
PDF = "AI短线美股交易白皮书_20260730.pdf"


def _pdf_bytes(label=b"TEST_FIXTURE"):
    """A complete one-page PDF with accurate xref offsets, not a signature stub."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>",
               b"<< /Length 0 >>\nstream\n\nendstream"]
    data = b"%PDF-1.4\n%" + label + b"\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 5\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return data + b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pdfs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and ("download" in values or
                           unquote(urlsplit(values.get("href", "")).path).lower().endswith(".pdf")):
            self.pdfs.append(values)


def _pdf_links(markup):
    parser = _Links()
    parser.feed(markup)
    return parser.pdfs


def _assert_unavailable(markup):
    assert _pdf_links(markup) == []
    assert "白皮书" in markup and "未附" in markup
    assert "下载完整白皮书" not in markup and "4.1MB" not in markup


def _result():
    return PipelineResult("2026-10-21", "TEST_FIXTURE", raw={
        "market": {"market_id": "us", "currency": "USD",
                   "scan_filters": {"min_price": 5, "min_adv": 20_000_000, "currency": "USD"}}})


def _market(root, markup):
    root.mkdir(parents=True, exist_ok=True)
    (root / "result_20261021.json").write_text(json.dumps({
        "trade_date": "2026-10-21", "action": "HOLD", "picks": []}), encoding="utf-8")
    (root / "日报_20261021.html").write_text(markup, encoding="utf-8")
    return root


def _anchor(href):
    return f"<!DOCTYPE html><meta charset='utf-8'><a download href='{escape(href, quote=True)}'>白皮书 PDF</a>"


def _build(tmp_path, us, *, cn=None, hk=None, out=None):
    return subprocess.run([
        sys.executable, "-B", str(REPOSITORY / "scripts/build_site.py"),
        "--us", str(us), "--cn", str(cn or tmp_path / "absent-cn"),
        "--hk", str(hk or tmp_path / "absent-hk"),
        "--portfolio", str(tmp_path / "absent-portfolio"),
        "--out", str(out or tmp_path / "site"),
    ], cwd=REPOSITORY, capture_output=True, text=True, timeout=15)


def test_default_renderer_never_promises_an_unbundled_pdf(tmp_path, monkeypatch):
    # Even a PDF in cwd cannot prove that a returned HTML string will be saved there.
    (tmp_path / PDF).write_bytes(_pdf_bytes())
    monkeypatch.chdir(tmp_path)
    _assert_unavailable(render_html(_result()))


def test_explicit_output_directory_preserves_valid_download_and_actual_size(tmp_path):
    content = _pdf_bytes()
    (tmp_path / PDF).write_bytes(content)
    result = _result()
    before = deepcopy(asdict(result))
    page = render_html(result, asset_dir=tmp_path)
    links = _pdf_links(page)
    assert len(links) == 1 and unquote(links[0]["href"]) == PDF
    assert f"{len(content):,}" in page and "字节" in page
    assert "4.1MB" not in page and "data:application/pdf" not in page
    assert asdict(result) == before


@pytest.mark.parametrize("content", [
    b"", b"<html>upstream error</html>", base64.b64encode(_pdf_bytes()),
    _pdf_bytes()[:-16], b"%PDF-1.4\nnot a PDF\n%%EOF\n",
    _pdf_bytes().replace(b"startxref\n", b"startxref\n999999"),
], ids=["empty", "html", "base64", "truncated", "signature-only", "bad-xref"])
def test_renderer_discloses_invalid_adjacent_pdf(tmp_path, content):
    (tmp_path / PDF).write_bytes(content)
    _assert_unavailable(render_html(_result(), asset_dir=tmp_path))


@pytest.mark.parametrize("kind", ["missing", "directory", "outside-symlink"])
def test_renderer_discloses_missing_nonfile_or_escaping_pdf(tmp_path, kind):
    assets = tmp_path / "report"
    assets.mkdir()
    if kind == "directory":
        (assets / PDF).mkdir()
    elif kind == "outside-symlink":
        external = tmp_path / "outside.pdf"
        external.write_bytes(_pdf_bytes())
        (assets / PDF).symlink_to(external)
    _assert_unavailable(render_html(_result(), asset_dir=assets))


def test_renderer_rechecks_after_pdf_disappears(tmp_path):
    asset = tmp_path / PDF
    asset.write_bytes(_pdf_bytes())
    assert len(_pdf_links(render_html(_result(), asset_dir=tmp_path))) == 1
    asset.unlink()
    _assert_unavailable(render_html(_result(), asset_dir=tmp_path))


def test_renderer_discloses_read_failure(tmp_path, monkeypatch):
    import trading_system.report_assets as assets
    (tmp_path / PDF).write_bytes(_pdf_bytes())
    monkeypatch.setattr(assets, "read_pdf_asset", lambda *a, **k: (_ for _ in ()).throw(PermissionError("TEST_FIXTURE denied")))
    _assert_unavailable(render_html(_result(), asset_dir=tmp_path))


@pytest.mark.parametrize("location", ["implicit-output", "explicit-output", "json-only", "missing"])
def test_report_cli_resolves_asset_at_actual_html_output(tmp_path, location):
    source = tmp_path / "json"
    output = source if location == "implicit-output" else tmp_path / "html"
    source.mkdir()
    output.mkdir(exist_ok=True)
    result_path = source / "result_20261021.json"
    result_path.write_text(json.dumps(asdict(_result())), encoding="utf-8")
    if location != "missing":
        ((source if location == "json-only" else output) / PDF).write_bytes(_pdf_bytes())
    args = [sys.executable, "-B", "-m", "trading_system.report_html", str(result_path)]
    html_path = output / "日报_20261021.html"
    if location != "implicit-output":
        args += ["-o", str(html_path)]
    run = subprocess.run(args, cwd=REPOSITORY, capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    page = html_path.read_text(encoding="utf-8")
    if location in ("implicit-output", "explicit-output"):
        assert len(_pdf_links(page)) == 1
        assert (html_path.parent / unquote(_pdf_links(page)[0]["href"])).read_bytes() == _pdf_bytes()
    else:
        _assert_unavailable(page)


def test_exporter_preserves_legacy_declared_pdf_and_html_bytes(tmp_path):
    page = _anchor(PDF)
    source = _market(tmp_path / "us", page)
    (source / PDF).write_bytes(_pdf_bytes())
    run = _build(tmp_path, source)
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "site/us.html").read_bytes() == page.encode()
    assert (tmp_path / "site" / PDF).read_bytes() == _pdf_bytes()


def test_exporter_preserves_nested_encoded_pdf_with_query_and_fragment(tmp_path):
    relative = "assets/白皮书 & notes.pdf"
    source = _market(tmp_path / "us", _anchor(quote(relative) + "?v=1&lang=zh#page=1"))
    target = source / relative
    target.parent.mkdir()
    target.write_bytes(_pdf_bytes())
    run = _build(tmp_path, source)
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "site" / relative).read_bytes() == target.read_bytes()


@pytest.mark.parametrize("kind", ["missing", "invalid", "directory", "outside-symlink", "disappeared"])
def test_exporter_rejects_unavailable_declared_pdf_before_success(tmp_path, kind):
    source = _market(tmp_path / "us", _anchor(PDF))
    asset = source / PDF
    if kind == "invalid":
        asset.write_bytes(b"<html>not pdf</html>")
    elif kind == "directory":
        asset.mkdir()
    elif kind == "outside-symlink":
        external = tmp_path / "outside.pdf"
        external.write_bytes(_pdf_bytes())
        asset.symlink_to(external)
    elif kind == "disappeared":
        asset.write_bytes(_pdf_bytes())
        (source / "日报_20261021.html").write_text(render_html(_result(), asset_dir=source), encoding="utf-8")
        asset.unlink()
    run = _build(tmp_path, source)
    assert run.returncode != 0
    assert "官网已生成" not in run.stdout
    assert not (tmp_path / "site/us.html").exists()


@pytest.mark.parametrize("href", [
    "../outside.pdf", "%2e%2e/outside.pdf", "/outside.pdf", "//host/outside.pdf",
    "file:///outside.pdf", "https://example.test/whitepaper.pdf", "assets\\outside.pdf",
    "assets/%2e%2e/outside.pdf", "data:application/pdf;base64,JVBERi0xLjQ=",
], ids=["parent", "encoded-parent", "absolute", "netloc", "file-scheme", "remote", "backslash", "nested-parent", "embedded"])
def test_exporter_rejects_unsafe_pdf_locations(tmp_path, href):
    source = _market(tmp_path / "us", _anchor(href))
    (tmp_path / "outside.pdf").write_bytes(_pdf_bytes())
    run = _build(tmp_path, source)
    assert run.returncode != 0
    assert "官网已生成" not in run.stdout
    assert not (tmp_path / "site/us.html").exists()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_exporter_cannot_write_through_destination_symlinks(tmp_path, kind):
    relative = PDF if kind == "file" else "assets/paper.pdf"
    source = _market(tmp_path / "us", _anchor(relative))
    (source / relative).parent.mkdir(exist_ok=True)
    (source / relative).write_bytes(_pdf_bytes())
    out = tmp_path / "site"
    out.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.pdf"
    protected.write_bytes(b"TEST_FIXTURE must remain")
    if kind == "file":
        (out / relative).symlink_to(protected)
    else:
        (out / "assets").symlink_to(outside, target_is_directory=True)
    run = _build(tmp_path, source, out=out)
    assert run.returncode != 0
    assert protected.read_bytes() == b"TEST_FIXTURE must remain"
    assert not (outside / "paper.pdf").exists()


@pytest.mark.parametrize("identical", [True, False])
def test_exporter_reconciles_shared_asset_across_markets(tmp_path, identical):
    us = _market(tmp_path / "us", _anchor(PDF))
    cn = _market(tmp_path / "cn", _anchor(PDF))
    (us / PDF).write_bytes(_pdf_bytes())
    (cn / PDF).write_bytes(_pdf_bytes() if identical else _pdf_bytes(b"DIFFERENT_FIXTURE"))
    run = _build(tmp_path, us, cn=cn)
    assert (run.returncode == 0) is identical, run.stderr
    if identical:
        assert (tmp_path / "site" / PDF).read_bytes() == _pdf_bytes()
    else:
        assert not (tmp_path / "site/us.html").exists()


def test_exporter_accepts_honest_unbundled_report_without_inventing_an_asset(tmp_path):
    source = _market(tmp_path / "us", render_html(_result()))
    run = _build(tmp_path, source)
    assert run.returncode == 0, run.stderr
    _assert_unavailable((tmp_path / "site/us.html").read_text(encoding="utf-8"))
    assert not (tmp_path / "site" / PDF).exists()


def test_research_writer_preserves_valid_adjacent_attachment(tmp_path):
    import main
    (tmp_path / PDF).write_bytes(_pdf_bytes())
    args = SimpleNamespace(out=str(tmp_path), html=True, mode="daily", quiet=True)
    main._research_daily_reports(args, _result(), "TEST_FIXTURE research only")
    assert len(_pdf_links((tmp_path / "日报_20261021.html").read_text(encoding="utf-8"))) == 1


def test_regular_daily_writer_preserves_valid_adjacent_attachment(tmp_path, monkeypatch):
    import main
    from test_strict_daily_core import daily_fixture
    execution, _, args, _, attempts = daily_fixture(tmp_path, monkeypatch)
    (execution.paths.reports_dir / PDF).write_bytes(_pdf_bytes())
    monkeypatch.setattr(main, "run_pipeline", lambda **kwargs: _result())
    main._daily(args, "yahoo")
    html_path = execution.paths.reports_dir / "日报_20261021.html"
    assert len(_pdf_links(html_path.read_text(encoding="utf-8"))) == 1
    assert attempts == []


def test_strict_daily_does_not_claim_an_untracked_attachment(tmp_path, monkeypatch):
    import main
    from test_strict_daily_core import daily_fixture
    execution, _, args, _, attempts = daily_fixture(tmp_path, monkeypatch)
    # The strict artifact contract does not include PDF companions. Do not infer
    # authority from an adjacent file or expand that frozen contract implicitly.
    (execution.paths.reports_dir / PDF).write_bytes(_pdf_bytes())
    result = main._daily(args, "yahoo", execution=execution)
    _assert_unavailable(Path(result.report_paths["html"]).read_text(encoding="utf-8"))
    assert len(execution.summary()) == 39 and attempts == []
