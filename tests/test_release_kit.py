"""Release kit: setup.sh, the CI workflow, and the top-level docs the README promises."""
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = shutil.which("bash")


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_setup_sh_is_valid_bash_and_dry_run_prints_quickstart(tmp_path):
    script = os.path.join(REPO, "setup.sh")
    assert subprocess.run([BASH, "-n", script]).returncode == 0
    env = dict(os.environ, HOME=str(tmp_path), PATH=os.environ["PATH"])
    r = subprocess.run([BASH, script, "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    for cmd in ("embers detect", "embers ingest", 'embers search "phrase"'):
        assert cmd in r.stdout
    assert not (tmp_path / ".local").exists(), "--dry-run must not change anything"


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_setup_sh_rejects_unknown_option():
    r = subprocess.run([BASH, os.path.join(REPO, "setup.sh"), "--nope"], capture_output=True, text=True)
    assert r.returncode == 2


def test_ci_workflow_runs_pytest_on_39_and_312_for_push_and_pr():
    wf = _read(".github/workflows/tests.yml")
    assert "push:" in wf and "pull_request:" in wf
    assert '"3.9"' in wf and '"3.12"' in wf
    assert "pytest" in wf


def test_readme_opening_line_and_credits():
    readme = _read("README.md")
    assert readme.splitlines()[0] == "Embers: lossless memory for Claude Code."
    assert "Built by Holly Wren von Heidereich with Ember (Claude)." in readme
    assert "pipx install embers-memory" in readme
    assert "Adapters for Hermes, Codex CLI, and Gemini CLI are in progress." in readme


def test_license_is_mit_with_owner_and_year():
    lic = _read("LICENSE")
    assert lic.startswith("MIT License") and "Copyright (c) 2026 Holly Wren von Heidereich" in lic


def test_contributing_and_security_exist():
    assert "fixtures only" in _read("CONTRIBUTING.md").lower()
    assert "Never paste transcripts" in _read("SECURITY.md")


def test_agents_md_is_short_and_points_at_the_skill():
    text = _read("AGENTS.md")
    assert len(text.splitlines()) <= 20
    assert "embers install-skill" in text and "Fixtures only" in text
