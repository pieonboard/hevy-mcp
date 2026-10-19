"""Explicit network-denied child entry; this is never part of production code."""

import os
import runpy
import sys
from pathlib import Path

from pytest_socket import disable_socket

assert os.environ.get("HEVY_CONFIG_DIR"), "child requires an isolated credential root"
assert "PYTHONPATH" not in os.environ, "child must not inherit Python import paths"
disable_socket(allow_unix_socket=True)
mode, target, *arguments = sys.argv[1:]
sys.argv = [target, *arguments]
if mode == "code":
    exec(compile(target, "<offline-child>", "exec"), {"__name__": "__main__"})
elif mode == "module":
    runpy.run_module(target, run_name="__main__", alter_sys=True)
elif mode == "path":
    # Console entry/wheel checks use the installed distribution from outside the repository.
    runpy.run_path(str(Path(target).resolve()), run_name="__main__")
else:
    raise ValueError("unknown bootstrap mode")
