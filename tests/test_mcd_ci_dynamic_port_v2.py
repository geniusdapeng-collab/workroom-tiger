"""MC-009 finite contract probes; no PostgreSQL, network, or remote CI.

Run from the candidate repository root. The MCD wrapper must explicitly bind
TIGER_TEST_NODE. The historical red harness runs this same file from an exact
read-only Git-blob snapshot, not a fabricated detached audit or MCD receipt.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path.cwd()
WORKFLOW = ROOT / ".github/workflows/workloom-ui-contract.yml"
PG_TEST = ROOT / "governance/apps/server/src/trpc/frontend-contract.pg.test.ts"
HELPER = ROOT / "governance/apps/server/src/test-fixtures/frontend-database.ts"
HELPER_IMPORT = 'import { assertFrontendFixtureDatabase } from "../test-fixtures/frontend-database.js";\n'
OLD_PG_SHA256 = "372e05f300dd84dd51792877776187c3bb18aa8fbcd90655ea88e117b4c17438"
KEYS = ("DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL")
OLD_GUARD = '''    const urls = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"].map((key) => new URL(process.env[key] ?? "http://invalid"));
    if (urls.some((url) => !["127.0.0.1", "[::1]"].includes(url.hostname)
      || url.port !== "55432" || !["postgres:", "postgresql:"].includes(url.protocol)
      || !/^\\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname) || url.pathname !== urls[0]!.pathname)) {
      throw new Error("只允许同一 workloom_tiger_ 前缀的本机隔离测试库，三种数据库角色必须指向该库");
    }
'''


def fixture(port="49153", host="127.0.0.1"):
    return {"TIGER_TEST_DATABASE_PORT": port, **{key:
        f"postgres://{role}:fixture@{host}:{port}/workloom_tiger_fixture"
        for key, role in zip(KEYS, ("postgres", "workloom_app", "workloom_gateway"))}}


def guard(tmp_path, values):
    configured = os.environ.get("TIGER_TEST_NODE")
    assert configured and Path(configured).is_absolute() and Path(configured).is_file(), \
        "MCD must bind its actual Node executable through TIGER_TEST_NODE"
    # No credentials or unrelated environment reach the child process.
    if HELPER.is_file():
        program = ("const {assertFrontendFixtureDatabase} = await import(process.argv[1]);"
                   "const value = JSON.parse(process.argv[2]);"
                   "const before = JSON.stringify(value);"
                   "assertFrontendFixtureDatabase(Object.freeze(value));"
                   "if(JSON.stringify(value)!==before) throw new Error('mutation');")
        argv = [configured, "--input-type=module", "-e", program, HELPER.as_uri(), json.dumps(values)]
        environment = {"PATH": "/usr/bin:/bin"}
    else:
        # The old seven-line guard is checked against the actual old test body.
        text = PG_TEST.read_text()
        assert OLD_GUARD in text
        program = tmp_path / "exact-old-guard.ts"
        program.write_text(OLD_GUARD)
        argv = [configured, str(program)]
        environment = {"PATH": "/usr/bin:/bin", **values}
    return subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=10)


def bind(tmp_path, port):
    workflow = WORKFLOW.read_text()
    marker = "        run: |\n"
    start = workflow.index(marker, workflow.index("- name: 绑定 PostgreSQL 动态服务端口")) + len(marker)
    end = workflow.index("      - uses:", start)
    lines = workflow[start:end].splitlines()
    assert lines and all(not line or line.startswith("          ") for line in lines)
    script = "\n".join(line[10:] for line in lines) + "\n"
    output = tmp_path / "github-env"
    output.write_text("")
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script],
        env={"PGPORT": port, "GITHUB_ENV": str(output), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=10)
    rows = output.read_text().splitlines()
    assert len(rows) == len(set(row.split("=", 1)[0] for row in rows))
    return result, dict(row.split("=", 1) for row in rows)


@pytest.mark.parametrize("port", ["1", "5432", "49153", "55432", "65535"])
def test_dynamic_binding_and_actual_consumer_guard_agree(tmp_path, port):
    result, values = bind(tmp_path, port)
    assert result.returncode == 0, result.stderr
    assert set(values) == {"TIGER_TEST_DATABASE_PORT", *KEYS}
    assert values["TIGER_TEST_DATABASE_PORT"] == port
    checked = guard(tmp_path, values)
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize("port", ["5432", "49153", "65535"])
def test_actual_consumer_accepts_explicit_dynamic_port_even_without_shell(tmp_path, port):
    result = guard(tmp_path, fixture(port))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("port", ["", "0", "65536", "-1", "0001", "+49153", "49153\nX=injected", "1;false"])
def test_invalid_service_port_writes_no_partial_target(tmp_path, port):
    result, values = bind(tmp_path, port)
    assert result.returncode != 0
    assert values == {}


@pytest.mark.parametrize("case", ["remote", "wrong_port", "different_db", "query", "fragment",
    "encoded_owner", "app_owner", "missing_gateway", "mixed_hosts", "unsafe_db", "newline"])
def test_consumer_guard_rejects_unsafe_or_ambiguous_target(tmp_path, case):
    values = fixture()
    if case == "remote": values[KEYS[0]] = values[KEYS[0]].replace("127.0.0.1", "db.example.invalid")
    elif case == "wrong_port": values[KEYS[1]] = values[KEYS[1]].replace(":49153", ":49154")
    elif case == "different_db": values[KEYS[2]] += "_other"
    elif case == "query": values[KEYS[0]] += "?host=db.example.invalid"
    elif case == "fragment": values[KEYS[0]] += "#override"
    elif case == "encoded_owner": values[KEYS[0]] = values[KEYS[0]].replace("://postgres:", "://%77orkloom_app:")
    elif case == "app_owner": values[KEYS[1]] = values[KEYS[1]].replace("workloom_app:", "postgres:")
    elif case == "missing_gateway": del values[KEYS[2]]
    elif case == "mixed_hosts": values[KEYS[1]] = values[KEYS[1]].replace("127.0.0.1", "[::1]")
    elif case == "unsafe_db": values[KEYS[0]] = values[KEYS[0]].replace("workloom_tiger_fixture", "production")
    else: values[KEYS[0]] += "\n"
    before = deepcopy(values)
    result = guard(tmp_path, values)
    assert result.returncode != 0
    assert values == before


def test_undefined_port_keeps_local_default(tmp_path):
    values = fixture("55432")
    del values["TIGER_TEST_DATABASE_PORT"]
    result = guard(tmp_path, values)
    assert result.returncode == 0, result.stderr


def test_production_test_guard_precedes_pool_and_original_test_is_byte_preserved():
    text = PG_TEST.read_text()
    assert text.count(HELPER_IMPORT) == 1
    assert text.count("    assertFrontendFixtureDatabase();\n") == 1
    assert text.index("    assertFrontendFixtureDatabase();") < text.index("    db = new pg.Pool(")
    restored = text.replace(HELPER_IMPORT, "", 1).replace("    assertFrontendFixtureDatabase();\n", OLD_GUARD, 1)
    assert hashlib.sha256(restored.encode()).hexdigest() == OLD_PG_SHA256
    workflow = WORKFLOW.read_text()
    assert "PGPORT: ${{ job.services.postgres.ports[5432] }}" in workflow
    assert re.search(r"^          - 5432$", workflow, re.M)
    assert "55432:5432" not in workflow
