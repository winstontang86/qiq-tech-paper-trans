"""preprocess.py —— 将 PDF / HTML / Markdown 转换为结构化 Markdown。

策略：
- PDF：小文件优先整篇 marker-pdf；大文件按页分块 marker-pdf，分块可并行。
  Marker 超时或失败时回退到 pymupdf 轻量提取，避免大文件卡住。
- HTML：markdownify + BeautifulSoup 清洗后转 Markdown。
- Markdown：直接返回原文。

性能相关：
- --chunk-concurrency：分块 Marker 的并行度；每个 worker 都会加载一份模型，
  内存占用随并行度线性上升，一般 2 个 worker 在 16GB 机器上安全。
- 每个分块会写入 status.json 记录使用的引擎（marker/pymupdf/skip/failed），
  --resume 可选地配合 --retry-fallback 仅重跑之前回退到 pymupdf 的分块。

输出：
- <outdir>/source.md        —— 结构化原文 Markdown
- <outdir>/assets/          —— 抽取的图片（如有）
- <outdir>/preprocess_chunks/<chunk_id>/status.json —— 每个分块的状态
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable


def _enable_line_buffered_output() -> None:
    """让长时间运行的 OCR/Marker 进度及时输出到宿主，避免被误判为无响应。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass


_enable_line_buffered_output()

from table_extractor import (
    TableImage,
    build_markdown_image_block,
    extract_tables_as_images,
    strip_markdown_tables,
)


def _inject_table_images(
    md_text: str,
    tables: list[TableImage],
    *,
    remove_existing_tables: bool = True,
) -> str:
    """把截图出的表格以 Markdown 图片形式插入到文本里，并（可选）删除原有 Markdown 表格。

    策略：
      1. 先按现有 Markdown 表格块进行替换（避免既有截图又有乱表）。
      2. 按 page 升序把所有表格图片追加到文末的 "附：表格截图" 区域。
         （想在页内就地插入需要准确的页锚；Marker 输出不保留页号，追加章节更稳妥。）
    """
    if not tables:
        return md_text
    text = strip_markdown_tables(md_text) if remove_existing_tables else md_text
    blocks = ["\n\n<!-- tables rendered as images below -->"]
    for ti in sorted(tables, key=lambda t: (t.page, t.index_on_page)):
        blocks.append(build_markdown_image_block(ti))
    return text.rstrip() + "\n" + "".join(blocks) + "\n"


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


def _clean_text(text: str) -> str:
    text = _dehyphenate(text)
    text = _strip_repeating_headers(text)
    return text.strip() + "\n"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _tail_file_text(path: Path, max_bytes: int = 4096) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return ""
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", errors="ignore")


def _terminate_process_tree(proc: subprocess.Popen, log_fp) -> None:
    """尽量优雅地终止 Marker 子进程组，避免超时 kill 后残留 semaphore/worker。"""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=20)
        log_fp.write(f"[{_now_iso()}] marker parent terminated child gracefully\n")
        log_fp.flush()
    except Exception as e:
        log_fp.write(f"[{_now_iso()}] marker parent graceful terminate failed: {e}; killing child\n")
        log_fp.flush()
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        import fitz  # pymupdf
    except ImportError as e:
        raise ImportError("pymupdf not installed. Run: pip install pymupdf") from e

    doc = fitz.open(str(pdf_path))
    try:
        return doc.page_count
    finally:
        doc.close()


def _copy_assets(src_assets: Path, dst_assets: Path) -> None:
    if not src_assets.exists():
        return
    for src in src_assets.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(src_assets)
        dst = dst_assets / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _rewrite_image_links(md: str, assets_prefix: str = "assets") -> str:
    """修正 Marker/转换输出中的裸图片名或以 `./` 开头的图片引用，
    给它们加上 `assets_prefix/` 前缀，避免 Markdown/Word 渲染时找不到图档。

    已经包含 `assets/`, `http://`, `https://`, `data:`, `/` 绝对路径的不动。
    """
    def repl(match: re.Match[str]) -> str:
        alt = match.group(1)
        raw = match.group(2).strip()
        if re.match(r"^(https?:|data:|/|#)", raw):
            return match.group(0)
        raw = raw.removeprefix("./")
        # 已经包含预期前缀 -> 不重写
        if raw.startswith(f"{assets_prefix}/") or raw.startswith(f"{assets_prefix}\\"):
            return match.group(0)
        return f"![{alt}]({assets_prefix}/{raw})"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, md)


def _prefix_markdown_image_links(md: str, prefix: str) -> str:
    """将 chunk 内相对图片链接改写到 assets/<chunk>/ 下。"""
    def repl(match: re.Match[str]) -> str:
        alt = match.group(1)
        raw = match.group(2).strip()
        if re.match(r"^(https?:|data:|/)", raw):
            return match.group(0)
        raw = raw.removeprefix("./")
        raw = raw.removeprefix("assets/")
        return f"![{alt}](assets/{prefix}/{raw})"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, md)


