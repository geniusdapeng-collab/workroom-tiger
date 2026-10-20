"""Offline checks of the actual fresh engine and installed provider caches.

Only temporary fixture cookie values are written/read. Existing user cache
databases are inspected through filesystem metadata, never opened. No market,
model, paid-provider, or broker request is made by these tests.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest

from trading_system.agent_evidence import atomic_json, digest, json_bytes, read_json
from trading_system.agent_runtime import (
    SOURCE_ROOT, executable_files, freeze_source, frozen_clock, policy_manifest,
    run_environment, stage_account, stage_calendar,
)


_CANARIES = {
    key: "TEST_FIXTURE_AMBIENT_VALUE_MUST_NOT_REACH_ENGINE"
    for key in (
        "TS_ENABLE_LLM", "TS_LLM_API_KEY", "TS_PROVIDER", "TS_DEMO",
        "TS_USE_TUNED", "TS_TUNED_PATH", "TS_SEARCH_SOURCES", "TS_LIVE",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY", "GEMINI_API_KEY", "TAVILY_API_KEY",
        "SERPAPI_API_KEY", "POLYGON_API_KEY", "FMP_API_KEY", "FRED_API_KEY",
        "ALPACA_API_KEY", "AWS_ACCESS_KEY_ID", "PYTHONPATH", "PYTHONHOME",
        "PYTHONSTARTUP", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    )
}
_DB_FILES = {"timezone": "tkr-tz.db", "cookie": "cookies.db", "isin": "isin-tkr.db"}
_CACHE_PROBE = textwrap.dedent('''
    import json
    import os
    from pathlib import Path
    import yfinance as yf
    from yfinance import cache

    def exercise_caches(marker):
        caches = {"timezone": cache.get_tz_cache(),
                  "cookie": cache.get_cookie_cache(), "isin": cache.get_isin_cache()}
        key = "TEST_FIXTURE_PROVIDER_ISOLATION"
        before = {name: obj.lookup(key) for name, obj in caches.items()}
        caches["timezone"].store(key, "America/New_York")
        caches["cookie"].store(key, {"fixture": marker})
        caches["isin"].store(key, "TEST_FIXTURE_TICKER")
        after = {name: obj.lookup(key) for name, obj in caches.items()}
        cookie = after["cookie"]
        assert cookie is not None and cookie["age"].total_seconds() >= 0
        after["cookie"] = cookie["cookie"]
        if before["cookie"] is not None:
            before["cookie"] = before["cookie"]["cookie"]
        return {"before": before, "after": after,
                "databases": {name: str(obj.get_db().database)
                              for name, obj in caches.items()},
                "home": os.environ.get("HOME"), "environment_keys": sorted(os.environ),
                "pid": os.getpid()}
''')
_ENGINE_MAIN = _CACHE_PROBE + textwrap.dedent('''
    # Import-time use makes this fail if the engine setter moves after loading
    # the business entry, even when _daily itself never constructs a provider.
    Path("business-entry-reached").write_text("TEST_FIXTURE", encoding="utf-8")
    observed = exercise_caches("fresh-engine")
    Path("cache-probe.json").write_text(json.dumps(observed), encoding="utf-8")
    raise RuntimeError("TEST_FIXTURE intentional stop after cache observation")
''')
_ENGINE_BOOTSTRAP = textwrap.dedent('''
    import json
    from pathlib import Path
    import runpy
    import sys
    entry, run, denied_directory = sys.argv[1:]
    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise AssertionError("TEST_FIXTURE forbids network access")
        if event == "os.mkdir" and denied_directory and str(args[0]) == denied_directory:
            raise PermissionError("TEST_FIXTURE denied cache directory creation")
    sys.addaudithook(audit)
    sys.argv = [entry, "engine", run]
    runpy.run_path(entry, run_name="__main__")
''')


def _prepare_run(root, number=1):
    source = root / "fixture-source"
    source.mkdir(parents=True)
    for name in executable_files(SOURCE_ROOT):
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE_ROOT / name, destination)
    (source / "main.py").write_text(_ENGINE_MAIN, encoding="utf-8")
    run = root / "runs" / ("run-" + f"{number:032x}")
    run.mkdir(parents=True)
    manifest = freeze_source(run, source)
    stage_account(root / "state", run, None)
    stage_calendar(run)
    request = {"schema_version": "tiger.agent-request/v1", "profile": "us-public-paper-v1",
               "paper_account": "public-usd", "idempotency_key": "cache-fixture-0001"}
    policy = policy_manifest()
    for name, value in (("request.json", request), ("policy.json", policy)):
        atomic_json(run, name, value)
    atomic_json(run, "admission.json", {
        "schema": "tiger.admission.v1", "run_id": run.name,
        "clock": frozen_clock(datetime.now(timezone.utc)), "parent": None,
        "request_sha256": digest(json_bytes(request)),
        "policy_sha256": digest(json_bytes(policy)),
        "source_sha256": digest(json_bytes(manifest)),
    })
    return run


def _engine_command(run, *, deny_mkdir=False):
    denied = str(run / "work" / "cache" / "yfinance") if deny_mkdir else ""
    return [sys.executable, "-I", "-B", "-c", _ENGINE_BOOTSTRAP,
            str(run / "source" / "trading_system" / "agent_supervisor.py"), str(run), denied]


def _engine(run, *, deny_mkdir=False):
    return subprocess.run(_engine_command(run, deny_mkdir=deny_mkdir),
                          cwd=run / "work", env=run_environment(run / "work"),
                          capture_output=True, text=True, timeout=30)


def _metadata_tree(root):
    """lstat only; do not follow symlinks or read private cache file contents."""
    if not root.exists() and not root.is_symlink():
        return None
    result = {}
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns,
        )
        if path.is_dir() and not path.is_symlink():
            pending.extend(path.iterdir())
    return result


def _user_cache_metadata():
    home = Path.home()
    roots = {home / "Library" / "Caches" / "py-yfinance", home / ".cache" / "py-yfinance"}
    ambient_xdg = os.environ.get("XDG_CACHE_HOME")
    if ambient_xdg and Path(ambient_xdg).is_absolute():
        roots.add(Path(ambient_xdg) / "py-yfinance")
    return {str(path): _metadata_tree(path) for path in roots}


def _assert_cache_result(run, observed):
    assert observed["before"] == {name: None for name in _DB_FILES}
    assert observed["after"] == {"timezone": "America/New_York",
                                 "cookie": {"fixture": "fresh-engine"},
                                 "isin": "TEST_FIXTURE_TICKER"}
    cache_root = run / "work" / "cache" / "yfinance"
    assert observed["databases"] == {
        name: str(cache_root / basename) for name, basename in _DB_FILES.items()}
    for basename in _DB_FILES.values():
        assert (cache_root / basename).is_file()
        assert (cache_root / basename).stat().st_size > 0
    # Includes any WAL/SHM sidecars, rather than checking only database names.
    for path in (run / "work").rglob("*"):
        if path.name.startswith(tuple(_DB_FILES.values())):
            assert path.parent == cache_root
    assert not (_CANARIES.keys() & set(observed["environment_keys"]))
    assert observed["home"] == os.environ.get("HOME")
    assert observed["pid"] != os.getpid()


def test_fresh_engines_write_all_real_caches_locally_without_user_cache_mutation(tmp_path, monkeypatch):
    for key, value in _CANARIES.items():
        monkeypatch.setenv(key, value)
    original_environment = dict(os.environ)
    before = _user_cache_metadata()
    pids = []
    for number in (1, 2):
        run = _prepare_run(tmp_path / f"attempt-{number}", number)
        completed = _engine(run)
        assert completed.returncode == 1, completed.stderr
        failure = read_json(run, "engine-result.json")
        assert failure["success"] is False and failure["error"]["code"] == "RUNNER_FAILED"
        observed = json.loads((run / "work" / "cache-probe.json").read_text())
        _assert_cache_result(run, observed)
        pids.append(observed["pid"])
    assert len(set(pids)) == 2
    assert _user_cache_metadata() == before
    assert dict(os.environ) == original_environment


def test_preinitialized_old_process_cannot_leak_cache_handles_into_fresh_engine(tmp_path):
    run = _prepare_run(tmp_path / "new-run")
    old_cache = tmp_path / "old-process-fixture-cache"
    old_cache.mkdir()
    launcher = _CACHE_PROBE + textwrap.dedent('''
        import subprocess
        import sys
        old_cache, command_json = sys.argv[1:]
        yf.set_tz_cache_location(old_cache)
        old_before = exercise_caches("old-process")
        def metadata():
            return {str(p): (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
                    for p in Path(old_cache).iterdir()}
        metadata_before = metadata()
        completed = subprocess.run(json.loads(command_json), capture_output=True,
                                   text=True, timeout=30)
        assert completed.returncode == 1, completed.stderr
        assert metadata() == metadata_before
        # Inspect the still-initialized old cache via real read APIs. Do not
        # call its setter again or mutate library-private manager variables.
        key = "TEST_FIXTURE_PROVIDER_ISOLATION"
        assert cache.get_tz_cache().lookup(key) == "America/New_York"
        assert cache.get_cookie_cache().lookup(key)["cookie"] == {"fixture": "old-process"}
        assert cache.get_isin_cache().lookup(key) == "TEST_FIXTURE_TICKER"
        print(json.dumps({"old": old_before, "old_metadata_unchanged": True}))
    ''')
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", launcher, str(old_cache),
         json.dumps(_engine_command(run))],
        cwd=run / "work", env=run_environment(run / "work"),
        capture_output=True, text=True, timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    old = json.loads(completed.stdout)
    observed = json.loads((run / "work" / "cache-probe.json").read_text())
    _assert_cache_result(run, observed)
    assert old["old"]["pid"] != observed["pid"]
    assert old["old_metadata_unchanged"] is True
    assert old["old"]["databases"] == {
        name: str(old_cache / basename) for name, basename in _DB_FILES.items()}


@pytest.mark.parametrize("failure", ["regular-file", "symlink", "mkdir-permission-error"])
def test_cache_setup_failure_prevents_business_entry(tmp_path, failure):
    run = _prepare_run(tmp_path / "fixture")
    run_environment(run / "work")
    target = run / "work" / "cache" / "yfinance"
    external = tmp_path / "external-fixture"
    external.mkdir()
    (external / "sentinel").write_text("untouched", encoding="utf-8")
    before_external = _metadata_tree(external)
    if failure == "regular-file":
        target.write_text("TEST_FIXTURE not a cache directory", encoding="utf-8")
    elif failure == "symlink":
        target.symlink_to(external, target_is_directory=True)
    completed = _engine(run, deny_mkdir=failure == "mkdir-permission-error")
    assert completed.returncode == 1, completed.stderr
    result = read_json(run, "engine-result.json")
    assert result["success"] is False
    assert result["error"]["code"] == (
        "ARTIFACT_WRITE_FAILED" if failure == "mkdir-permission-error" else "RESULT_INVALID")
    assert not (run / "work" / "business-entry-reached").exists()
    assert not (run / "work" / "cache-probe.json").exists()
    assert _metadata_tree(external) == before_external


def test_environment_is_allowlisted_without_mutating_home_or_parent(tmp_path, monkeypatch):
    for key, value in _CANARIES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HTTP_PROXY", "https://unreviewed.invalid:443")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@127.0.0.1:7890")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")
    before = dict(os.environ)
    work = tmp_path / "work"
    env = run_environment(work)
    assert not (_CANARIES.keys() & env.keys())
    assert "HTTP_PROXY" not in env and "HTTPS_PROXY" not in env
    assert env["ALL_PROXY"] == "socks5://127.0.0.1:7890"
    assert env.get("HOME") == before.get("HOME")
    assert env["XDG_CACHE_HOME"] == str(work / "cache")
    assert env["TMPDIR"] == str(work / "tmp")
    assert env["NETRC"] == os.devnull
    assert dict(os.environ) == before


@pytest.mark.parametrize("value,expected", [
    ("http://127.0.0.1:7890", True), ("https://localhost:7890/", True),
    ("socks5h://[::1]:7890", True), ("http://proxy.invalid:7890", False),
    ("http://127.0.0.1:7890/path", False), ("http://127.0.0.1:7890/?key=x", False),
    ("http://127.0.0.1:7890/#fragment", False), ("file://localhost/cache", False),
])
def test_only_reviewed_loopback_proxy_is_inherited(tmp_path, monkeypatch, value, expected):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv("http_proxy", value)
    env = run_environment(tmp_path / "work")
    assert (env.get("HTTP_PROXY") == value) is expected
    assert "http_proxy" not in env


def test_absent_home_is_not_replaced_with_synthetic_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    env = run_environment(tmp_path / "work")
    assert "HOME" not in env and "HOME" not in os.environ
