"""Packaging contract: PyPI name, import package, and both console scripts."""
import os
import re

import embers

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pyproject():
    with open(os.path.join(REPO, "pyproject.toml"), encoding="utf-8") as f:
        return f.read()


def test_distribution_name_and_version():
    text = _pyproject()
    assert re.search(r'^name = "embers-memory"$', text, re.M)
    assert re.search(r'^version = "%s"$' % re.escape(embers.__version__), text, re.M)
    assert embers.__version__ == "0.2.0"


def test_both_console_scripts_point_at_one_entry():
    text = _pyproject()
    assert re.search(r'^embers = "embers\.cli:main"$', text, re.M)
    assert re.search(r'^mem = "embers\.cli:main"$', text, re.M)
    assert 'packages = ["embers"]' in text
