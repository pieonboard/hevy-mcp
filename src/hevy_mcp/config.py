from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from pydantic import BaseModel

from .errors import ConfigurationError


def config_dir() -> Path:
    override = os.environ.get("HEVY_CONFIG_DIR")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "hevy-mcp"


def secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def atomic_write(path: Path, data: str, mode: int = 0o600, exclusive: bool = False) -> None:
    secure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            os.link(tmp, path)
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
        os.chmod(path, mode)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


class Settings(BaseModel):
    workout_delete_backend: str | None = None


class SettingsStore:
    def __init__(self, root: Path | None = None):
        self.root = root or config_dir()
        self.path = self.root / "config.yaml"

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
            return Settings.model_validate(data.get("hevy", data))
        except Exception as exc:
            raise ConfigurationError(f"Invalid config: {exc}") from exc

    def set_delete_backend(self, value: str) -> None:
        if value not in {"internal", "v1", "v2"}:
            raise ConfigurationError("backend must be internal, v1, or v2")
        atomic_write(
            self.path, yaml.safe_dump({"hevy": {"workout_delete_backend": value}}, sort_keys=True)
        )
