"""Internal execution switches are code constants, not deployment config.

These values used to be environment flags.  They are implementation details or
security invariants; exposing them again would recreate the configuration
sprawl this test is meant to prevent.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

INTERNAL_CONSTANTS = {
    "MULTI_KB_REVIEW_REQUIRE_APPROVAL_FOR_CRITICAL": True,
    "SANDBOX_DIAGNOSTIC_TEST_ENABLED": False,
    "SHARED_RETRIEVAL_EXECUTOR_ENABLED": True,
    "MULTI_KB_EXPLICIT_FIELDS_ENABLED": False,
    "REVIEWER_AGENT_ENABLED": False,
    "RESEARCH_AGENT_ENABLED": False,
    "CONVERSATION_UNIFIED_TOOLS_ENABLED": True,
}

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every remaining env-backed boolean must be deliberate.  Adding a new
# _env_bool(...) without registering it here fails the guard test below.
ALLOWED_ENV_BOOL_FLAGS = {
    "ADMISSION_CONTROL_ENABLED",
    "DISTRIBUTED_ADMISSION_ENABLED",
    "EVIDENCE_SOURCE_AWARE_ENABLED",
    "MCP_ALLOW_INSECURE_HTTP",
    "MCP_ALLOW_PRIVATE_HOSTS",
    "MCP_ENABLED",
    "MCP_WRITE_ENABLED",
    "MULTI_KB_CHAT_ENABLED",
    "MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED",
    "MULTI_KB_HUMAN_REVIEW_ENABLED",
    "REDIS_REQUIRED",
    "RESOURCE_GOVERNOR_ENABLED",
    "RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED",
    "RESULT_EXPORT_DOCX_ENABLED",
    "RESULT_EXPORT_ENABLED",
    "RESULT_EXPORT_MD_ENABLED",
    "RESULT_EXPORT_ORGANIZE_ENABLED",
    "RESULT_EXPORT_PDF_ENABLED",
    "RESULT_EXPORT_PPTX_ENABLED",
    "RESULT_EXPORT_XLSX_ENABLED",
    "SANDBOX_ALLOW_DYNAMIC_INSTALL",
    "SANDBOX_ENABLED",
    "SANDBOX_GATEWAY_ENABLED",
    "SANDBOX_REQUIRE_RUNTIME_ATTESTATION",
    "SKILLS_ENABLED",
}


def test_only_registered_env_bool_flags_are_used():
    text = (REPO_ROOT / "src/settings.py").read_text(encoding="utf-8")
    found = set(re.findall(r'_env_bool\(\s*"([A-Z0-9_]+)"', text))
    if "_EVIDENCE_BOOL_KEYS" in text:
        found.add("EVIDENCE_SOURCE_AWARE_ENABLED")
    assert found == ALLOWED_ENV_BOOL_FLAGS


def _conflicting_env_value(expected: object) -> str:
    if expected is True:
        return "false"
    if expected is False:
        return "true"
    if expected == "off":
        return "enabled"
    return "tenant-a"


def test_internal_constants_ignore_environment_and_default_values():
    env = os.environ.copy()
    env["PYTHON_DOTENV_DISABLED"] = "1"
    for key, expected in INTERNAL_CONSTANTS.items():
        env[key] = _conflicting_env_value(expected)

    assertions = " ".join(
        f"assert s.{key} == {expected!r};"
        for key, expected in INTERNAL_CONSTANTS.items()
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import src.settings as s; "
                f"{assertions} "
                "assert not any(k in s.DEFAULT_VALUES for k in "
                f"{tuple(INTERNAL_CONSTANTS)!r})"
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_turn_meta_behavior_switches_ignore_environment():
    env = os.environ.copy()
    env["PYTHON_DOTENV_DISABLED"] = "1"
    for key in (
        "AGENT_COMPLEXITY_BUDGET_ENABLED",
        "AGENT_PARTIAL_SYNTHESIS_ENABLED",
        "AGENT_VERIFIER_ENABLED",
        "AGENT_RESEARCH_SUBAGENT_ENABLED",
    ):
        env[key] = "false"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import src.agents.turn_meta as t; "
                "assert t.COMPLEXITY_BUDGET_ENABLED is True; "
                "assert t.PARTIAL_SYNTHESIS_ENABLED is True; "
                "assert t.VERIFIER_ENABLED is True; "
                "assert t.RESEARCH_SUBAGENT_ENABLED is True"
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_obsolete_env_keys_cover_internal_constants_and_ignore_live_flags(tmp_path):
    import src.settings as settings

    assert set(INTERNAL_CONSTANTS) <= set(settings.OBSOLETE_ENV_KEYS)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SHARED_RETRIEVAL_EXECUTOR_ENABLED=true\nMULTI_KB_CHAT_ENABLED=true\n",
        encoding="utf-8",
    )
    obsolete = settings.obsolete_env_keys(str(env_file))
    assert "SHARED_RETRIEVAL_EXECUTOR_ENABLED" in obsolete
    assert "MULTI_KB_CHAT_ENABLED" not in obsolete


def test_internal_constants_are_absent_from_env_example():
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    configured = {
        line.split("=", 1)[0].strip().lstrip("#").strip()
        for line in lines
        if "=" in line
    }
    import src.settings as settings

    assert INTERNAL_CONSTANTS.keys().isdisjoint(configured)
    assert settings.OBSOLETE_ENV_KEYS.keys().isdisjoint(configured)
    assert {
        "AGENT_COMPLEXITY_BUDGET_ENABLED",
        "AGENT_PARTIAL_SYNTHESIS_ENABLED",
        "AGENT_VERIFIER_ENABLED",
        "AGENT_RESEARCH_SUBAGENT_ENABLED",
    }.isdisjoint(configured)
