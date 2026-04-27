"""fetch.py —— 处理 URL 输入。

支持：
- arXiv 链接：优先尝试 HTML 版（ar5iv / arxiv.org/html/<id>），失败回退 PDF。
- 普通 PDF 直链：下载到本地。
- 普通 HTML 页面：下载 HTML 源文件。

输入为本地路径时直接返回，不做网络操作。
"""
from __future__ import annotations

import os
import re
import sys
import argparse
from pathlib import Path
from typing import Tuple
from urllib.parse import urlparse


ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def _sanitize_filename(s: str) -> str:
    s = re.sub(r"[^\w.\-]+", "_", s)
    return s.strip("_") or "download"


def _download(url: str, dst: Path, timeout: int = 60) -> bool:
    """Download URL to dst. Returns True on success."""
    import requests
    try:
        r = requests.get(url, timeout=timeout, stream=True,
                         headers={"User-Agent": "QiQ-tech-paper-trans/0.1"})
        r.raise_for_status()
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"[fetch] download failed: {url} -> {e}", file=sys.stderr)
        return False


def _detect_arxiv_id(url: str) -> str | None:
    m = ARXIV_ID_RE.search(url)
    return m.group(1) if m else None


def fetch(input_str: str, outdir: Path) -> Tuple[Path, str]:
    """Resolve input to a local file.

    Returns (local_path, kind) where kind in {"pdf", "html", "arxiv_html"}.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # Local file
    p = Path(input_str)
    if p.exists():
        suffix = p.suffix.lower()
        if suffix == ".pdf":
            return p.resolve(), "pdf"
        if suffix in {".html", ".htm"}:
            return p.resolve(), "html"
        if suffix in {".md", ".markdown"}:
            return p.resolve(), "markdown"
        raise ValueError(f"Unsupported local file type: {suffix}")

    # URL
    parsed = urlparse(input_str)
    if not parsed.scheme:
        raise FileNotFoundError(f"Not a URL nor existing path: {input_str}")

    # arXiv special handling
    arxiv_id = _detect_arxiv_id(input_str) if "arxiv.org" in parsed.netloc else None
    if arxiv_id:
        stem = _sanitize_filename(f"arxiv_{arxiv_id}")
        # Try HTML first: arxiv.org/html/<id> then ar5iv
        candidates = [
            f"https://arxiv.org/html/{arxiv_id}",
            f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}",
        ]
        for c in candidates:
            dst = outdir / f"{stem}.html"
            if _download(c, dst):
                if dst.stat().st_size > 2048:  # sanity: not a tiny 404 page
                    return dst.resolve(), "arxiv_html"
                dst.unlink(missing_ok=True)
        # Fallback PDF
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        dst = outdir / f"{stem}.pdf"
        if _download(pdf_url, dst):
            return dst.resolve(), "pdf"
        raise RuntimeError(f"Failed to download arXiv {arxiv_id}")

    # Generic URL: judge by suffix first
    suffix = Path(parsed.path).suffix.lower()
    stem = _sanitize_filename(Path(parsed.path).stem or parsed.netloc)

    if suffix == ".pdf":
        dst = outdir / f"{stem}.pdf"
        if _download(input_str, dst):
            return dst.resolve(), "pdf"
        raise RuntimeError(f"Failed to download: {input_str}")

    # Assume HTML
    dst = outdir / f"{stem}.html"
    if _download(input_str, dst):
        return dst.resolve(), "html"
    raise RuntimeError(f"Failed to download: {input_str}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()
    path, kind = fetch(args.input, Path(args.outdir))
    print(f"{kind}\t{path}")


if __name__ == "__main__":
    main()
