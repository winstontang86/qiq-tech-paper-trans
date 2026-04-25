"""preprocess.py —— 将 PDF / HTML / Markdown 转换为结构化 Markdown。

策略：
- PDF：优先使用 marker-pdf（版面还原、公式 LaTeX、图片抽取都好）。
  回退：pymupdf + pdfplumber 的轻量提取（质量弱）。
- HTML：markdownify + BeautifulSoup 清洗后转 Markdown。
- Markdown：直接返回原文。

输出：
- <outdir>/source.md        —— 结构化原文 Markdown
- <outdir>/assets/          —— 抽取的图片（如有）
"""
from __future__ import annotations

import os
import re
import sys
import argparse
import shutil
from pathlib import Path


def _dehyphenate(text: str) -> str:
    """修复跨行连字符：'hyphen-\nation' -> 'hyphenation'。保留真正的复合词连字符。"""
    # 仅处理 "word-\nword" 这类跨行形式
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # 多余的行首行尾空白
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text


def _strip_repeating_headers(md: str) -> str:
    """尝试剥离页眉页脚：跨多页重复出现的短行。"""
    lines = md.split("\n")
    # 统计短行（<= 60 字符，非空）的出现次数
    from collections import Counter
    short_lines = [ln.strip() for ln in lines if 0 < len(ln.strip()) <= 60]
    cnt = Counter(short_lines)
    # 出现 >= 3 次且不像正文（不以标点结尾、不是标题）的，判为页眉页脚
    repeats = {
        s for s, c in cnt.items()
        if c >= 3
        and not s.endswith((".", "。", "!", "?", "？", "！", ":", "："))
        and not s.startswith("#")
        and not re.match(r"^\d+\.\s", s)
    }
    if not repeats:
        return md
    out = [ln for ln in lines if ln.strip() not in repeats]
    return "\n".join(out)


def preprocess_pdf_marker(pdf_path: Path, outdir: Path) -> Path:
    """Use marker-pdf to convert PDF to Markdown."""
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.output import text_from_rendered
    except ImportError as e:
        raise ImportError(
            "marker-pdf not installed. Run: pip install marker-pdf"
        ) from e

    converter = PdfConverter(artifact_dict=create_model_dict())
    rendered = converter(str(pdf_path))
    text, _, images = text_from_rendered(rendered)

    # Save images
    assets_dir = outdir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(images, dict):
        for name, img in images.items():
            img_path = assets_dir / name
            img_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if hasattr(img, "save"):
                    img.save(img_path)
                elif isinstance(img, (bytes, bytearray)):
                    img_path.write_bytes(bytes(img))
            except Exception as e:
                print(f"[preprocess] save image failed: {name} -> {e}", file=sys.stderr)

    text = _dehyphenate(text)
    text = _strip_repeating_headers(text)

    md_path = outdir / "source.md"
    md_path.write_text(text, encoding="utf-8")
    return md_path


def preprocess_pdf_fallback(pdf_path: Path, outdir: Path) -> Path:
    """Fallback: pymupdf + pdfplumber. Structure is weaker; formulas/tables may break."""
    try:
        import fitz  # pymupdf
    except ImportError as e:
        raise ImportError("pymupdf not installed. Run: pip install pymupdf") from e

    assets_dir = outdir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    chunks = []
    for i, page in enumerate(doc):
        text = page.get_text("text")
        chunks.append(text)
        # Extract images
        for img_idx, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha >= 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_path = assets_dir / f"page{i + 1}_img{img_idx + 1}.png"
                pix.save(str(img_path))
                chunks.append(f"\n![figure](assets/{img_path.name})\n")
                pix = None
            except Exception:
                pass
    doc.close()

    text = "\n\n".join(chunks)
    text = _dehyphenate(text)
    text = _strip_repeating_headers(text)

    md_path = outdir / "source.md"
    md_path.write_text(text, encoding="utf-8")
    return md_path


def preprocess_html(html_path: Path, outdir: Path) -> Path:
    """HTML -> Markdown via BeautifulSoup + markdownify."""
    from bs4 import BeautifulSoup
    try:
        from markdownify import markdownify as md_convert
    except ImportError as e:
        raise ImportError("markdownify not installed. Run: pip install markdownify") from e

    html = html_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")

    # Remove non-content tags
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()

    # arXiv/ar5iv: main content usually in <article> or <main>
    main = soup.find("article") or soup.find("main") or soup.body or soup
    md = md_convert(
        str(main),
        heading_style="ATX",
        bullets="-",
    )
    md = _dehyphenate(md)

    # Save images referenced (best-effort: we do NOT download remote images in v1;
    # keep URLs as-is so LLM sees image anchors and postprocess preserves them)
    md_path = outdir / "source.md"
    md_path.write_text(md, encoding="utf-8")
    return md_path


def preprocess(input_path: Path, kind: str, outdir: Path) -> Path:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if kind == "markdown":
        dst = outdir / "source.md"
        shutil.copyfile(input_path, dst)
        return dst

    if kind in ("html", "arxiv_html"):
        return preprocess_html(input_path, outdir)

    if kind == "pdf":
        try:
            return preprocess_pdf_marker(input_path, outdir)
        except Exception as e:
            print(f"[preprocess] marker failed ({e}); falling back to pymupdf", file=sys.stderr)
            return preprocess_pdf_fallback(input_path, outdir)

    raise ValueError(f"Unknown kind: {kind}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--kind", required=True, choices=["pdf", "html", "arxiv_html", "markdown"])
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()
    path = preprocess(Path(args.input), args.kind, Path(args.outdir))
    print(path)


if __name__ == "__main__":
    main()
