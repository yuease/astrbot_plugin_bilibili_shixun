"""Build a source-only archive without importing or running the plugin."""

import argparse
import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = (
    "__init__.py",
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "SOURCE.md",
    "PRIVACY.md",
    "CHANGELOG.md",
    "ruff.toml",
    ".gitignore",
)
FOLDERS = ("core", "services", "views", ".astrbot-plugin", "docs", "scripts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "dist/astrbot_plugin_bilibili_shixun-v0.9.1.zip"
    )
    output = parser.parse_args().output.resolve()
    files = [ROOT / name for name in TOP_LEVEL]
    for folder in FOLDERS:
        files.extend(
            path
            for path in (ROOT / folder).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
            and not (folder == "docs" and path.name.startswith("investigation-"))
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(set(files)):
            entry = zipfile.ZipInfo(
                (Path(ROOT.name) / path.relative_to(ROOT)).as_posix(),
                date_time=(2026, 10, 27, 0, 0, 0),
            )
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    if output.stat().st_size > 16 * 1024 * 1024:
        raise RuntimeError("安装包超过 AstrBot 插件市场16MB上限")
    print(f"Source archive: {output}")
    print(f"Files: {len(files)}; SHA-256: {hashlib.sha256(output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