def _write_marker_output(pdf_path: Path, outdir: Path) -> Path:
    """在当前进程执行 Marker。仅供隔离子进程调用。"""
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.output import text_from_rendered
    except ImportError as e:
        raise ImportError(
            "marker-pdf not installed. Run: pip install marker-pdf"
        ) from e

    outdir.mkdir(parents=True, exist_ok=True)
    print(f"[preprocess] marker worker start pdf={pdf_path.name}", flush=True)
    print("[preprocess] marker worker loading models; first run may download Hugging Face artifacts and take several minutes", flush=True)
    converter = PdfConverter(artifact_dict=create_model_dict())
    print("[preprocess] marker worker models ready", flush=True)
    rendered = converter(str(pdf_path))
    print("[preprocess] marker worker rendered pdf", flush=True)
    text, _, images = text_from_rendered(rendered)

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
                print(f"[preprocess] save image failed: {name} -> {e}", file=sys.stderr, flush=True)

    md_path = outdir / "source.md"
    md_path.write_text(_clean_text(text), encoding="utf-8")
    print(f"[preprocess] marker worker wrote {md_path}", flush=True)
    return md_path


def _run_marker_subprocess(
    pdf_path: Path,
    workdir: Path,
    timeout: int,
    label: str,
    *,
    progress_interval: int = 30,
) -> Path:
    """用子进程运行 Marker，超时后可安全终止，并把日志持久化到 marker.log。"""
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = workdir / "marker.log"
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--marker-worker",
        "--input",
        str(pdf_path),
        "--outdir",
        str(workdir),
    ]
    progress_interval = max(5, int(progress_interval or 30))
    print(f"[preprocess] marker start label={label} timeout={timeout}s log={log_path}", flush=True)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    with log_path.open("a", encoding="utf-8") as log_fp:
        log_fp.write(f"\n[{_now_iso()}] marker parent start label={label} timeout={timeout}s cmd={' '.join(cmd)}\n")
        log_fp.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=(os.name == "posix"),
        )
        started = time.monotonic()
        next_heartbeat = started + progress_interval
        last_log_size = log_path.stat().st_size if log_path.exists() else 0
        last_log_activity = started
        max_timeout = timeout * 3 if timeout > 0 else 0

        while True:
            rc = proc.poll()
            if rc is not None:
                log_fp.write(f"[{_now_iso()}] marker parent exit label={label} rc={rc}\n")
                log_fp.flush()
                if rc != 0:
                    raise RuntimeError(f"marker failed label={label} exit={rc}; see {log_path}")
                md_path = workdir / "source.md"
                if not md_path.exists() or md_path.stat().st_size == 0:
                    raise RuntimeError(f"marker produced empty output label={label}; see {log_path}")
                print(f"[preprocess] marker done label={label} log={log_path}", flush=True)
                return md_path

            elapsed = time.monotonic() - started
            now = time.monotonic()
            current_log_size = log_path.stat().st_size if log_path.exists() else 0
            if current_log_size > last_log_size:
                last_log_size = current_log_size
                last_log_activity = now
            idle_for = now - last_log_activity

            if timeout > 0 and elapsed > timeout:
                if elapsed <= max_timeout and idle_for < 90:
                    if now >= next_heartbeat:
                        msg = (
                            f"[preprocess] marker still active label={label} pid={proc.pid} elapsed={int(elapsed)}s "
                            f"base_timeout={timeout}s max_timeout={max_timeout}s idle_for={int(idle_for)}s log={log_path}"
                        )
                        print(msg, flush=True)
                        _write_json(workdir / "heartbeat.json", {
                            "updated_at": _now_iso(),
                            "label": label,
                            "pid": proc.pid,
                            "elapsed_seconds": int(elapsed),
                            "timeout_seconds": timeout,
                            "max_timeout_seconds": max_timeout,
                            "idle_for_seconds": int(idle_for),
                            "hint": "marker_still_active_after_base_timeout",
                            "log_path": str(log_path),
                        })
                        log_fp.write(f"[{_now_iso()}] {msg}\n")
                        log_fp.flush()
                        next_heartbeat = now + progress_interval
                    time.sleep(1)
                    continue
                _terminate_process_tree(proc, log_fp)
                log_fp.write(
                    f"[{_now_iso()}] marker parent timeout label={label} after {int(elapsed)}s "
                    f"base_timeout={timeout}s max_timeout={max_timeout}s idle_for={int(idle_for)}s\n"
                )
                log_fp.flush()
                raise TimeoutError(f"marker timeout label={label} after {int(elapsed)}s; see {log_path}")

            if now >= next_heartbeat:
                tail = _tail_file_text(log_path)
                hint = ""
                tail_lower = tail.lower()
                if "recognizing text" in tail_lower:
                    hint = " hint=ocr_text_recognition"
                elif "recognizing layout" in tail_lower:
                    hint = " hint=layout_recognition"
                elif any(k in tail_lower for k in ["download", "huggingface", "fetching", "xet", "model"]):
                    hint = " hint=model_init_or_download"
                msg = (
                    f"[preprocess] marker running label={label} pid={proc.pid} elapsed={int(elapsed)}s "
                    f"timeout={timeout}s idle_for={int(idle_for)}s log={log_path}{hint}"
                )
                print(msg, flush=True)
                _write_json(workdir / "heartbeat.json", {
                    "updated_at": _now_iso(),
                    "label": label,
                    "pid": proc.pid,
                    "elapsed_seconds": int(elapsed),
                    "timeout_seconds": timeout,
                    "idle_for_seconds": int(idle_for),
                    "hint": hint.strip() or None,
                    "log_path": str(log_path),
                })
                log_fp.write(f"[{_now_iso()}] {msg}\n")
                log_fp.flush()
                next_heartbeat = now + progress_interval
            time.sleep(1)


