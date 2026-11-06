"""The built package carries every file the documentation serves. The Docker image copies the folder as it is, so only an install from a built wheel could miss one."""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[1]
ASSETS = ("docs.css", "brand-mark.svg", "icon-32.png", "apple-touch-icon.png", "outfit-latin.woff2", "OFL-Outfit.txt", "og-api-docs.png")


def test_every_documentation_asset_is_in_a_built_wheel(tmp_path):
    pytest.importorskip("setuptools")
    source = tmp_path / "src"
    source.mkdir()
    shutil.copy(API / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(API / "cricket_api", source / "cricket_api", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    out = tmp_path / "out"
    built = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "--no-cache-dir", "-q", "-w", str(out), str(source)], capture_output=True, text=True, timeout=300)
    assert built.returncode == 0, built.stderr[-800:]
    wheel = next(out.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    for asset in ASSETS:
        assert f"cricket_api/docsite/assets/{asset}" in names, f"{asset} is not in the wheel"
    on_disk = {p.name for p in (API / "cricket_api" / "docsite" / "assets").iterdir() if p.suffix in (".css", ".svg", ".png", ".woff2", ".txt")}
    in_wheel = {n.rsplit("/", 1)[1] for n in names if n.startswith("cricket_api/docsite/assets/")}
    assert on_disk == in_wheel, "every asset file on disk is packaged, and nothing else is"


def test_package_data_names_the_stylesheet_the_svg_the_pngs_the_font_and_its_licence():
    text = (API / "pyproject.toml").read_text()
    assert '"cricket_api.docsite" = ["assets/*.css", "assets/*.svg", "assets/*.png", "assets/*.woff2", "assets/OFL-Outfit.txt"]' in text
