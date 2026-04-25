"""translate.py —— 按段生成滑动窗口三段法 prompt，并收集译文。

设计：
  本 skill 不直接调用 LLM API。由于 WorkBuddy 外层 Agent 本身就是 LLM，我们把每段的
  翻译请求写成结构化 prompt 文件，Agent 读取并逐段产出译文后回写到约定路径。

支持两种模式：
  --mode generate   生成 prompts/ 目录下的 <seg_id>.prompt.md 与空的 <seg_id>.zh.md。
                    同时生成 INDEX.md 指导 Agent 如何逐段翻译。
  --mode collect    扫描 <seg_id>.zh.md（已由 Agent 填写），组装成 translated.md。
                    参考文献段直接保留原文。

断点续译：
  collect 阶段遇到空 zh.md 会警告；generate 阶段不会覆盖已有非空 zh.md。
"""
from __future__ import annotations

import os
import re
import sys
import json
import argparse
from pathlib import Path
from typing import List, Dict, Any


# 窗口大小（字符数，近似 token * 4）
PREV_WINDOW_CHARS = 800 * 4
NEXT_WINDOW_CHARS = 800 * 4


def load_glossary(builtin_path: Path, user_path: Path | None) -> Dict[str, str]:
    def _load(p: Path) -> Dict[str, str]:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_")}

    glossary: Dict[str, str] = {}
    if builtin_path.exists():
        glossary.update(_load(builtin_path))
    if user_path and Path(user_path).exists():
        glossary.update(_load(Path(user_path)))
    return glossary


def _filter_glossary_for_text(glossary: Dict[str, str], text: str) -> List[str]:
    """仅保留在 text 中出现的术语，减少 prompt 长度。"""
    lines = []
    text_lower = text.lower()
    for en, zh in glossary.items():
        # 简单匹配：忽略大小写；单词边界
        key = en.lower()
        if re.search(r"\b" + re.escape(key) + r"\b", text_lower):
            lines.append(f"{en} -> {zh}")
    return lines


def _build_window(segments: List[Dict[str, Any]], idx: int) -> Dict[str, str]:
    """为第 idx 段构建 prev / next 窗口；不跨越 section。"""
    cur = segments[idx]
    cur_section = cur.get("section_heading", "")

    def gather(start: int, step: int, budget: int) -> str:
        pieces = []
        used = 0
        i = start
        while 0 <= i < len(segments) and used < budget:
            if segments[i].get("section_heading", "") != cur_section:
                break
            if segments[i].get("is_reference"):
                break
            piece = segments[i]["text"]
            if used + len(piece) > budget:
                # 截取片段末尾或开头
                take = budget - used
                piece = piece[-take:] if step < 0 else piece[:take]
            pieces.append(piece)
            used += len(piece)
            i += step
        if step < 0:
            pieces.reverse()
        return "\n\n".join(pieces).strip()

    prev_text = gather(idx - 1, -1, PREV_WINDOW_CHARS)
    next_text = gather(idx + 1, +1, NEXT_WINDOW_CHARS)
    return {"prev": prev_text, "next": next_text}


SYSTEM_PROMPT = """你是专业的技术论文译者，擅长 AI/机器学习领域英文论文的中文翻译。严格遵循以下规则：

【风格】信达雅，学术书面语，第三人称视角；忠实于原文，不得省略、不得自行概括、不得补全原文没有的内容。一段对应一段，逐句翻译。

【结构保真】
- Markdown 结构（标题 # 层级、列表、引用块）一比一保留。
- 文本中的占位符形如 ⟦CODE_0001⟧、⟦FORMULA_0003⟧、⟦TABLE_0002⟧、⟦IMAGE_0005⟧、⟦INLINE_FORMULA_0004⟧，这些都代表被锁定的内容（公式/代码/表格/图片），必须原样保留在译文对应位置，不得改动，不得翻译。
- 引用编号 [12]、[Author, 2024]、(Smith et al., 2023) 原样保留。

【术语】
- 严格使用 <glossary> 中给出的对照；同一术语全文一致。
- 术语首次出现："中文（English）"；后续只用中文。
- 专有名词（模型名、机构名、人名、数据集名）保留英文原样。
- 缩写首次出现："中文全称（英文缩写）"；后续只用缩写。

【上下文】<previous_context> 和 <next_context> 仅用于理解上下文，不得翻译它们。只输出 <current_segment> 的中文译文。

【数字、单位、日期】保留原格式；单位不译；年份日期保留原写法。

【输出格式】直接输出译文，不要添加任何前缀、后缀、解释、"以下是翻译"等字样。不输出 XML 标签。中英文之间加空格，中文用全角标点，英文保留半角标点。
"""


USER_TEMPLATE = """<previous_context>
{prev}
</previous_context>

<current_segment id="{seg_id}">
{current}
</current_segment>

<next_context>
{next}
</next_context>

<glossary>
{glossary}
</glossary>

请翻译 <current_segment> 的内容为中文。仅输出译文本身，不要输出上下文和任何解释。
"""