def preprocess_pdf_marker(
    pdf_path: Path,
    outdir: Path,
    timeout: int = 900,
    *,
    table_strategy: str = "image",
    progress_interval: int = 30,
) -> Path:
    """Use marker-pdf to convert PDF to Markdown in an isolated subprocess.

    table_strategy=image 时额外调用 pdfplumber 把复杂表格截为 PNG，
    在 Markdown 中替换掉原有 Markdown 表格块、以图片形式保留。
    """
    tmp = outdir / "_marker_full"
    if tmp.exists():
        shutil.rmtree(tmp)
    md_path = _run_marker_subprocess(
        pdf_path,
        tmp,
        timeout,
        "full",
        progress_interval=progress_interval,
    )

    # Marker 输出的 Markdown 里图片引用通常是裸文件名（与图片同目录），
    # 但图片会被拷贝到 outdir/assets/ 下，因此需要把链接改写为 assets/<name>。
    text = md_path.read_text(encoding="utf-8")
    text = _rewrite_image_links(text, "assets")

    _copy_assets(tmp / "assets", outdir / "assets")

    if table_strategy == "image":
        try:
            tables = extract_tables_as_images(
                pdf_path,
                out_image_dir=outdir / "assets" / "tables",
                outdir_root=outdir,
            )
            if tables:
                print(f"[preprocess] table_strategy=image: captured {len(tables)} tables as PNG")
                text = _inject_table_images(text, tables)
            else:
                print("[preprocess] table_strategy=image: no tables detected")
        except Exception as e:
            print(f"[preprocess] table extraction failed ({e}); keep Markdown tables as-is",
                  file=sys.stderr)

    final_md = outdir / "source.md"
    final_md.write_text(text, encoding="utf-8")
    return final_md


def _extract_pdf_pages(src_pdf: Path, dst_pdf: Path, start_page: int, end_page: int) -> Path:
    """抽取 1-based 闭区间页码到新的 PDF。"""
    try:
        import fitz  # pymupdf
    except ImportError as e:
        raise ImportError("pymupdf not installed. Run: pip install pymupdf") from e

    src = fitz.open(str(src_pdf))
    dst = fitz.open()
    try:
        dst.insert_pdf(src, from_page=start_page - 1, to_page=end_page - 1)
        dst_pdf.parent.mkdir(parents=True, exist_ok=True)
        dst.save(str(dst_pdf))
    finally:
        dst.close()
        src.close()
    return dst_pdf


def preprocess_pdf_fallback(
    pdf_path: Path,
    outdir: Path,
    *,
    start_page: int | None = None,
    end_page: int | None = None,
    asset_prefix: str = "",
    output_name: str = "source.md",
) -> Path:
    """Fallback: pymupdf light extraction. Page range uses 1-based inclusive pages."""
    try:
        import fitz  # pymupdf
    except ImportError as e:
        raise ImportError("pymupdf not installed. Run: pip install pymupdf") from e

    assets_dir = outdir / "assets"
    if asset_prefix:
        assets_dir = assets_dir / asset_prefix
    assets_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    chunks = []
    try:
        first = start_page or 1
        last = end_page or doc.page_count
        for page_no in range(first, last + 1):
            page = doc[page_no - 1]
            text = page.get_text("text")
            chunks.append(text)
            for img_idx, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    img_path = assets_dir / f"page{page_no}_img{img_idx + 1}.png"
                    pix.save(str(img_path))
                    rel = img_path.relative_to(outdir)
                    chunks.append(f"\n![figure]({rel.as_posix()})\n")
                    pix = None
                except Exception:
                    pass
    finally:
        doc.close()

    md_path = outdir / output_name
    md_path.write_text(_clean_text("\n\n".join(chunks)), encoding="utf-8")
    return md_path


