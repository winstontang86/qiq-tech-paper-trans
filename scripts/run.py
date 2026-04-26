"""run.py —— 总入口。

阶段：
  stage1_prepare: fetch + preprocess + segment + translate.generate
    产出 INDEX.md 与 per-segment prompts，等待外部 LLM 执行器填译文。
  stage2_finalize: translate.collect + postprocess + qa_report
    组装译文、后处理、阻断级质检、产出最终文件。

用法示例：
  # 阶段 1：准备任务
  python3 run.py --input paper.pdf --outdir out/ --stage prepare

  # 阶段 2：外部 LLM 执行器完成翻译后，组装 + 质检
  python3 run.py --outdir out/ --stage finalize

  # 一键模式：当前等价于 prepare，不绑定任何具体 LLM 平台
  python3 run.py --input paper.pdf --outdir out/ --stage all
  （all 模式会生成翻译任务索引；译文写回后再执行 finalize）
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
from qa_report import check as qa_check, write_report as qa_write, write_fix_prompts as qa_write_fix_prompts


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

    source_md = outdir / "source.md"
    if args.resume and source_md.exists() and source_md.stat().st_size > 0:
        print(f"[run] resume: source.md exists; skip preprocess: {source_md}")
    else:
        source_md = preprocess(
            local_path,
            kind,
            outdir,
            pdf_engine=args.pdf_engine,
            marker_timeout=args.marker_timeout,
            large_pdf_pages=args.large_pdf_pages,
            pdf_chunk_pages=args.pdf_chunk_pages,
            chunk_timeout=args.chunk_timeout,
            chunk_fallback=args.chunk_fallback,
            resume=args.resume,
        )
        print(f"[run] preprocessed: {source_md}")

    segments_path = outdir / "segments.json"
    locked_path = outdir / "locked_blocks.json"
    masked_path = outdir / "masked.md"
    if (
        args.resume
        and segments_path.exists()
        and segments_path.stat().st_size > 0
        and locked_path.exists()
        and locked_path.stat().st_size > 0
    ):
        seg_out = {"masked": masked_path, "locked": locked_path, "segments": segments_path}
        print(f"[run] resume: segments.json exists; skip segment: {segments_path}")
    else:
        seg_out = segment_run(source_md, outdir, table_mode=args.table_mode)
        print(f"[run] segmented: {seg_out['segments']} table_mode={args.table_mode}")

    builtin = SKILL_ROOT / "references" / "glossary_ai_ml.json"
    user_glossary = Path(args.glossary) if args.glossary else None
    glossary = load_glossary(builtin, user_glossary)
    translate_generate(
        seg_out["segments"], outdir, glossary,
        unit_mode=args.unit_mode,
        hybrid_max_chars=args.hybrid_max_chars,
    )
    print(f"[run] prompts generated in {outdir}/prompts_per_segment/ unit_mode={args.unit_mode}")
    print(f"[run] INDEX: {outdir}/INDEX.md")
    print()
    print("=" * 60)
    print("下一步：外部 LLM 执行器按 INDEX.md 指引逐个翻译单元翻译。")
    print("所有翻译单元完成并写回 zh_per_segment/ 后，运行：")
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
    fix_dir = qa_write_fix_prompts(summary, outdir)
    print(f"[run] qa report: {qa_report_path}")
    if fix_dir:
        print(f"[run] fix prompts: {fix_dir}")

    if blockers and not args.force:
        print(f"[run] BLOCKED: {len(blockers)} blocker(s).")
        raise SystemExit(2)

    if args.bilingual:
        import json as _json
        units_path = outdir / "translation_units.json"
        if units_path.exists():
            items = _json.loads(units_path.read_text(encoding="utf-8"))
        else:
            items = _json.loads(segments_path.read_text(encoding="utf-8"))
        zh_dir = outdir / "zh_per_segment"
        bilingual_lines = []
        for item in items:
            if item.get("is_reference"):
                continue
            item_id = item["id"]
            zh_path = zh_dir / f"{item_id}.zh.md"
            zh = zh_path.read_text(encoding="utf-8").strip() if zh_path.exists() else ""
            bilingual_lines.append(f"<!-- {item_id} EN -->")
            bilingual_lines.append(item["text"])
            bilingual_lines.append("")
            bilingual_lines.append(f"<!-- {item_id} ZH -->")
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
                    help="(prepare) reuse existing source.md, segments.json and completed chunk outputs when possible")
    ap.add_argument("--pdf-engine", choices=["auto", "marker", "pymupdf", "marker-chunked"], default="auto",
                    help="PDF preprocess engine: auto uses full Marker for small PDFs and chunked Marker for large PDFs")
    ap.add_argument("--marker-timeout", type=int, default=900,
                    help="timeout seconds for full-PDF Marker in auto/marker mode")
    ap.add_argument("--large-pdf-pages", type=int, default=30,
                    help="auto mode switches to chunked Marker when PDF pages exceed this threshold")
    ap.add_argument("--pdf-chunk-pages", type=int, default=8,
                    help="pages per chunk for marker-chunked mode")
    ap.add_argument("--chunk-timeout", type=int, default=300,
                    help="timeout seconds for each Marker chunk")
    ap.add_argument("--chunk-fallback", choices=["pymupdf", "skip", "fail"], default="pymupdf",
                    help="fallback policy when a Marker chunk fails or times out")
    ap.add_argument("--unit-mode", choices=["segment", "section", "hybrid"], default="hybrid",
                    help="translation unit: segment is safest, section is fastest, hybrid balances both")
    ap.add_argument("--hybrid-max-chars", type=int, default=12000,
                    help="max chars per unit in hybrid mode")
    ap.add_argument("--table-mode", choices=["lock", "translate"], default="lock",
                    help="lock tables as placeholders or translate table text while preserving structure")
    args = ap.parse_args()

    if args.stage in ("prepare", "all"):
        if not args.input:
            ap.error("--input required for prepare/all stage")
        stage_prepare(args)
    if args.stage == "finalize":
        stage_finalize(args)


if __name__ == "__main__":
    main()
