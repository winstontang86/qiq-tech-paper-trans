"""qa_report.py —— 阻断级质检。

对原文与译文进行对齐检查。任一阻断项未通过则返回非零退出码，除非 --force。
"""
from __future__ import annotations

import re
import json
import argparse
from pathlib import Path
from typing import List, Dict, Any, Tuple


SUMMARY_PHRASES = [
    "综上所述", "简而言之", "总的来说", "作者主要讨论了",
    "简要介绍", "下面我们", "总体而言", "总结来说",
]


def _count(pattern: re.Pattern, text: str) -> int:
    return len(pattern.findall(text))


FORMULA_PATTERNS_CHECK = [
    re.compile(r"\$\$(?:[^$]|\\\$)+?\$\$", re.DOTALL),
    re.compile(r"\\\[(?:.|\n)+?\\\]", re.DOTALL),
    re.compile(r"\\begin\{(equation|align|gather|eqnarray)\*?\}(?:.|\n)+?\\end\{\1\*?\}", re.DOTALL),
    re.compile(r"(?<!\\)\$(?!\s)(?:[^$\n]|\\\$)+?(?<!\s)\$", re.DOTALL),
]
CODE_RE = re.compile(r"```[a-zA-Z0-9]*\n.*?\n```", re.DOTALL)
TABLE_RE = re.compile(
    r"(?:^\|[^\n]*\|\s*\n)+^\|[ :\-|]+\|\s*\n(?:^\|[^\n]*\|\s*\n?)+",
    re.MULTILINE,
)
IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]+\)")
CITATION_RE = re.compile(r"\[\d+(?:,\s*\d+)*\]|\([A-Z][\w\-]+(?:\s+et\s+al\.?)?,?\s+\d{4}[a-z]?\)")


def count_elements(text: str) -> Dict[str, int]:
    formula = sum(_count(p, text) for p in FORMULA_PATTERNS_CHECK)
    return {
        "code": _count(CODE_RE, text),
        "table": _count(TABLE_RE, text),
        "image": _count(IMAGE_RE, text),
        "formula": formula,
        "citation": _count(CITATION_RE, text),
    }


def _paragraphs(text: str) -> List[str]:
    return [p for p in re.split(r"\n{2,}", text) if p.strip()]


def _is_mostly_english(s: str) -> bool:
    """判断文本是否疑似未翻译（英文字符占比 > 70%）。"""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    eng = sum(1 for c in letters if c.isascii())
    return eng / len(letters) > 0.7


def check(source_md: Path, translated_md: Path, segments_path: Path,
          skip_checks: List[str] | None = None) -> Tuple[Dict[str, Any], List[str]]:
    skip = set(skip_checks or [])
    src = source_md.read_text(encoding="utf-8")
    tgt = translated_md.read_text(encoding="utf-8")
    segments = json.loads(segments_path.read_text(encoding="utf-8"))

    src_counts = count_elements(src)
    tgt_counts = count_elements(tgt)

    src_paragraphs = _paragraphs(src)
    tgt_paragraphs = _paragraphs(tgt)

    blockers: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    # B1 段落数对齐（允许译文略多，不允许少）
    if "B1" not in skip and len(tgt_paragraphs) < len(src_paragraphs) * 0.9:
        blockers.append({
            "id": "B1",
            "title": "段落数对齐异常",
            "detail": f"原文 {len(src_paragraphs)} 段 / 译文 {len(tgt_paragraphs)} 段"
        })

    # B2 图片
    if "B2" not in skip and tgt_counts["image"] < src_counts["image"]:
        blockers.append({
            "id": "B2", "title": "图片保留异常",
            "detail": f"原文 {src_counts['image']} 张 / 译文 {tgt_counts['image']} 张"
        })

    # B3 表格
    if "B3" not in skip and tgt_counts["table"] < src_counts["table"]:
        blockers.append({
            "id": "B3", "title": "表格保留异常",
            "detail": f"原文 {src_counts['table']} 个 / 译文 {tgt_counts['table']} 个"
        })

    # B4 公式
    if "B4" not in skip and tgt_counts["formula"] < src_counts["formula"]:
        blockers.append({
            "id": "B4", "title": "公式保留异常",
            "detail": f"原文 {src_counts['formula']} 个 / 译文 {tgt_counts['formula']} 个"
        })

    # B5 代码块
    if "B5" not in skip and tgt_counts["code"] < src_counts["code"]:
        blockers.append({
            "id": "B5", "title": "代码块保留异常",
            "detail": f"原文 {src_counts['code']} 个 / 译文 {tgt_counts['code']} 个"
        })

    # B6 引用
    if "B6" not in skip and tgt_counts["citation"] < int(src_counts["citation"] * 0.95):
        blockers.append({
            "id": "B6", "title": "引用编号保留异常",
            "detail": f"原文 {src_counts['citation']} 处 / 译文 {tgt_counts['citation']} 处"
        })

    # B7 段级长度比（只对非 reference 段进行）
    seg_rows = []
    bad_ratio_segs = []
    zh_dir = translated_md.parent / "zh_per_segment"
    for seg in segments:
        if seg.get("is_reference"):
            continue
        zh_path = zh_dir / f"{seg['id']}.zh.md"
        if not zh_path.exists():
            continue
        zh_text = zh_path.read_text(encoding="utf-8").strip()
        en_len = max(seg["char_len"], 1)
        zh_len = len(zh_text)
        ratio = zh_len / en_len
        status = "OK"
        if zh_len == 0:
            status = "EMPTY"
            bad_ratio_segs.append((seg["id"], "empty", ratio))
        elif "B7" not in skip and not (0.3 <= ratio <= 2.5):
            status = "BAD"
            bad_ratio_segs.append((seg["id"], f"{ratio:.2f}", ratio))
        seg_rows.append({
            "id": seg["id"], "en": en_len, "zh": zh_len,
            "ratio": round(ratio, 2), "status": status,
        })
    if "B7" not in skip and bad_ratio_segs:
        blockers.append({
            "id": "B7", "title": "段级长度比异常",
            "detail": f"共 {len(bad_ratio_segs)} 段超出 [0.3, 2.5] 区间",
            "segments": [s[0] for s in bad_ratio_segs[:20]],
        })

    # B8 摘要性短语
    if "B8" not in skip:
        hit = [ph for ph in SUMMARY_PHRASES if ph in tgt]
        if hit:
            warnings.append({
                "id": "B8", "title": "译文含摘要性短语",
                "detail": f"命中: {', '.join(hit)}",
            })

    # W4 疑似未翻译段（警告）
    untranslated = []
    for seg in segments:
        if seg.get("is_reference"):
            continue
        zh_path = zh_dir / f"{seg['id']}.zh.md"
        if not zh_path.exists():
            continue
        zh = zh_path.read_text(encoding="utf-8").strip()
        if zh and _is_mostly_english(zh):
            untranslated.append(seg["id"])
    if untranslated:
        warnings.append({
            "id": "W4", "title": "疑似未翻译段落",
            "detail": f"共 {len(untranslated)} 段",
            "segments": untranslated[:20],
        })

    summary = {
        "source": str(source_md),
        "translated": str(translated_md),
        "src_paragraphs": len(src_paragraphs),
        "tgt_paragraphs": len(tgt_paragraphs),
        "src_counts": src_counts,
        "tgt_counts": tgt_counts,
        "blockers": blockers,
        "warnings": warnings,
        "segments": seg_rows,
        "passed": len(blockers) == 0,
    }
    return summary, blockers


