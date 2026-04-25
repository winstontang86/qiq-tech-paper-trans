"""run.py —— 总入口。

阶段：
  stage1_prepare: fetch + preprocess + segment + translate.generate
    产出 INDEX.md 与 per-segment prompts，等待外层 LLM Agent 填译文。
  stage2_finalize: translate.collect + postprocess + qa_report
    组装译文、后处理、阻断级质检、产出最终文件。

用法示例：
  # 阶段 1：准备任务
  python3 run.py --input paper.pdf --outdir out/ --stage prepare

  # 阶段 2：LLM 逐段翻译后，组装 + 质检
  python3 run.py --outdir out/ --stage finalize

  # 一键（需要外层 LLM 在同一进程内逐段处理，本 skill 默认走两阶段）
  python3 run.py --input paper.pdf --outdir out/ --stage all
  （all 模式等价于 prepare，打印提示让 LLM 继续处理；完成后再 finalize）
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

# 允许作为 `python3 run.py` 直接运行
sys.path.insert(0, str(Path(__file__).parent))

from fetch import fetch
from preprocess import preprocess
from segment import run as segment_run
from translate import load_glossary, generate as translate_generate, collect as translate_collect
from postprocess import postprocess
from qa_report import check as qa_check, write_report as qa_write


SKILL_ROOT = Path(__file__).parent.parent


def stem_of(input_str: str) -> str:
    p = Path(input_str)
    if p.exists():
        return p.stem
    # URL
    from urllib.parse import urlparse
    u = urlparse(input_str)
    s = Path(u.path).stem or u.netloc.replace(".", "_")
    return s or "paper"


def stage_prepare(args) -> Path:
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[run] stage=prepare input={args.input}")
    local_path, kind = fetch(args.input, outdir)
    print(f"[run] fetched: kind={kind} path={local_path}")

    source_md = preprocess(local_path, kind, outdir)
    print(f"[run] preprocessed: {source_md}")

    seg_out = segment_run(source_md, outdir)
    print(f"[run] segmented: {seg_out['segments']}")

    builtin = SKILL_ROOT / "references" / "glossary_ai_ml.json"
    user_glossary = Path(args.glossary) if args.glossary else None
    glossary = load_glossary(builtin, user_glossary)
    translate_generate(seg_out["segments"], outdir, glossary)
    print(f"[run] prompts generated in {outdir}/prompts_per_segment/")
    print(f"[run] INDEX: {outdir}/INDEX.md")
    print()
    print("=" * 60)
    print("下一步：LLM Agent 按 INDEX.md 指引逐段翻译。")
    print("所有段落翻译完成后，运行：")
    print(f"  python3 {Path(__file__).name} --stage finalize --outdir {outdir}")
    if args.bilingual:
        print("  （finalize 阶段加 --bilingual 生成双语对照）")
    print("=" * 60)
    return outdir


def stage_finalize(args) -> None:
    outdir = Path(args.outdir)
    segments_path = outdir / "segments.json"
    locked_path = outdir / "locked_blocks.json"
    source_md = outdir / "source.md"
    if not segments_path.exists():
        raise FileNotFoundError(f"missing {segments_path}; run --stage prepare first")

    raw = translate_collect(segments_path, outdir)
    print(f"[run] translated_raw: {raw}")

    stem = stem_of(args.input) if args.input else "translated"
    final_md = outdir / f"{stem}.zh.md"
    postprocess(raw, locked_path, final_md)
    print(f"[run] postprocessed: {final_md}")

    skip = [s.strip() for s in (args.skip_checks or "").split(",") if s.strip()]
    summary, blockers = qa_check(source_md, final_md, segments_path, skip_checks=skip)
    qa_report_path = outdir / f"{stem}.qa.md"
    qa_write(summary, qa_report_path)
    print(f"[run] qa report: {qa_report_path}")

    if blockers and not args.force:
        print(f"[run] BLOCKED: {len(blockers)} blocker(s).")
        raise SystemExit(2)

    if args.bilingual:
        import json as _json
        segs = _json.loads(segments_path.read_text(encoding="utf-8"))
        zh_dir = outdir / "zh_per_segment"
        bilingual_lines = []
        for seg in segs:
            if seg.get("is_reference"):
                bilingual_lines.append(seg["text"])
                bilingual_lines.append("")
                continue
            zh_path = zh_dir / f"{seg['id']}.zh.md"
            zh = zh_path.read_text(encoding="utf-8").strip() if zh_path.exists() else ""
            bilingual_lines.append(f"<!-- {seg['id']} EN -->")
            bilingual_lines.append(seg["text"])
            bilingual_lines.append("")
            bilingual_lines.append(f"<!-- {seg['id']} ZH -->")
            bilingual_lines.append(zh)
            bilingual_lines.append("")
            bilingual_lines.append("---")
            bilingual_lines.append("")
        bilingual_path = outdir / f"{stem}.bilingual.md"
        bilingual_path.write_text("\n".join(bilingual_lines), encoding="utf-8")
        print(f"[run] bilingual: {bilingual_path}")

    print(f"[run] DONE. Output: {final_md}")


def main():
    ap = argparse.ArgumentParser(description="Technical paper translation pipeline.")
    ap.add_argument("--input", help="PDF path or URL")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--stage", choices=["prepare", "finalize", "all"], default="all")
    ap.add_argument("--glossary", help="User glossary JSON (overrides builtin)")
    ap.add_argument("--bilingual", action="store_true", help="Also emit bilingual .md")
    ap.add_argument("--force", action="store_true", help="Skip blocker checks")
    ap.add_argument("--skip-checks", default="", help="comma-separated Bx to skip")
    ap.add_argument("--resume", action="store_true",
                    help="(prepare) keep existing non-empty zh_per_segment/*")
    args = ap.parse_args()

    if args.stage in ("prepare", "all"):
        if not args.input:
            ap.error("--input required for prepare/all stage")
        stage_prepare(args)
    if args.stage == "finalize":
        stage_finalize(args)


if __name__ == "__main__":
    main()
