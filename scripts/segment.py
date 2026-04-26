"""segment.py —— 结构化 Markdown 分段 + 锁定块占位符化。

职责：
1. 识别 locked blocks（公式、代码、表格、图片、引用编号），替换为稳定占位符。
2. 按段落自然边界分段；段落长度控制在目标 token 区间。
3. 识别大章节标题；分段不跨越大章节。
4. 识别参考文献节（References / Bibliography），从该节开始及其后所有内容均标记为跳过，不进入最终译文。

输出：
- <outdir>/segments.json     —— 段落列表 + 元信息
- <outdir>/locked_blocks.json —— 占位符 -> 原内容
- <outdir>/masked.md          —— 用占位符替换后的全文（方便调试）
"""
from __future__ import annotations

import os
import re
import sys
import json
import argparse
from pathlib import Path
from typing import List, Dict, Any

# 目标 token 估计：英文约 4 字符 / token，中文约 1.5 字符 / token
# v1 简化：用字符数近似 token 数。
TARGET_MIN_CHARS = 1500 * 4   # ~1500 tokens
TARGET_MAX_CHARS = 2500 * 4   # ~2500 tokens

FORMULA_PATTERNS = [
    # display math $$...$$
    (re.compile(r"\$\$(?:[^$]|\\\$)+?\$\$", re.DOTALL), "FORMULA"),
    # \[ ... \]
    (re.compile(r"\\\[(?:.|\n)+?\\\]", re.DOTALL), "FORMULA"),
    # \begin{equation|align|gather|eqnarray}...\end{...}
    (re.compile(r"\\begin\{(equation|align|gather|eqnarray)\*?\}(?:.|\n)+?\\end\{\1\*?\}",
                re.DOTALL), "FORMULA"),
    # inline $...$（放最后，避免吞噬 display math）
    (re.compile(r"(?<!\\)\$(?!\s)(?:[^$\n]|\\\$)+?(?<!\s)\$", re.DOTALL), "INLINE_FORMULA"),
]

CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9]*\n.*?\n```", re.DOTALL)

# Markdown 表格：至少一行 |---| 对齐行 + 前后的 | 包裹行
TABLE_RE = re.compile(
    r"(?:^\|[^\n]*\|\s*\n)+^\|[ :\-|]+\|\s*\n(?:^\|[^\n]*\|\s*\n?)+",
    re.MULTILINE
)

# Image: ![alt](url) —— 单行
IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]+\)")

# Reference cutoff header
REFERENCES_HEADER_RE = re.compile(
    r"^(#{1,6})\s*(references\b.*|bibliography\b.*|参考文献.*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _placeholder(kind: str, idx: int) -> str:
    return f"⟦{kind}_{idx:04d}⟧"


def mask_locked_blocks(md: str, table_mode: str = "lock") -> tuple[str, Dict[str, str]]:
    """Replace locked blocks with placeholders. Return (masked_md, mapping).

    table_mode:
    - lock: Markdown 表格整体锁定为占位符，最大化防丢失。
    - translate: 表格不锁定，交给翻译 prompt 要求保持列数/分隔线并翻译文字单元格。
    """
    if table_mode not in {"lock", "translate"}:
        raise ValueError(f"Unknown table_mode: {table_mode}")

    mapping: Dict[str, str] = {}
    counters: Dict[str, int] = {}

    def _next(kind: str) -> str:
        counters[kind] = counters.get(kind, 0) + 1
        return _placeholder(kind, counters[kind])

    # Order matters: code blocks first (they may contain $), then tables, formulas, images
    def replace_one(text: str, pat: re.Pattern, kind: str) -> str:
        def repl(m: re.Match) -> str:
            ph = _next(kind)
            mapping[ph] = m.group(0)
            return ph
        return pat.sub(repl, text)

    masked = md
    masked = replace_one(masked, CODE_BLOCK_RE, "CODE")
    if table_mode == "lock":
        masked = replace_one(masked, TABLE_RE, "TABLE")
    for pat, kind in FORMULA_PATTERNS:
        masked = replace_one(masked, pat, kind)
    masked = replace_one(masked, IMAGE_RE, "IMAGE")

    return masked, mapping


def split_sections(md: str) -> List[Dict[str, Any]]:
    """Split by top-level (# / ##) headings. Each section keeps its heading as first line.

    Returns list of {heading_level, heading_text, body}.
    """
    lines = md.split("\n")
    sections: List[Dict[str, Any]] = []
    current = {"heading_level": 0, "heading_text": "", "lines": []}

    for ln in lines:
        m = re.match(r"^(#{1,3})\s+(.+)$", ln)
        if m and len(m.group(1)) <= 2:  # Only split on # or ##
            if current["lines"] or current["heading_text"]:
                sections.append(current)
            current = {
                "heading_level": len(m.group(1)),
                "heading_text": m.group(2).strip(),
                "lines": [ln],
            }
        else:
            current["lines"].append(ln)
    if current["lines"] or current["heading_text"]:
        sections.append(current)

    for s in sections:
        s["body"] = "\n".join(s["lines"])
        s.pop("lines")
    return sections


def _is_references_section(heading_text: str) -> bool:
    if not heading_text:
        return False
    h = heading_text.strip().lower()
    return h in {"references", "bibliography", "参考文献"} or \
           h.startswith("references") or h.startswith("bibliography")


def _split_paragraphs(body: str) -> List[str]:
    """按空行分段；保留段内换行。"""
    parts = re.split(r"\n{2,}", body)
    return [p for p in (p.strip("\n") for p in parts) if p.strip()]


def _pack_paragraphs(paragraphs: List[str],
                     min_chars: int = TARGET_MIN_CHARS,
                     max_chars: int = TARGET_MAX_CHARS) -> List[str]:
    """把段落拼成目标长度的 chunk；单段超长则单独成块；小段合并。"""
    chunks: List[str] = []
    buf: List[str] = []
    buf_len = 0

    for p in paragraphs:
        p_len = len(p)
        if p_len >= max_chars:
            # Flush buffer
            if buf:
                chunks.append("\n\n".join(buf))
                buf, buf_len = [], 0
            chunks.append(p)
            continue
        if buf_len + p_len + 2 > max_chars and buf_len >= min_chars:
            chunks.append("\n\n".join(buf))
            buf, buf_len = [p], p_len
        else:
            buf.append(p)
            buf_len += p_len + 2
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def segment(masked_md: str, mapping: Dict[str, str]) -> List[Dict[str, Any]]:
    """Return list of segments.

    Each segment: {
      id, section_heading, section_level,
      text,            # masked text (with placeholders)
      is_reference,    # True if at/after References section (excluded from final translation)
      char_len,
    }
    """
    sections = split_sections(masked_md)
    segments: List[Dict[str, Any]] = []
    seg_idx = 0
    after_references = False

    def append_reference_segment(sec: Dict[str, Any], paragraphs: List[str], heading_text: str | None = None) -> None:
        nonlocal seg_idx
        if not paragraphs:
            return
        seg_idx += 1
        segments.append({
            "id": f"seg_{seg_idx:04d}",
            "section_heading": heading_text if heading_text is not None else sec["heading_text"],
            "section_level": sec["heading_level"],
            "text": "\n\n".join(paragraphs),
            "is_reference": True,
            "char_len": sum(len(p) for p in paragraphs),
        })

    def append_translatable_segments(sec: Dict[str, Any], paragraphs: List[str]) -> None:
        nonlocal seg_idx
        chunks = _pack_paragraphs(paragraphs)
        for ch in chunks:
            seg_idx += 1
            segments.append({
                "id": f"seg_{seg_idx:04d}",
                "section_heading": sec["heading_text"],
                "section_level": sec["heading_level"],
                "text": ch,
                "is_reference": False,
                "char_len": len(ch),
            })

    for sec in sections:
        if _is_references_section(sec["heading_text"]):
            after_references = True
        body = sec["body"]
        if not after_references:
            ref_match = REFERENCES_HEADER_RE.search(body)
            if ref_match:
                before_body = body[:ref_match.start()].strip("\n")
                after_body = body[ref_match.start():].strip("\n")
                before_paragraphs = _split_paragraphs(before_body)
                append_translatable_segments(sec, before_paragraphs)

                after_references = True
                ref_heading = re.sub(r"^#{1,6}\s*", "", ref_match.group(0).strip()).strip()
                append_reference_segment(sec, _split_paragraphs(after_body), ref_heading)
                continue

        is_ref = after_references
        paragraphs = _split_paragraphs(body)
        # Heading itself is first paragraph — pack with body
        if not paragraphs:
            continue

        if is_ref:
            # References and all following sections are excluded from final translation.
            append_reference_segment(sec, paragraphs)
            continue

        append_translatable_segments(sec, paragraphs)
    return segments


def run(source_md_path: Path, outdir: Path, table_mode: str = "lock") -> Dict[str, Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    md = source_md_path.read_text(encoding="utf-8")
    masked, mapping = mask_locked_blocks(md, table_mode=table_mode)

    (outdir / "masked.md").write_text(masked, encoding="utf-8")
    (outdir / "locked_blocks.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "segment_config.json").write_text(
        json.dumps({"table_mode": table_mode}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    segments = segment(masked, mapping)
    (outdir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "masked": outdir / "masked.md",
        "locked": outdir / "locked_blocks.json",
        "segments": outdir / "segments.json",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="source.md from preprocess step")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--table-mode", choices=["lock", "translate"], default="lock",
                    help="lock Markdown tables as placeholders or translate textual table cells")
    args = ap.parse_args()
    out = run(Path(args.source), Path(args.outdir), table_mode=args.table_mode)
    for k, v in out.items():
        print(f"{k}\t{v}")


if __name__ == "__main__":
    main()
