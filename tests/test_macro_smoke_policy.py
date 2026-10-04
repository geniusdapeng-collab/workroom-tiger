"""Run the original live macro case in real pytest children with owned HTTP fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


_REPO = Path(__file__).resolve().parents[1]
_OFFICIAL_TEST = "tests/test_provider_official.py"
_SMOKE_CASE = f"{_OFFICIAL_TEST}::test_smoke_real_fred_cboe"
_FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
_CBOE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"

# Loaded before collection; no provider or test module is imported by this plugin.
_HTTP_PLUGIN = r'''
from datetime import date, timedelta
import json
import os
from pathlib import Path
import socket

import requests

_root = Path(os.environ["TIGER_MACRO_FIXTURE_DIR"])
_mode = os.environ["TIGER_MACRO_FIXTURE_MODE"]
_fred = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
_cboe = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


def _record(item):
    with (_root / "http-attempts.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item) + "\n")


def _deny_socket(*args, **kwargs):
    _record({"kind": "socket"})
    raise AssertionError("unowned socket access in macro policy regression")


def _deny_session(*args, **kwargs):
    _record({"kind": "session"})
    raise AssertionError("unowned Requests session in macro policy regression")


def _get(url, timeout=None, **kwargs):
    if url not in {_fred, _cboe}:
        _record({"kind": "unexpected-http"})
        raise AssertionError("unexpected URL in macro policy regression")
    _record({"kind": "get", "url": url, "timeout": timeout})
    source = "FRED" if url == _fred else "CBOE"
    if _mode == source.lower() + "-timeout":
        raise requests.exceptions.ConnectTimeout(source + " controlled ConnectTimeout")
    rows = 29 if _mode == source.lower() + "-short" else 30
    dates = [date(2026, 7, 1) + timedelta(days=index) for index in range(rows)]
    if source == "FRED":
        body = "DATE,DGS10\n" + "".join(f"{day.isoformat()},4.67\n" for day in dates)
    else:
        body = "DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(
            f"{day:%m/%d/%Y},15.0,15.5,14.5,15.1\n" for day in dates
        )
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response.encoding = "utf-8"
    response._content = body.encode("utf-8")
    return response


socket.socket.connect = _deny_socket
socket.create_connection = _deny_socket
requests.sessions.Session.request = _deny_session
requests.get = _get


def pytest_collection_finish(session):
    (_root / "collected.json").write_text(
        json.dumps([item.nodeid for item in session.items]), encoding="utf-8"
    )
'''


def _run_original(tmp_path, *, flag=None, mode="valid", collect_only=False):
    (tmp_path / "_tiger_macro_http.py").write_text(_HTTP_PLUGIN, encoding="utf-8")
    # An explicit allowlist prevents ambient live flags, credentials and pytest plugins
    # from changing the behavior of the original case in the child.
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(_REPO)]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TIGER_MACRO_FIXTURE_DIR": str(tmp_path),
        "TIGER_MACRO_FIXTURE_MODE": mode,
    }
    if flag is not None:
        environment["RUN_TIGER_LIVE_MACRO_TESTS"] = flag
    report = tmp_path / "pytest.xml"
    arguments = [
        sys.executable, "-m", "pytest", "-p", "_tiger_macro_http", "-q", "-rs",
        "-p", "no:cacheprovider", f"--junitxml={report}",
    ]
    if collect_only:
        arguments.extend(["--collect-only", _OFFICIAL_TEST])
    else:
        arguments.append(_SMOKE_CASE)
    (tmp_path / "invocation.json").write_text(json.dumps({
        "argv": arguments, "cwd": str(_REPO), "environment": environment,
        "ownedHttpFixture": True,
    }, indent=2), encoding="utf-8")
    result = subprocess.run(
        arguments, cwd=_REPO, env=environment, capture_output=True,
        text=True, encoding="utf-8", timeout=30, check=False,
    )
    (tmp_path / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    attempts = tmp_path / "http-attempts.jsonl"
    calls = [json.loads(line) for line in attempts.read_text(encoding="utf-8").splitlines()] if attempts.exists() else []
    return result, calls, ET.parse(report).getroot()


def _case(report):
    cases = report.findall(".//testcase")
    assert len(cases) == 1
    assert cases[0].get("name") == "test_smoke_real_fred_cboe"
    return cases[0]


def _expected_calls(*sources):
    return [{"kind": "get", "url": url, "timeout": 20} for url in sources]


@pytest.mark.parametrize("flag", [None, "1"])
def test_collection_never_probes_external_macro_sources(tmp_path, flag):
    result, calls, _report = _run_original(tmp_path, flag=flag, collect_only=True)
    assert result.returncode == 0, result.stdout + result.stderr
    collected = json.loads((tmp_path / "collected.json").read_text(encoding="utf-8"))
    assert _SMOKE_CASE in collected
    assert len(collected) == 6
    assert calls == []


@pytest.mark.parametrize("flag", [None, "0", "true", ""])
def test_original_smoke_requires_an_explicit_one(tmp_path, flag):
    result, calls, report = _run_original(tmp_path, flag=flag)
    assert result.returncode == 0, result.stdout + result.stderr
    case = _case(report)
    assert case.find("failure") is None and case.find("error") is None
    skipped = case.find("skipped")
    assert skipped is not None
    assert "RUN_TIGER_LIVE_MACRO_TESTS=1" in skipped.get("message", "")
    assert "RUN_TIGER_LIVE_MACRO_TESTS=1" in result.stdout
    assert calls == []


def test_opt_in_original_smoke_reads_both_sources_at_the_existing_minimum(tmp_path):
    result, calls, report = _run_original(tmp_path, flag="1")
    assert result.returncode == 0, result.stdout + result.stderr
    case = _case(report)
    assert list(case) == []
    assert calls == _expected_calls(_FRED, _CBOE)


@pytest.mark.parametrize("source", ["fred", "cboe"])
def test_opt_in_source_timeout_is_a_failure_in_the_original_case(tmp_path, source):
    result, calls, report = _run_original(tmp_path, flag="1", mode=source + "-timeout")
    assert result.returncode == 1, result.stdout + result.stderr
    case = _case(report)
    assert case.find("skipped") is None and case.find("error") is None
    failure = case.find("failure")
    assert failure is not None
    assert source.upper() + " controlled ConnectTimeout" in failure.get("message", "")
    assert calls == _expected_calls(*([_FRED] if source == "fred" else [_FRED, _CBOE]))


@pytest.mark.parametrize("source", ["fred", "cboe"])
def test_opt_in_short_series_is_a_failure_in_the_original_case(tmp_path, source):
    result, calls, report = _run_original(tmp_path, flag="1", mode=source + "-short")
    assert result.returncode == 1, result.stdout + result.stderr
    case = _case(report)
    assert case.find("skipped") is None and case.find("error") is None
    failure = case.find("failure")
    assert failure is not None
    assert source.upper() in failure.get("message", "")
    assert "29<30" in failure.get("message", "")
    assert calls == _expected_calls(*([_FRED] if source == "fred" else [_FRED, _CBOE]))
