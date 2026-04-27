# qiq-tech-paper-trans

`qiq-tech-paper-trans` 是一个用于将英文技术论文翻译为中文的 skill，重点面向 AI/ML 与通用技术论文，目标是在保留论文结构的前提下生成忠实、流畅、适合阅读的中文译文。

## 期望解决的问题

- 有些技术论文的排版比较特殊，直接给大模型翻译之后内容有点乱。
- 翻译之后把原文中的图和表格会有缺失的情况。
- 翻译大段内容的时候，会出现不忠于原文进行翻译，中间自行简要概括。
- PDF 解析结果不稳定，复杂表格容易在 Markdown 或 Word 中破坏排版。

## 主要功能

- 支持本地 PDF 与论文 URL 输入，arXiv 链接会优先使用 HTML 版本。
- 保留标题层级、图片、表格、公式、代码块和引用编号。
- 采用滑动窗口翻译单元，兼顾上下文连贯与术语一致。
- 默认从 `References` / `Bibliography` 开始截断，不翻译参考文献及其后内容。
- 表格默认以图片方式保留，减少复杂表格乱码和排版损坏。
- 提供质检报告，发现正文、图表、公式、引用等缺失时阻断输出。
- 可选输出双语对照 Markdown 与 Word 文档。

## 基本使用

```bash
python3 scripts/run.py \
  --input /path/to/paper.pdf \
  --outdir /path/to/output
```

也可以输入论文 URL：

```bash
python3 scripts/run.py \
  --input https://arxiv.org/abs/2403.xxxxx \
  --outdir /path/to/output
```

常用选项：

- `--bilingual`：额外输出双语对照 Markdown。
- `--export-docx`：额外导出 Word 文档，需要本机安装 `pandoc`。
- `--resume`：断点续译，复用已有中间产物。
- `--force`：跳过阻断级质检，仅建议在明确知道风险时使用。

更完整的运行协议与参数说明见 `SKILL.md`。