def _read_chunk_status(chunk_dir: Path) -> dict:
    p = chunk_dir / "status.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_chunk_status(chunk_dir: Path, status: dict) -> None:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(status)
    source_md = chunk_dir / "source.md"
    payload["updated_at"] = _now_iso()
    payload["source_md_exists"] = source_md.exists()
    payload["source_md_size"] = source_md.stat().st_size if source_md.exists() else 0
    payload["chunk_pdf_exists"] = any(p.is_file() and p.suffix.lower() == ".pdf" for p in chunk_dir.iterdir())
    _write_json(chunk_dir / "status.json", payload)


def _build_progress_payload(plan: list[dict], todo_ids: list[str] | None = None) -> dict:
    chunks = []
    completed = 0
    failed = 0
    running = 0
    pending = 0
    for item in plan:
        chunk_dir = item["chunk_dir"]
        status = _read_chunk_status(chunk_dir)
        source_md = chunk_dir / "source.md"
        has_source_md = source_md.exists() and source_md.stat().st_size > 0
        state = "completed" if has_source_md else (status.get("state") or "pending")
        engine = status.get("engine") or ("marker" if has_source_md else None)
        if state == "completed":
            completed += 1
        elif state == "failed":
            failed += 1
        elif state == "running":
            running += 1
        else:
            pending += 1
        chunks.append({
            "chunk_id": item["chunk_id"],
            "pages": [item["start"], item["end"]],
            "state": state,
            "engine": engine,
            "error": status.get("error"),
            "updated_at": status.get("updated_at"),
        })
    return {
        "updated_at": _now_iso(),
        "total": len(plan),
        "todo": todo_ids or [],
        "completed": completed,
        "failed": failed,
        "running": running,
        "pending": pending,
        "chunks": chunks,
    }


def _write_progress_file(chunks_dir: Path, plan: list[dict], todo_ids: list[str] | None = None) -> None:
    _write_json(chunks_dir / "progress.json", _build_progress_payload(plan, todo_ids))


def _summarize_progress(payload: dict) -> str:
    active = [
        f"{chunk['chunk_id']}:{chunk.get('engine') or '-'}"
        for chunk in payload.get("chunks", [])
        if chunk.get("state") == "running"
    ]
    parts = [
        f"total={payload.get('total', 0)}",
        f"completed={payload.get('completed', 0)}",
        f"running={payload.get('running', 0)}",
        f"pending={payload.get('pending', 0)}",
        f"failed={payload.get('failed', 0)}",
    ]
    if active:
        parts.append(f"active={','.join(active[:5])}")
        if len(active) > 5:
            parts.append(f"active_more={len(active) - 5}")
    return " ".join(parts)


def _start_chunk_progress_heartbeat(
    chunks_dir: Path,
    plan: list[dict],
    todo_ids_getter: Callable[[], list[str]],
    stop_event: Event,
    progress_lock: Lock,
    *,
    progress_interval: int = 30,
) -> Thread:
    """父进程定期汇总所有分块状态，避免大 PDF 处理时控制台长时间无输出。"""
    interval = max(5, int(progress_interval or 30))
    started = time.monotonic()

    def loop() -> None:
        while not stop_event.wait(interval):
            with progress_lock:
                todo_ids = todo_ids_getter()
                payload = _build_progress_payload(plan, todo_ids)
                payload["elapsed_seconds"] = int(time.monotonic() - started)
                payload["progress_interval_seconds"] = interval
                _write_json(chunks_dir / "progress.json", payload)
            print(
                f"[preprocess] chunk heartbeat elapsed={payload['elapsed_seconds']}s "
                f"{_summarize_progress(payload)} progress={chunks_dir / 'progress.json'}",
                flush=True,
            )

    thread = Thread(target=loop, name="preprocess-chunk-progress", daemon=True)
    thread.start()
    return thread


def _load_resume_plan_from_progress(chunks_dir: Path, total_pages: int) -> list[dict] | None:
    progress_path = chunks_dir / "progress.json"
    if not progress_path.exists():
        return None
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    raw_chunks = progress.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        return None

    plan: list[dict] = []
    for raw in raw_chunks:
        pages = raw.get("pages")
        chunk_id = raw.get("chunk_id")
        if (
            not chunk_id
            or not isinstance(pages, list)
            or len(pages) != 2
            or not isinstance(pages[0], int)
            or not isinstance(pages[1], int)
        ):
            return None
        start, end = pages
        if start < 1 or end < start or end > total_pages:
            return None
        chunk_dir = chunks_dir / chunk_id
        plan.append({
            "chunk_id": chunk_id,
            "start": start,
            "end": end,
            "chunk_dir": chunk_dir,
            "chunk_pdf": chunk_dir / f"{chunk_id}.pdf",
        })

    covered: set[int] = set()
    for item in plan:
        covered.update(range(item["start"], item["end"] + 1))
    if covered != set(range(1, total_pages + 1)):
        return None

    return sorted(plan, key=lambda item: item["start"])


