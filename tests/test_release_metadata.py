import json
import tomllib
from pathlib import Path

from app.main import app
from app.version import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_public_release_versions_are_aligned() -> None:
    assert __version__ == "1.4.0"
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
    assert f"image: ${{PARSEFLOW_IMAGE:-parseflow:{__version__}}}" in compose
    assert f'name = "parse-agent"\nversion = "{__version__}"' in lock


def test_release_pipeline_validates_and_signs_the_published_digest() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    publish_job = workflow.split("  publish-image:", maxsplit=1)[1]

    assert 'PARSEFLOW_RELEASE_REF=$PARSEFLOW_IMAGE@$digest' in publish_job
    assert 'id: build_release' in publish_job
    assert "PARSEFLOW_IMAGE: ${{ steps.build_release.outputs.release_ref }}" in publish_job
    assert "PARSEFLOW_SMOKE_SKIP_BUILD: \"true\"" in publish_job
    assert "image-ref: ${{ steps.build_release.outputs.release_ref }}" in publish_job
    assert 'test "$observed_digest" = "$PARSEFLOW_DIGEST"' in publish_job
    assert 'cosign sign --yes "$PARSEFLOW_RELEASE_REF"' in publish_job
    assert "cosign verify" in publish_job
    assert publish_job.count('"$PARSEFLOW_RELEASE_REF"') >= 2


def test_python_package_declares_build_backend_and_console_script() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["build-system"]["build-backend"] == "setuptools.build_meta"
    assert project["project"]["scripts"]["parse-agent-mcp"] == "app.mcp_server:main"
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
