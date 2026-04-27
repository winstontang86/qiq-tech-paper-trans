"""pack.py —— 把整个 skill 目录打成 zip，便于分发。

用法：
  python3 scripts/pack.py --out ~/Downloads/

会排除：__pycache__、.DS_Store、assets/samples/ 大文件、zh_per_segment/ 等工作产物。
"""
from __future__ import annotations

import os
import sys
import argparse
import zipfile
from pathlib import Path


SKILL_ROOT = Path(__file__).parent.parent
SKILL_NAME = "qiq-tech-paper-trans"


EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv", "dist",
                "zh_per_segment", "prompts_per_segment"}
EXCLUDE_FILES = {".DS_Store", "translated_raw.md", "masked.md",
                 "segments.json", "locked_blocks.json", "INDEX.md"}
EXCLUDE_SUFFIXES = {".pyc"}


def _read_version() -> str:
    skill_md = SKILL_ROOT / "SKILL.md"
    if skill_md.exists():
        for line in skill_md.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("version:"):
                return s.split(":", 1)[1].strip()
    return "0.0.0"


def should_skip(path: Path, base: Path) -> bool:
    rel = path.relative_to(base)
    for part in rel.parts:
        if part in EXCLUDE_DIRS:
            return True
    if path.name in EXCLUDE_FILES:
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return False


def pack(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    version = _read_version()
    zip_name = f"{SKILL_NAME}-v{version}.zip"
    zip_path = out_dir / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(SKILL_ROOT):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                fp = Path(root) / f
                if should_skip(fp, SKILL_ROOT):
                    continue
            # In-zip path: qiq-tech-paper-trans/<relative>
                rel = fp.relative_to(SKILL_ROOT)
                arcname = f"{SKILL_NAME}/{rel.as_posix()}"
                zf.write(fp, arcname)
    return zip_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / "Downloads"),
                    help="Output directory for the zip")
    args = ap.parse_args()
    p = pack(Path(args.out))
    print(f"Packed: {p}")
    print(f"Size: {p.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