def _adopt_completed_chunk_if_present(item: dict) -> bool:
    """父进程被中断时，Marker worker 可能已写出 source.md 但 status 仍停在 running。"""
    chunk_dir = item["chunk_dir"]
    chunk_md = chunk_dir / "source.md"
    if not chunk_md.exists() or chunk_md.stat().st_size == 0:
        return False

    status = _read_chunk_status(chunk_dir)
    if status.get("state") == "completed" and status.get("engine"):
        return True

    fixed = {
        **status,
        "chunk_id": item["chunk_id"],
        "start_page": item["start"],
        "end_page": item["end"],
        "engine": status.get("engine") or "marker",
        "state": "completed",
        "last_step": status.get("last_step") or "source_md_adopted",
        "error": None,
        "log_path": str(chunk_dir / "marker.log"),
    }
    if fixed["last_step"] in {"marker_running", "init", "pdf_ready"}:
        fixed["last_step"] = "source_md_adopted_after_interrupt"
    _write_chunk_status(chunk_dir, fixed)
    print(f"[preprocess] adopted completed chunk from existing source.md: {item['chunk_id']}", flush=True)
    return True


def _process_one_chunk(
    pdf_path_str: str,
    chunk_pdf_str: str,
    chunk_dir_str: str,
    chunk_id: str,
    start: int,
    end: int,
    chunk_timeout: int,
    chunk_fallback: str,
    progress_interval: int,
) -> dict:
    """在独立进程中处理单个分块：抽页 -> Marker -> 失败则 fallback。返回状态 dict。

    顶层函数以便被 ProcessPoolExecutor pickle。
    """
    pdf_path = Path(pdf_path_str)
    chunk_pdf = Path(chunk_pdf_str)
    chunk_dir = Path(chunk_dir_str)
    chunk_md = chunk_dir / "source.md"
    log_path = chunk_dir / "marker.log"
    started = time.monotonic()
    started_at = _now_iso()

    status = {
        "chunk_id": chunk_id,
        "pages": [start, end],
        "start_page": start,
        "end_page": end,
        "engine": None,
        "state": "running",
        "last_step": "init",
        "error": None,
        "started_at": started_at,
        "elapsed_seconds": 0,
        "pid": os.getpid(),
        "ts": int(time.time()),
        "chunk_pdf": str(chunk_pdf),
        "log_path": str(log_path),
        "heartbeat_path": str(chunk_dir / "heartbeat.json"),
        "progress_interval_seconds": max(5, int(progress_interval or 30)),
    }
    _write_chunk_status(chunk_dir, status)

    def touch_status(last_step: str | None = None) -> None:
        if last_step:
            status["last_step"] = last_step
        status["elapsed_seconds"] = int(time.monotonic() - started)
        _write_chunk_status(chunk_dir, status)

    try:
        if not chunk_pdf.exists():
            touch_status("extract_pdf_pages")
            _extract_pdf_pages(pdf_path, chunk_pdf, start, end)
        touch_status("pdf_ready")
        try:
            status["engine"] = "marker"
            touch_status("marker_running")
            _run_marker_subprocess(
                chunk_pdf,
                chunk_dir,
                chunk_timeout,
                chunk_id,
                progress_interval=progress_interval,
            )
            status["engine"] = "marker"
            status["state"] = "completed"
            status["last_step"] = "marker_done"
            status["error"] = None
        except Exception as e:
            status["error"] = f"{type(e).__name__}: {e}"
            if chunk_fallback == "fail":
                status["engine"] = "failed"
                status["state"] = "failed"
                touch_status("marker_failed")
                raise
            if chunk_fallback == "skip":
                print(f"[preprocess] marker failed for {chunk_id} ({e}); skipping chunk", file=sys.stderr, flush=True)
                chunk_md.write_text("", encoding="utf-8")
                status["engine"] = "skip"
                status["state"] = "completed"
                status["last_step"] = "skip_empty_chunk"
            else:
                print(f"[preprocess] marker failed for {chunk_id} ({e}); falling back to pymupdf", file=sys.stderr, flush=True)
                status["engine"] = "pymupdf"
                touch_status("fallback_running")
                preprocess_pdf_fallback(
                    pdf_path,
                    chunk_dir,
                    start_page=start,
                    end_page=end,
                )
                status["engine"] = "pymupdf"
                status["state"] = "completed"
                status["last_step"] = "fallback_done"
    except Exception:
        status["state"] = "failed"
        status["elapsed_seconds"] = int(time.monotonic() - started)
        if status.get("last_step") == "init":
            status["last_step"] = "failed_before_start"
        _write_chunk_status(chunk_dir, status)
        raise
    status["elapsed_seconds"] = int(time.monotonic() - started)
    _write_chunk_status(chunk_dir, status)
    return status


