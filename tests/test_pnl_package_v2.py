"""Actual package/module closure and anonymous transport, in isolated Git fixtures."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = Path(os.environ.get("TIGER_TEST_NODE") or shutil.which("node")
            or "/Users/mac/Library/Application Support/TigerPnL/runtime/node")
SPEC = importlib.util.spec_from_file_location("tiger_website_package_test", ROOT / "pnl/deploy/package.py")
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, timeout=20)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    shutil.copytree(ROOT / "pnl", repo / "pnl", ignore=shutil.ignore_patterns("__pycache__"))
    shared = repo / PACKAGE.SHARED
    shared.parent.mkdir(parents=True)
    shared.write_bytes((ROOT / PACKAGE.SHARED).read_bytes())
    git(repo, "init", "--quiet")
    git(repo, "add", "pnl", PACKAGE.SHARED)
    git(repo, "-c", "user.name=Synthetic test fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "SYNTHETIC_TEST isolated package source")
    return repo, git(repo, "rev-parse", "HEAD").decode().strip()


def test_exact_git_package_contains_shared_contract_and_imports_all_runtime_entries(source, tmp_path):
    repo, commit = source
    # Untracked configuration must never enter an exact-commit package.
    (repo / "pnl/runtime.env").write_text("SYNTHETIC_TEST_PRIVATE_CONFIGURATION_NEVER_PACKAGE=1\n")
    body, manifest = PACKAGE.build(repo, commit)
    assert manifest["commit"] == commit
    assert PACKAGE.SHARED in manifest["runtimeClosure"]
    assert "pnl/runtime.env" not in [row["path"] for row in manifest["files"]]
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        archive.extractall(extracted, filter="data")
    assert hashlib.sha256(body).hexdigest() == manifest["archiveSha256"]
    script = ('import {pathToFileURL} from "node:url"; const files=process.argv.slice(1);'
              'process.argv.length=1; for(const file of files) await import(pathToFileURL(file).href);'
              'console.log("ISOLATED_RUNTIME_CLOSURE_LOADED_NO_SERVER_NO_ACCOUNT");')
    if not NODE.exists():
        pytest.fail("A real trusted Node runtime is required for package closure verification")
    completed = subprocess.run([str(NODE), "--input-type=module", "-e", script,
        *[str(extracted / name) for name in PACKAGE.ENTRIES]],
        cwd=extracted, capture_output=True, text=True, timeout=20,
        env={key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ})
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ISOLATED_RUNTIME_CLOSURE_LOADED_NO_SERVER_NO_ACCOUNT"
    assert not (extracted / "data").exists()


def test_unregistered_or_missing_import_is_rejected_before_package_creation(source):
    repo, commit = source
    git(repo, "rm", "--quiet", PACKAGE.SHARED)
    git(repo, "-c", "user.name=Synthetic test fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "SYNTHETIC_TEST omit required shared module")
    with pytest.raises(ValueError, match="dependency is absent"):
        PACKAGE.build(repo, "HEAD")
    git(repo, "reset", "--hard", "--quiet", commit)
    with (repo / "pnl/server.mjs").open("a") as handle:
        handle.write('\nimport "../private-outside-website.mjs";\n')
    git(repo, "add", "pnl/server.mjs")
    git(repo, "-c", "user.name=Synthetic test fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "SYNTHETIC_TEST unregistered module")
    with pytest.raises(ValueError, match="outside the reviewed package"):
        PACKAGE.build(repo, "HEAD")


def test_remote_fetcher_recreates_exact_archive_and_refuses_wrong_source_or_overwrite(source, tmp_path):
    repo, commit = source
    body, manifest = PACKAGE.build(repo, commit)
    blobs = {row["path"]: git(repo, "show", commit + ":" + row["path"]) for row in manifest["files"]}
    target = tmp_path / "transport"
    target.mkdir()
    code = PACKAGE.transport(manifest).replace("/var/tmp/tiger-pnl-staging", str(target))
    compile(code, "anonymous-exact-source-fetcher", "exec")
    calls = []
    prefix = "https://raw.githubusercontent.com/geniusdapeng-collab/workroom-tiger/" + commit + "/"
    def response(url, timeout):
        assert timeout == 45 and url.startswith(prefix)
        name = url[len(prefix):]
        calls.append(name)
        return io.BytesIO(blobs[name])
    with patch("urllib.request.urlopen", response), redirect_stdout(io.StringIO()):
        exec(code, {})
    staged = target / ("tiger-pnl-" + commit + ".tar.gz")
    assert staged.read_bytes() == body
    assert sorted(calls) == sorted(blobs)
    with patch("urllib.request.urlopen", response), redirect_stdout(io.StringIO()), pytest.raises(FileExistsError):
        exec(code, {})
    assert staged.read_bytes() == body
    failed_target = tmp_path / "transport-rejected"
    failed_target.mkdir()
    bad_code = code.replace(str(target), str(failed_target))
    def wrong(url, timeout):
        return io.BytesIO(b"SYNTHETIC_TEST_WRONG_SOURCE_BYTES")
    with patch("urllib.request.urlopen", wrong), pytest.raises(ValueError, match="Source bytes differ"):
        exec(bad_code, {})
    assert not list(failed_target.iterdir())


def test_legacy_pnl_only_archive_is_a_real_missing_module_failure(source, tmp_path):
    repo, commit = source
    files = {name: value for name, value in
        ((row["path"], (row["mode"], git(repo, "show", commit + ":" + row["path"])))
         for row in PACKAGE.build(repo, commit)[1]["files"]) if name.startswith("pnl/")}
    extracted = tmp_path / "legacy-pnl-only"
    extracted.mkdir()
    with tarfile.open(fileobj=io.BytesIO(PACKAGE.pack(files)), mode="r:gz") as archive:
        archive.extractall(extracted, filter="data")
    script = ('import {pathToFileURL} from "node:url"; const file=process.argv[1];'
              'process.argv.length=1; await import(pathToFileURL(file).href);')
    completed = subprocess.run([str(NODE), "--input-type=module", "-e", script,
        str(extracted / "pnl/server.mjs")], capture_output=True, text=True, timeout=20)
    assert completed.returncode != 0
    assert "ERR_MODULE_NOT_FOUND" in completed.stderr and "required-v2-contract.mjs" in completed.stderr
