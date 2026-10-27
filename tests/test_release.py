"""Install-package checks in a fresh process with no external sockets or real data."""

import ast
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version


def test_release_archive_metadata_and_fresh_import(tmp_path):
    root = Path(__file__).resolve().parents[1]
    archive = tmp_path / "plugin.zip"
    subprocess.run(
        [sys.executable, str(root / "scripts/build_release.py"), "--output", str(archive)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None and archive.stat().st_size < 16 * 1024 * 1024
        names = z.namelist()
        assert not any(
            part in name
            for name in names
            for part in (
                ".dev/",
                "tests/",
                "session.json",
                "identity.json",
                "__pycache__",
                ".env",
                "users-v1/",
            )
        )
        meta = yaml.safe_load(z.read("astrbot_plugin_bilibili_shixun/metadata.yaml"))
        assert meta["name"] == "astrbot_plugin_bilibili_shixun" and meta["author"] == "yuease"
        assert meta["repo"] == "https://github.com/yuease/astrbot_plugin_bilibili_shixun"
        assert set(meta["support_platforms"]) == {"aiocqhttp", "qq_official", "qq_official_webhook"}
        assert Version("4.29.1") in SpecifierSet(meta["astrbot_version"])
        for name in names:
            if name.endswith(".py"):
                ast.parse(z.read(name).decode("utf-8"), filename=name)
        destination = tmp_path / "extracted"
        z.extractall(destination)
    env = dict(
        os.environ,
        ASTRBOT_ROOT=str(tmp_path / "host-data"),
        ASTRBOT_TEST_MODE="true",
        TESTING="true",
    )
    script = """
import socket, sys
def denied(*args, **kwargs):
    raise AssertionError('No external sockets during release import')
socket.socket.connect = denied
socket.socket.connect_ex = denied
sys.path.insert(0, sys.argv[1])
from astrbot_plugin_bilibili_shixun import main
from astrbot_plugin_bilibili_shixun.services.contracts import TOOL_NAMES
assert len(TOOL_NAMES) == 10
assert main.BilibiliShixunPlugin.name == 'astrbot_plugin_bilibili_shixun'
print('isolated-release-import-ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(destination)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "isolated-release-import-ok" in result.stdout
