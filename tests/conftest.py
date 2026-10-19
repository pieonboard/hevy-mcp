"""Install isolation before test collection imports any application code."""

import os
import tempfile
from pathlib import Path

import pytest
from pytest_socket import disable_socket

_COLLECTION_ROOT = tempfile.TemporaryDirectory(prefix="hevy-tests-")
os.environ["HEVY_CONFIG_DIR"] = str(Path(_COLLECTION_ROOT.name) / "collection")
os.environ["HOME"] = _COLLECTION_ROOT.name
os.environ["XDG_CONFIG_HOME"] = str(Path(_COLLECTION_ROOT.name) / "xdg")
os.environ.pop("PYTHONPATH", None)
# Rich styles CLI help when it detects a colour-capable environment, which a CI
# runner is. It renders a styled option as separate escape-wrapped fragments, so
# "--live" never appears as plain text and the help assertions fail. A dumb
# terminal keeps the help plain, and a pinned width keeps the layout stable.
os.environ["NO_COLOR"] = "1"
os.environ.pop("FORCE_COLOR", None)
os.environ["TERM"] = "dumb"
os.environ["COLUMNS"] = "200"
os.environ["LINES"] = "50"
disable_socket(allow_unix_socket=True)


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HEVY_CONFIG_DIR", str(tmp_path / "hevy"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def child_env():
    """Do not forward inherited tokens, Python paths or home credential stores."""
    return {
        key: os.environ[key]
        for key in ("PATH", "HEVY_CONFIG_DIR", "HOME", "XDG_CONFIG_HOME", "LANG")
        if key in os.environ
    }


@pytest.fixture
def child_command():
    import sys

    bootstrap = Path(__file__).with_name("child_bootstrap.py")
    return lambda code, *args: [sys.executable, str(bootstrap), "code", code, *map(str, args)]