def preprocess_pdf_chunked(
    pdf_path: Path,
    outdir: Path,
    *,
    chunk_pages: int = 6,
    chunk_timeout: int = 900,
    chunk_fallback: str = "pymupdf",
    resume: bool = False,
    chunk_concurrency: int = 1,
    retry_fallback: bool = False,
    table_strategy: str = "image",
    progress_interval: int = 30,
) -> Path:
    """大 PDF 分块 Marker；单块失败时按策略 fallback。可并行多分块。

    resume=True：已有非空 source.md 的分块默认跳过，不重跑。
    retry_fallback=True：resume 模式下额外重跑之前 engine=pymupdf/skip/failed 的分块。
    chunk_concurrency>=2：使用进程池并行执行；每个 worker 独立加载 Marker 模型。
    """
    total_pages = _pdf_page_count(pdf_path)
    chunks_dir = outdir / "preprocess_chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    # 1. 规划所有分块。resume 时优先沿用旧 progress.json，避免默认 chunk_pages 调整后无法复用旧现场。
    plan = _load_resume_plan_from_progress(chunks_dir, total_pages) if resume else None
    if plan:
        print(f"[preprocess] resume: reuse previous chunk plan from progress.json ({len(plan)} chunks)", flush=True)
    else:
        plan = []
        for start in range(1, total_pages + 1, chunk_pages):
            end = min(start + chunk_pages - 1, total_pages)
            chunk_id = f"chunk_{((start - 1) // chunk_pages) + 1:03d}_p{start:03d}-p{end:03d}"
            chunk_dir = chunks_dir / chunk_id
            chunk_pdf = chunk_dir / f"{chunk_id}.pdf"
            plan.append({
                "chunk_id": chunk_id,
                "start": start,
                "end": end,
                "chunk_dir": chunk_dir,
                "chunk_pdf": chunk_pdf,
            })

    # 2. 筛出需要执行的分块
    todo: list[dict] = []
    for item in plan:
        chunk_dir = item["chunk_dir"]
        chunk_pdf = item["chunk_pdf"]
        chunk_md = chunk_dir / "source.md"

        completed_from_disk = _adopt_completed_chunk_if_present(item)
        status = _read_chunk_status(chunk_dir)
        engine = status.get("engine")
        state = status.get("state")

        if completed_from_disk:
            if retry_fallback and engine in {"pymupdf", "skip", "failed"}:
                print(f"[preprocess] retry-fallback: rerun {item['chunk_id']} (was engine={engine})", flush=True)
            else:
                print(f"[preprocess] reuse completed chunk: {item['chunk_id']} engine={engine or 'unknown'}", flush=True)
                continue

        if resume and chunk_md.exists() and chunk_md.stat().st_size > 0:
            if retry_fallback and engine in {"pymupdf", "skip", "failed"}:
                print(f"[preprocess] retry-fallback: rerun {item['chunk_id']} (was engine={engine})", flush=True)
            else:
                print(f"[preprocess] resume chunk exists: {item['chunk_id']} engine={engine or 'unknown'}", flush=True)
                continue

        if resume and (chunk_pdf.exists() or status):
            print(
                f"[preprocess] resume interrupted chunk: {item['chunk_id']} "
                f"state={state or 'unknown'} engine={engine or 'unknown'} pdf_exists={chunk_pdf.exists()}"
            )
            chunk_dir.mkdir(parents=True, exist_ok=True)
            todo.append(item)
            continue

        # 需要（重新）执行：清空目录
        if chunk_dir.exists():
            shutil.rmtree(chunk_dir)
        chunk_dir.mkdir(parents=True, exist_ok=True)
        todo.append(item)

    todo_ids = [item["chunk_id"] for item in todo]
    _write_progress_file(chunks_dir, plan, todo_ids)

    # 3. 执行：并行 or 串行
    concurrency = max(1, int(chunk_concurrency))
    if todo:
        print(f"[preprocess] chunked marker: total={len(plan)} todo={len(todo)} "
              f"chunk_pages={chunk_pages} chunk_timeout={chunk_timeout}s concurrency={concurrency} "
              f"progress_interval={progress_interval}s")
        print(f"[preprocess] chunk progress file: {chunks_dir / 'progress.json'}", flush=True)
        print(f"[preprocess] chunk logs: {chunks_dir}/*/marker.log", flush=True)
    else:
        print(f"[preprocess] chunked marker: nothing to do, using existing {len(plan)} chunks")

    progress_lock = Lock()
    running_todo_ids = set(todo_ids)
    stop_heartbeat = Event()
    heartbeat_thread: Thread | None = None

    def current_todo_ids() -> list[str]:
        return sorted(running_todo_ids)

    def mark_done_and_write_progress(chunk_id: str) -> None:
        with progress_lock:
            running_todo_ids.discard(chunk_id)
            _write_progress_file(chunks_dir, plan, current_todo_ids())

    def write_progress(ids: list[str] | None = None) -> None:
        with progress_lock:
            _write_progress_file(chunks_dir, plan, ids if ids is not None else current_todo_ids())

    try:
        if todo:
            heartbeat_thread = _start_chunk_progress_heartbeat(
                chunks_dir,
                plan,
                current_todo_ids,
                stop_heartbeat,
                progress_lock,
                progress_interval=progress_interval,
            )

        if todo and concurrency > 1:
            with ProcessPoolExecutor(max_workers=concurrency) as pool:
                futures = {}
                for item in todo:
                    fut = pool.submit(
                        _process_one_chunk,
                        str(pdf_path),
                        str(item["chunk_pdf"]),
                        str(item["chunk_dir"]),
                        item["chunk_id"],
                        item["start"],
                        item["end"],
                        chunk_timeout,
                        chunk_fallback,
                        progress_interval,
                    )
                    futures[fut] = item["chunk_id"]
                done = 0
                for fut in as_completed(futures):
                    cid = futures[fut]
                    done += 1
                    try:
                        st = fut.result()
                        print(f"[preprocess] chunk done [{done}/{len(todo)}] {cid} engine={st.get('engine')}", flush=True)
                    except Exception as e:
                        print(f"[preprocess] chunk FAILED [{done}/{len(todo)}] {cid}: {e}", file=sys.stderr, flush=True)
                        if chunk_fallback == "fail":
                            raise
                    finally:
                        mark_done_and_write_progress(cid)
        else:
            for idx, item in enumerate(todo, start=1):
                try:
                    st = _process_one_chunk(
                        str(pdf_path),
                        str(item["chunk_pdf"]),
                        str(item["chunk_dir"]),
                        item["chunk_id"],
                        item["start"],
                        item["end"],
                        chunk_timeout,
                        chunk_fallback,
                        progress_interval,
                    )
                    print(f"[preprocess] chunk done [{idx}/{len(todo)}] {item['chunk_id']} engine={st.get('engine')}", flush=True)
                except Exception as e:
                    print(f"[preprocess] chunk FAILED [{idx}/{len(todo)}] {item['chunk_id']}: {e}", file=sys.stderr, flush=True)
                    if chunk_fallback == "fail":
                        raise
                finally:
                    mark_done_and_write_progress(item["chunk_id"])
    finally:
        stop_heartbeat.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=2)

    write_progress(ids=[])

    # 4. 合并所有分块输出
    combined = []
    all_tables: list[TableImage] = []
    for item in plan:
        chunk_dir = item["chunk_dir"]
        chunk_id = item["chunk_id"]
        chunk_md = chunk_dir / "source.md"
        _copy_assets(chunk_dir / "assets", outdir / "assets" / chunk_id)
        text = chunk_md.read_text(encoding="utf-8") if chunk_md.exists() else ""
        text = _prefix_markdown_image_links(text, chunk_id)

        if table_strategy == "image" and item["chunk_pdf"].exists():
            try:
                chunk_tables = extract_tables_as_images(
                    item["chunk_pdf"],
                    out_image_dir=outdir / "assets" / chunk_id / "tables",
                    outdir_root=outdir,
                    page_offset=item["start"] - 1,
                )
                if chunk_tables:
                    print(f"[preprocess] {chunk_id}: captured {len(chunk_tables)} tables as PNG")
                    text = strip_markdown_tables(text)
                    all_tables.extend(chunk_tables)
            except Exception as e:
                print(f"[preprocess] table extraction failed for {chunk_id} ({e})",
                      file=sys.stderr)

        combined.append(
            f"<!-- PDF_CHUNK {chunk_id} pages={item['start']}-{item['end']} -->\n\n{text.strip()}\n"
        )

    merged = "\n\n".join(combined)
    if table_strategy == "image" and all_tables:
        # 分块模式下已经在各自位置 strip 掉了乱 Markdown 表格；
        # 统一在文末追加所有截图，避免穿插到 PDF_CHUNK 注释之间。
        merged = _inject_table_images(merged, all_tables, remove_existing_tables=False)

    final_md = outdir / "source.md"
    final_md.write_text(_clean_text(merged), encoding="utf-8")
    return final_md


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


