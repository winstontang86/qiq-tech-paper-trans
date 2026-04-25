---
name: technical-paper-translation
version: 0.1.0
description: |
  英文技术论文翻译为中文（信达雅学术风格）。支持本地 PDF 文件与 URL 输入（arXiv
  链接优先抓取 HTML 版本）。针对 AI/ML 论文深度优化，兼容通用技术论文。采用滑动
  窗口三段法保证上下文连贯、术语一致；阻断级质检确保段落、图片、表格、公式、代
  码、引用均完整保留。默认输出 Markdown。
  触发词：翻译论文、翻译技术论文、翻译学术论文、翻译 arxiv、arxiv 翻译、论文汉化、
  paper translation、translate paper、英译中论文、paper to Chinese、学术翻译。
location: user
entrypoint: scripts/run.py
---

# technical-paper-translation

英文技术论文翻译为中文（信达雅学术风格）的 skill。

## 何时使用此 skill

当用户提出以下类型请求时，**立即加载并使用此 skill**：

- 翻译一篇 PDF 论文 / arXiv 论文 / 技术论文
- 提供论文 URL（arxiv.org / openreview / ACL Anthology 等）并要求翻译
- 对论文做中文化 / 汉化处理
- 需要保留图表、公式、引用编号的严格学术翻译
- 类似需求："把这篇 paper 翻一下"、"帮我译成中文"、"这篇 arxiv 能不能汉化"

## 核心原则

1. **信达雅 + 忠实**：学术语气，禁止擅自摘要、省略、补全。
2. **结构保真**：标题层级、图片、表格、公式（LaTeX）、代码块、引用编号 `[12]`、参考文献均原样保留。
3. **滑动窗口三段法**：翻译时输入 `previous + current + next`，仅译 `current`，保证代词指代与术语一致。
4. **术语一致**：内置 AI/ML 术语表 + 支持用户自定义 `glossary.json` 覆盖。
5. **阻断级质检**：段落对齐、图片/表格/公式/代码/引用数量一致、长度比正常、无摘要性短语；任一不通过则终止并报告，除非用户明确 `--force` 跳过。

## 输入

- **本地 PDF**：`/path/to/paper.pdf`
- **URL**：
  - arXiv（`arxiv.org/abs/xxxx` 或 `arxiv.org/pdf/xxxx`）→ 自动改走 HTML 版（ar5iv / arxiv.org/html）质量更高
  - 其他 PDF 直链 → 下载后走 PDF 流程
  - OpenReview / ACL Anthology HTML 页 → 直接 HTML 解析

## 输出

- `<paper_stem>.zh.md` —— 中文译文
- `<paper_stem>.qa.md` —— 质检报告
- `<paper_stem>.assets/` —— 抽出的图片
- 可选 `<paper_stem>.bilingual.md` —— 双语对照（`--bilingual` 启用）

## 执行流程

```
输入 (PDF / URL)
  → fetch.py       下载（URL 情况）
  → preprocess.py  PDF/HTML → 结构化 Markdown (Marker 优先，回退 pdfplumber+pymupdf)
  → segment.py     分段 + 锚点化（锁定公式/代码/表格/图片/引用）
  → translate.py   滑动窗口三段法翻译 + 术语表 + 断点续译
  → postprocess.py 回贴锚点 + 中英排版规范化
  → qa_report.py   阻断级质检
  → 输出
```

## 使用方式

LLM 助手在满足触发条件后，按如下方式调用：

```bash
# 检测 Python
which python3

# 本地 PDF
python3 ~/.workbuddy/skills/technical-paper-translation/scripts/run.py \
  --input /path/to/paper.pdf \
  --outdir /path/to/output

# URL 输入
python3 ~/.workbuddy/skills/technical-paper-translation/scripts/run.py \
  --input https://arxiv.org/abs/2403.xxxxx \
  --outdir /path/to/output

# 可选参数
--bilingual          同时输出双语对照 Markdown
--glossary FILE      用户自定义术语表（覆盖内置）
--force              跳过阻断级质检（仅在用户明确要求时使用）
--resume             断点续译
```

## LLM 翻译调用约定（重要）

本 skill 的 `translate.py` 本身不直接调用 LLM API，而是把每段的"三段窗口 prompt"写入中间文件，由外层 Agent（即当前对话的 LLM）读取并逐段产出译文，再回写。这样可以复用当前 WorkBuddy 会话的模型，无需管理 API key。

具体交互协议见 `prompts/translate_segment.md`。

## 依赖

- Python 3.10+（推荐系统已有的 3.12）
- 首次运行时按需 `pip install -r requirements.txt`
- Marker 会在首次 PDF 解析时下载 ~1–2GB 模型权重

## 版本

v0.1.0（2026-04-25）—— 初始版本，v1。
