"""postprocess.py —— 译文后处理：回贴锁定块、规范中英排版。

步骤：
1. 从 translated_raw.md 读入含占位符的译文。
2. 按 locked_blocks.json 回贴原始锁定内容。
3. 文本规范化：
   - 中英文之间加空格
   - 中文段落中的半角 , ; : ? ! 转全角
   - 移除多余空行（≥3 个连续换行压缩为 2 个）
4. 写入 translated.md。
"""
from __future__ import annotations

import re
import json
import argparse
from pathlib import Path


PLACEHOLDER_RE = re.compile(r"⟦(CODE|FORMULA|INLINE_FORMULA|TABLE|IMAGE)_(\d{4})⟧")


def restore_locked(text: str, mapping: dict) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(0)
        return mapping.get(key, key)
    return PLACEHOLDER_RE.sub(repl, text)


# 中文字符
CJK = r"\u4e00-\u9fff"

# 中英文之间加空格
SPACE_CJK_ALNUM_RE = re.compile(rf"([{CJK}])([A-Za-z0-9])")
SPACE_ALNUM_CJK_RE = re.compile(rf"([A-Za-z0-9])([{CJK}])")

# 半角标点 -> 全角（仅在 CJK 上下文中）
HALF_TO_FULL = {
    ",": "，",
    ";": "；",
    ":": "：",
    "?": "？",
    "!": "！",
}


def _normalize_punct_cjk_context(text: str) -> str:
    """仅当半角标点左侧或右侧紧邻 CJK 字符时，转为全角。
    避免误伤代码块/英文句子的标点。"""
    # 先按行处理；跳过占位符回贴后的代码/表格/公式不在此文件处理
    # （它们已在 restore_locked 之后，直接跳过被三个反引号围住的块）
    result = []
    in_code_block = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            continue
        if in_code_block:
            result.append(line)
            continue
        # 跳过明显的表格行
        if re.match(r"^\s*\|", line):
            result.append(line)
            continue

        new = line
        for half, full in HALF_TO_FULL.items():
            # CJK + half + (space?) + CJK  或 CJK + half 末尾
            new = re.sub(rf"([{CJK}]){re.escape(half)}(?=[{CJK} ])", r"\1" + full, new)
            new = re.sub(rf"([{CJK}]){re.escape(half)}$", r"\1" + full, new)
        # 句末 .
        new = re.sub(rf"([{CJK}])\.(?=\s|$)", r"\1。", new)
        result.append(new)
    return "\n".join(result)


def _space_between_cjk_and_alnum(text: str) -> str:
    result = []
    in_code_block = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            continue
        if in_code_block:
            result.append(line)
            continue
        if re.match(r"^\s*\|", line):
            result.append(line)
            continue
        new = SPACE_CJK_ALNUM_RE.sub(r"\1 \2", line)
        new = SPACE_ALNUM_CJK_RE.sub(r"\1 \2", new)
        result.append(new)
    return "\n".join(result)


def _compact_blanklines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def postprocess(raw_translated_path: Path, locked_blocks_path: Path, out_path: Path) -> Path:
    raw = raw_translated_path.read_text(encoding="utf-8")
    mapping = json.loads(locked_blocks_path.read_text(encoding="utf-8"))

    text = restore_locked(raw, mapping)
    text = _normalize_punct_cjk_context(text)
    text = _space_between_cjk_and_alnum(text)
    text = _compact_blanklines(text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="translated_raw.md")
    ap.add_argument("--locked", required=True, help="locked_blocks.json")
    ap.add_argument("--out", required=True, help="output translated.md")
    args = ap.parse_args()
    p = postprocess(Path(args.raw), Path(args.locked), Path(args.out))
    print(p)


if __name__ == "__main__":
    main()