def generate(segments_path: Path, outdir: Path, glossary: Dict[str, str]) -> None:
    segments: List[Dict[str, Any]] = json.loads(segments_path.read_text(encoding="utf-8"))

    prompts_dir = outdir / "prompts_per_segment"
    zh_dir = outdir / "zh_per_segment"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    zh_dir.mkdir(parents=True, exist_ok=True)

    index_lines = [
        "# 段落翻译任务索引",
        "",
        "## 给 LLM Agent 的说明",
        "",
        "- 对每个段落 seg_XXXX：",
        "  1. 读取 `prompts_per_segment/seg_XXXX.prompt.md`",
        "  2. 其中 system 部分放在本次 LLM 调用的 system prompt；user 部分作为 user message。",
        "  3. 将 LLM 输出的中文译文写入 `zh_per_segment/seg_XXXX.zh.md`（覆盖写）。",
        "  4. is_reference=True 的段落无需翻译（已自动跳过）。",
        "- 全部段落翻译完后，执行：",
        "  `python3 translate.py --mode collect --workdir <outdir>` 组装译文。",
        "",
        "## 段落清单",
        "",
        "| seg_id | section | char_len | is_reference | status |",
        "|---|---|---|---|---|",
    ]

    for idx, seg in enumerate(segments):
        seg_id = seg["id"]
        zh_path = zh_dir / f"{seg_id}.zh.md"
        prompt_path = prompts_dir / f"{seg_id}.prompt.md"

        if seg.get("is_reference"):
            # Auto-fill with original (no translation)
            zh_path.write_text(seg["text"], encoding="utf-8")
            index_lines.append(
                f"| {seg_id} | {seg['section_heading']} | {seg['char_len']} | yes | skipped |"
            )
            continue

        window = _build_window(segments, idx)
        gloss_lines = _filter_glossary_for_text(glossary, seg["text"] + "\n" + window["prev"] + "\n" + window["next"])
        glossary_text = "\n".join(gloss_lines) if gloss_lines else "（无需特别关注的术语）"

        user_msg = USER_TEMPLATE.format(
            prev=window["prev"] or "（本段为文档开头或章节开头）",
            seg_id=seg_id,
            current=seg["text"],
            next=window["next"] or "（本段为文档末尾或章节末尾）",
            glossary=glossary_text,
        )

        prompt_md = (
            "# SYSTEM\n\n"
            + SYSTEM_PROMPT
            + "\n\n---\n\n# USER\n\n"
            + user_msg
        )
        prompt_path.write_text(prompt_md, encoding="utf-8")

        # Only create empty zh file if not exists (resume-friendly)
        if not zh_path.exists():
            zh_path.write_text("", encoding="utf-8")

        status = "done" if zh_path.read_text(encoding="utf-8").strip() else "pending"
        index_lines.append(
            f"| {seg_id} | {seg['section_heading']} | {seg['char_len']} | no | {status} |"
        )

    (outdir / "INDEX.md").write_text("\n".join(index_lines), encoding="utf-8")


def collect(segments_path: Path, outdir: Path) -> Path:
    segments: List[Dict[str, Any]] = json.loads(segments_path.read_text(encoding="utf-8"))
    zh_dir = outdir / "zh_per_segment"

    pieces = []
    missing = []
    for seg in segments:
        zh_path = zh_dir / f"{seg['id']}.zh.md"
        if not zh_path.exists() or not zh_path.read_text(encoding="utf-8").strip():
            if seg.get("is_reference"):
                pieces.append(seg["text"])
            else:
                missing.append(seg["id"])
                pieces.append(f"\n[!MISSING TRANSLATION: {seg['id']}]\n\n" + seg["text"])
            continue
        pieces.append(zh_path.read_text(encoding="utf-8").rstrip())

    translated = "\n\n".join(pieces).strip() + "\n"
    out_path = outdir / "translated_raw.md"
    out_path.write_text(translated, encoding="utf-8")

    if missing:
        print(f"[translate.collect] WARNING: {len(missing)} segments missing translations: "
              f"{', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}",
              file=sys.stderr)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["generate", "collect"])
    ap.add_argument("--segments", help="segments.json path")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--glossary-builtin", default=None)
    ap.add_argument("--glossary-user", default=None)
    args = ap.parse_args()

    workdir = Path(args.workdir)
    segments_path = Path(args.segments) if args.segments else (workdir / "segments.json")

    if args.mode == "generate":
        builtin = Path(args.glossary_builtin) if args.glossary_builtin else (
            Path(__file__).parent.parent / "references" / "glossary_ai_ml.json"
        )
        glossary = load_glossary(builtin, Path(args.glossary_user) if args.glossary_user else None)
        generate(segments_path, workdir, glossary)
        print(f"[translate] generated prompts under {workdir}/prompts_per_segment/")
    else:
        out = collect(segments_path, workdir)
        print(out)


if __name__ == "__main__":
    main()