def preprocess(
    input_path: Path,
    kind: str,
    outdir: Path,
    *,
    pdf_engine: str = "auto",
    marker_timeout: int = 1800,
    large_pdf_pages: int = 20,
    pdf_chunk_pages: int = 6,
    chunk_timeout: int = 900,
    chunk_fallback: str = "pymupdf",
    resume: bool = False,
    chunk_concurrency: int = 1,
    retry_fallback: bool = False,
    table_strategy: str = "image",
    progress_interval: int = 30,
) -> Path:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if kind == "markdown":
        dst = outdir / "source.md"
        shutil.copyfile(input_path, dst)
        return dst

    if kind in ("html", "arxiv_html"):
        return preprocess_html(input_path, outdir)

    if kind == "pdf":
        if pdf_engine == "pymupdf":
            md = preprocess_pdf_fallback(input_path, outdir)
            if table_strategy == "image":
                _apply_table_images_to_existing_md(input_path, outdir, md)
            return md
        if pdf_engine == "marker":
            return preprocess_pdf_marker(
                input_path, outdir, timeout=marker_timeout,
                table_strategy=table_strategy,
                progress_interval=progress_interval,
            )
        if pdf_engine == "marker-chunked":
            return preprocess_pdf_chunked(
                input_path,
                outdir,
                chunk_pages=pdf_chunk_pages,
                chunk_timeout=chunk_timeout,
                chunk_fallback=chunk_fallback,
                resume=resume,
                chunk_concurrency=chunk_concurrency,
                retry_fallback=retry_fallback,
                table_strategy=table_strategy,
                progress_interval=progress_interval,
            )

        pages = _pdf_page_count(input_path)
        print(f"[preprocess] pdf pages={pages} engine=auto")
        if pages > large_pdf_pages:
            print(
                f"[preprocess] pages>{large_pdf_pages}; using chunked marker "
                f"chunk_pages={pdf_chunk_pages} chunk_timeout={chunk_timeout}s "
                f"concurrency={chunk_concurrency} progress_interval={progress_interval}s"
            )
            return preprocess_pdf_chunked(
                input_path,
                outdir,
                chunk_pages=pdf_chunk_pages,
                chunk_timeout=chunk_timeout,
                chunk_fallback=chunk_fallback,
                resume=resume,
                chunk_concurrency=chunk_concurrency,
                retry_fallback=retry_fallback,
                table_strategy=table_strategy,
                progress_interval=progress_interval,
            )

        try:
            return preprocess_pdf_marker(
                input_path, outdir, timeout=marker_timeout,
                table_strategy=table_strategy,
                progress_interval=progress_interval,
            )
        except Exception as e:
            print(f"[preprocess] marker failed ({e}); falling back to pymupdf", file=sys.stderr)
            md = preprocess_pdf_fallback(input_path, outdir)
            if table_strategy == "image":
                _apply_table_images_to_existing_md(input_path, outdir, md)
            return md

    raise ValueError(f"Unknown kind: {kind}")


