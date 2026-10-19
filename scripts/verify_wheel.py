"""Build and verify installed wheels on 3.12/3.13, using only populated uv caches.

Run after uv sync --all-groups. A missing interpreter/wheel cache fails setup;
this script never downloads a browser or enables test networking.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from tarfile import open as open_tar
from zipfile import ZipFile

REPO = Path(__file__).resolve().parents[1]
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")


def collected_count(output: str) -> int:
    match = re.search(r"(\d+) tests? collected", output)
    if match is None:
        raise RuntimeError(f"Missing pytest collection count:\n{output}")
    return int(match[1])


def run(command: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180, check=False
    )
    if result.returncode:
        raise RuntimeError(f"Command failed: {command!r}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def verify(root: Path) -> None:
    setup_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    expected_tests = collected_count(
        run(
            [str(REPO / ".venv/bin/python"), "-m", "pytest", "--collect-only", "-q"],
            REPO,
            setup_env,
        )
    )
    run([UV, "build", "--offline"], REPO, setup_env)
    requirements = root / "requirements.txt"
    run(
        [
            UV,
            "export",
            "--frozen",
            "--offline",
            "--all-groups",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(requirements),
        ],
        REPO,
        setup_env,
    )
    wheel = REPO / "dist/hevy_mcp-0.1.0-py3-none-any.whl"
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        expected_modules = {
            path.relative_to(REPO / "src").as_posix()
            for path in (REPO / "src/hevy_mcp").rglob("*.py")
        }
        expected_data = {
            path.relative_to(REPO / "src").as_posix()
            for path in (REPO / "src/hevy_mcp/data").iterdir()
            if path.is_file()
        }
        expected_docs = {"hevy_mcp_docs/README.md": REPO / "README.md"}
        expected_docs.update(
            {
                "hevy_mcp_docs/" + path.relative_to(REPO).as_posix(): path
                for path in (REPO / "docs").rglob("*")
                if path.is_file()
            }
        )
        assert {
            name for name in names if name.startswith("hevy_mcp/")
        } == expected_modules | expected_data
        assert {name for name in names if name.startswith("hevy_mcp_docs/")} == set(expected_docs)
        for name in expected_data:
            assert archive.read(name) == (REPO / "src" / name).read_bytes()
        for name, path in expected_docs.items():
            assert archive.read(name) == path.read_bytes()
        forbidden = (
            ".refs",
            "credentials",
            "__pycache__",
            ".env",
            "deleted-workouts",
            "updated-workouts",
            "snapshots",
            ".har",
            ".venv",
            ".log",
            "token",
        )
        assert not any(part in name.lower() for name in names for part in forbidden)
        assert not any(name.lower().endswith(".js") for name in names)
        assert b"Requires-Python: >=3.12" in archive.read("hevy_mcp-0.1.0.dist-info/METADATA")
        wheel_names = names
        wheel_hashes = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names}
    with open_tar(REPO / "dist/hevy_mcp-0.1.0.tar.gz") as archive:
        names = archive.getnames()
        assert not any(part in name.lower() for name in names for part in forbidden)
        assert not any(name.lower().endswith(".js") for name in names)
        prefix = "hevy_mcp-0.1.0/"
        for path in [
            REPO / "README.md",
            *expected_docs.values(),
            *(REPO / "src" / name for name in expected_data),
        ]:
            member = archive.extractfile(prefix + path.relative_to(REPO).as_posix())
            assert member is not None and member.read() == path.read_bytes()
        assert prefix + "scripts/smoke_test.py" in names
        sdist_names = names
    manifest = REPO / "dist/artifact-contents.json"
    manifest.write_text(
        json.dumps(
            {"wheel": wheel_names, "sdist": sdist_names, "wheel_sha256": wheel_hashes}, indent=2
        )
        + "\n"
    )
    print(
        f"Wheel/sdist contents verified: {len(wheel_names)}/{len(sdist_names)} entries; {manifest}",
        flush=True,
    )

    for version in ("3.12", "3.13"):
        venv = root / f"py{version}"
        run([UV, "venv", "--offline", "--python", version, str(venv)], root, setup_env)
        python = str(venv / "bin/python")
        run(
            [
                UV,
                "pip",
                "install",
                "--offline",
                "--python",
                python,
                "-r",
                str(requirements),
                str(wheel),
            ],
            root,
            setup_env,
        )
        home = root / f"home-{version}"
        home.mkdir()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HEVY_CONFIG_DIR": str(root / f"config-{version}"),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "xdg"),
            "LANG": "C.UTF-8",
            "PLAYWRIGHT_BROWSERS_PATH": str(root / "missing-browser"),
        }
        code = """
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import sys
from pytest_socket import disable_socket
disable_socket(allow_unix_socket=True)
import hevy_mcp
import hevy_mcp.auth, hevy_mcp.config, hevy_mcp.errors, hevy_mcp.logging
import hevy_mcp.results, hevy_mcp.runtime, hevy_mcp.server, hevy_mcp.cli
import hevy_mcp.models, hevy_mcp.service, hevy_mcp.deletion
import hevy_mcp.delete_client, hevy_mcp.raw_client
from hevy_mcp.catalog import Catalog
import fastmcp, mcp, hevy_unofficial, playwright
path = Path(hevy_mcp.__file__).resolve()
assert 'site-packages' in path.parts, path
assert path.is_relative_to(Path(sys.prefix).resolve()), path
assert not Path(os.environ['HEVY_CONFIG_DIR']).exists()
assert metadata.version('mcp') == '2.3.0'
assert metadata.version('fastmcp') == '4.1.0'
assert Catalog().search(limit=1)['catalog']['source_count'] == 451
import asyncio
async def release_probe():
    server = hevy_mcp.server.create_server()
    assert len(await server.list_tools()) == 12
    result = await server.call_tool('search_exercise_templates', {'limit': 1})
    assert not result.is_error and result.structured_content['returned_count'] == 1
    resource = await server.read_resource('hevy://templates')
    assert json.loads(resource.contents[0].content)['catalog']['source_count'] == 451
asyncio.run(release_probe())
print(json.dumps({'python': sys.version.split()[0], 'package': str(path),
    **{name: metadata.version(name) for name in
       ('fastmcp', 'mcp', 'hevy-unofficial', 'playwright', 'typer', 'click')}}))
"""
        print(run([python, "-c", code], root, env).strip(), flush=True)
        bootstrap = str(REPO / "tests/child_bootstrap.py")
        for mode, target in (("module", "hevy_mcp.cli"), ("path", str(venv / "bin/hevy-mcp"))):
            for arguments in (
                ["--help"],
                ["serve", "--log-level", "DEBUG"],
                ["auth", "login", "--help"],
                ["doctor", "--help"],
                ["test-delete-backend", "--help"],
            ):
                output = run([python, bootstrap, mode, target, *arguments], root, env)
                assert output == "" if arguments[0] == "serve" else "--help" in output
        output = run(
            [python, "-m", "pytest", "-c", str(REPO / "pyproject.toml"), str(REPO / "tests"), "-q"],
            root,
            env,
        )
        passed = re.search(r"(\d+) passed", output)
        assert passed is not None and int(passed[1]) == expected_tests, output
        print(output.strip(), flush=True)
    print("Installed-wheel matrix passed", flush=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="hevy-wheel-") as temporary:
        verify(Path(temporary))
