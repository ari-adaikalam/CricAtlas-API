"""The API owns its data paths: the server and the direct exporter default to api/data, production keeps its explicit /data, and nothing in the runtime code names the dataset pipeline's folders."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cricket_api import export, settings

API = Path(__file__).resolve().parents[1]


def test_the_server_defaults_to_the_api_owned_export(monkeypatch):
    monkeypatch.delenv("CRICKET_API_DATA", raising=False)
    assert settings.Settings().data_dir == Path("api/data/exports/current")
    monkeypatch.setenv("CRICKET_API_DATA", "/data")
    assert settings.Settings().data_dir == Path("/data")


def test_the_direct_exporter_defaults_to_api_owned_folders(monkeypatch):
    monkeypatch.delenv("CRICKET_API_DATA_ROOT", raising=False)
    assert export.default_data_dir() == Path("api/data")
    out = subprocess.run([sys.executable, "-m", "cricket_api.export", "--help"], cwd=API.parent, env={"PYTHONPATH": str(API), "PATH": ""}, capture_output=True, text=True).stdout
    assert "api/data/csv" in out and "api/data/exports/manual" in out and "data/output" not in out


def test_production_compose_keeps_its_explicit_data_setting():
    if not (API / "deploy/compose.yaml").exists():
        pytest.skip("the compose file is not part of the public repository")
    assert re.search(r"CRICKET_API_DATA:\s*/data\b", (API / "deploy/compose.yaml").read_text())


def test_no_runtime_code_names_the_dataset_pipelines_folders():
    for path in (API / "cricket_api").rglob("*.py"):
        text = re.sub(r"#.*", "", path.read_text())
        assert not re.search(r"""["'](\.\./)*data/(output|api|players|work|cricsheet)\b""", text), f"{path.name} names a root data folder"