def _apply_table_images_to_existing_md(pdf_path: Path, outdir: Path, md_path: Path) -> None:
    """在 pymupdf fallback 之后补截表格图片并改写 source.md。"""
    try:
        tables = extract_tables_as_images(
            pdf_path,
            out_image_dir=outdir / "assets" / "tables",
            outdir_root=outdir,
        )
    except Exception as e:
        print(f"[preprocess] table extraction failed ({e})", file=sys.stderr)
        return
    if not tables:
        return
    print(f"[preprocess] table_strategy=image: captured {len(tables)} tables as PNG (fallback)")
    text = md_path.read_text(encoding="utf-8")
    text = _inject_table_images(text, tables)
    md_path.write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--kind", choices=["pdf", "html", "arxiv_html", "markdown"])
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--pdf-engine", choices=["auto", "marker", "pymupdf", "marker-chunked"], default="auto")
    ap.add_argument("--marker-timeout", type=int, default=1800)
    ap.add_argument("--large-pdf-pages", type=int, default=20)
    ap.add_argument("--pdf-chunk-pages", type=int, default=6)
    ap.add_argument("--chunk-timeout", type=int, default=900,
                    help="base timeout seconds for each Marker chunk; active OCR logs may extend up to 3x")
    ap.add_argument("--chunk-fallback", choices=["pymupdf", "skip", "fail"], default="pymupdf")
    ap.add_argument("--chunk-concurrency", type=int, default=1,
                    help="parallel workers for chunked marker; each worker loads its own model")
    ap.add_argument("--retry-fallback", action="store_true",
                    help="with --resume, rerun chunks whose previous engine was pymupdf/skip/failed")
    ap.add_argument("--progress-interval", type=int, default=30,
                    help="seconds between progress heartbeats for Marker and chunked PDF preprocessing")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--table-strategy", choices=["image", "markdown"], default="image",
                    help="image: crop table regions from PDF as PNG and replace Markdown tables; "
                         "markdown: keep Marker's Markdown tables as-is")
    ap.add_argument("--marker-worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.marker_worker:
        path = _write_marker_output(Path(args.input), Path(args.outdir))
    else:
        if not args.kind:
            ap.error("--kind is required unless --marker-worker is used")
        path = preprocess(
            Path(args.input),
            args.kind,
            Path(args.outdir),
            pdf_engine=args.pdf_engine,
            marker_timeout=args.marker_timeout,
            large_pdf_pages=args.large_pdf_pages,
            pdf_chunk_pages=args.pdf_chunk_pages,
            chunk_timeout=args.chunk_timeout,
            chunk_fallback=args.chunk_fallback,
            resume=args.resume,
            chunk_concurrency=args.chunk_concurrency,
            retry_fallback=args.retry_fallback,
            table_strategy=args.table_strategy,
            progress_interval=args.progress_interval,
        )
    print(path)


if __name__ == "__main__":
    main()