def write_report(summary: Dict[str, Any], out_path: Path) -> Path:
    lines = [
        "# 翻译质检报告",
        "",
        f"- 原文：`{summary['source']}`",
        f"- 译文：`{summary['translated']}`",
        f"- 原文段数：{summary['src_paragraphs']}  /  译文段数：{summary['tgt_paragraphs']}",
        f"- 结论：**{'通过' if summary['passed'] else '阻断'}**",
        "",
        "## 元素计数对比",
        "",
        "| 元素 | 原文 | 译文 |",
        "|---|---|---|",
    ]
    sc = summary["src_counts"]
    tc = summary["tgt_counts"]
    for k in ("image", "table", "formula", "code", "citation"):
        lines.append(f"| {k} | {sc[k]} | {tc[k]} |")
    lines.append("")

    if summary["blockers"]:
        lines += ["## 阻断项", ""]
        for b in summary["blockers"]:
            lines.append(f"### {b['id']} {b['title']}")
            lines.append("")
            lines.append(f"- {b['detail']}")
            if "segments" in b:
                lines.append(f"- 相关段: {', '.join(b['segments'])}")
            lines.append("")

    if summary["warnings"]:
        lines += ["## 警告", ""]
        for w in summary["warnings"]:
            lines.append(f"### {w['id']} {w['title']}")
            lines.append("")
            lines.append(f"- {w['detail']}")
            if "segments" in w:
                lines.append(f"- 相关段: {', '.join(w['segments'])}")
            lines.append("")

    # 段级详情（最多列 200 段）
    seg_rows = summary["segments"][:200]
    if seg_rows:
        lines += ["## 段级详情（前 200 段）", "",
                  "| seg_id | en_chars | zh_chars | ratio | status |",
                  "|---|---|---|---|---|"]
        for r in seg_rows:
            lines.append(f"| {r['id']} | {r['en']} | {r['zh']} | {r['ratio']} | {r['status']} |")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--translated", required=True)
    ap.add_argument("--segments", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--skip-checks", default="", help="comma-separated, e.g. B7,B8")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    skip = [s.strip() for s in args.skip_checks.split(",") if s.strip()]
    summary, blockers = check(
        Path(args.source), Path(args.translated), Path(args.segments), skip_checks=skip
    )
    write_report(summary, Path(args.report))

    if blockers and not args.force:
        print(f"[qa] BLOCKED: {len(blockers)} blocker(s). See {args.report}")
        raise SystemExit(2)
    print(f"[qa] OK (blockers={len(blockers)}, warnings={len(summary['warnings'])})")


if __name__ == "__main__":
    main()
