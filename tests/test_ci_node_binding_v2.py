"""The actual CI pytest invocation supplies its selected Node to consumers.

The isolated executable boundary checks environment propagation without
recursively starting pytest, downloading Node, or contacting a database.
"""
from pathlib import Path
import subprocess

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/workloom-ui-contract.yml"


@pytest.mark.parametrize("prior", [None, "/SYNTHETIC_TEST/stale-node"])
def test_actual_ci_pytest_command_binds_selected_node_before_consumer(tmp_path, prior):
    commands = [line.strip() for line in WORKFLOW.read_text().splitlines()
                if "python -m pytest -q" in line]
    assert len(commands) == 1
    executables = tmp_path / "executables with spaces"
    executables.mkdir()
    node = executables / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o700)
    python = executables / "python"
    python.write_text("#!/bin/sh\n"
        "[ \"$#\" = 4 ] && [ \"$1\" = -m ] && [ \"$2\" = pytest ] && [ \"$3\" = -q ] || exit 17\n"
        "[ \"${TIGER_TEST_NODE-}\" = \"$EXPECTED_NODE\" ] || exit 18\n"
        "[ \"$4\" = \"$EXPECTED_NODE\" ] || exit 19\n")
    python.chmod(0o700)
    environment = {"PATH": str(executables), "EXPECTED_NODE": str(node)}
    if prior is not None:
        environment["TIGER_TEST_NODE"] = prior
    # The extra argument is expanded by the same shell immediately before the
    # consumer starts, proving selection and exported child environment agree.
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c",
        commands[0] + ' "$(command -v node)"'], env=environment,
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stdout == result.stderr == ""
    assert environment.get("TIGER_TEST_NODE") == prior
