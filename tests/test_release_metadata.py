import json
import tomllib
from pathlib import Path

from app.main import app
from app.version import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_public_release_versions_are_aligned() -> None:
    assert __version__ == "0.7.1"
    assert app.version == __version__

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    web = json.loads((ROOT / "web/package.json").read_text(encoding="utf-8"))
    web_lock = json.loads((ROOT / "web/package-lock.json").read_text(encoding="utf-8"))
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert project["project"]["version"] == __version__
    assert web["version"] == __version__
    assert web_lock["version"] == __version__
    assert web_lock["packages"][""]["version"] == __version__
    assert f"image: parseflow:{__version__}" in compose
    assert f'name = "parse-agent"\nversion = "{__version__}"' in lock


def test_python_package_declares_build_backend_and_console_script() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["build-system"]["build-backend"] == "setuptools.build_meta"
    assert project["project"]["scripts"]["parse-agent-mcp"] == "app.mcp_server:main"
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
