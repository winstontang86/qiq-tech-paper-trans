# 学术翻译风格指南（信达雅）

## 一、总原则

- **信**：忠实原文语义与事实，不增不减，不擅自摘要。
- **达**：行文通顺自然，避免直译腔（"被…所…"、"作为…的…"）。
- **雅**：学术书面语，第三人称视角；简洁克制，不煽情不夸张。

## 二、语气与人称

- 英文 "We propose / We show" → 中文优先省略主语："本文提出…"、"结果表明…"。
- 避免口语："我们觉得"、"其实"、"感觉"。
- 避免营销腔："革命性"、"颠覆"、"震撼"。
- 避免过度翻译：`significant improvement` 译"显著提升"而非"意义重大的突破"。

## 三、句式调整

- 英文长句拆为中文短句，优先短句 + 逗号；中文一般不超过 40 字/句。
- 被动语态 `is/are + 过去分词` 多数转主动语态；仅当主语不明确时保留被动。
- 定语从句前置：`the model that achieves SOTA` → "达到最先进水平的模型"。
- 连词补全：英文常省略 "and then / furthermore"，中文按逻辑补"此外"、"进而"、"随后"。

## 四、标点规范

| 场景 | 规则 |
|---|---|
| 中文正文 | 全角标点：，。；：？！""''（） |
| 中英混排 | 中英文之间加半角空格：`使用 Transformer 架构` |
| 英文术语首次出现 | `中文（English）`，括号用中文全角 |
| 数字 + 单位 | 数字和单位间不加空格：`10ms`、`1.5GB` |
| 百分号 | 半角：`3.5%` |
| 引号 | 中文用""和''；英文保留 "" 和 '' |

## 五、术语处理

1. **模型名、数据集名、人名、机构名**：保留英文。
   - GPT-4、LLaMA-3、ImageNet、Hugging Face、OpenAI、MIT
2. **已广泛中文化的术语**：用中文。
   - attention → 注意力；transformer → Transformer（保留，专有化）
3. **缩写**：首次出现"中文全称（缩写）"，后续只用缩写。
   - "专家混合（MoE）"、"检索增强生成（RAG）"
4. **动词性术语**：
   - fine-tune → 微调；pre-train → 预训练；distill → 蒸馏

## 六、常见陷阱

| 英文 | 易错 | 正确 |
|---|---|---|
| significant | 意义重大 | 显著 |
| novel | 新奇的 | 新颖 / 本文提出的 |
| state-of-the-art | 艺术状态 | 最先进 / SOTA |
| robust | 鲁莽 | 鲁棒 |
| ablation | 消融（医学） | 消融（ML 语境） |
| embedding | 嵌入物 | 嵌入（表示） |
| training | 培训 | 训练 |
| evaluation | 评价 | 评测 / 评估 |
| ground truth | 地面真相 | 真实标签 / 真值 |
| end-to-end | 点对点 | 端到端 |

## 七、图表、公式、代码

- **图注 / 表注**：翻译为"图 X：..." / "表 X：..."；`Figure 3` → "图 3"；`Table 2` → "表 2"。
- **公式**：不译；公式内的 `\text{loss}`、`\mathrm{...}` 保留。
- **变量符号**：不译，保留原符号。
- **代码注释**：仅翻译英文自然语言注释；变量名、函数名、关键字不动。
- **算法伪代码**：关键字 `for / while / if / return` 不译；其余自然语言译中文。

## 八、参考文献

- 默认不译。保留原始作者名、期刊/会议名、年份、DOI、arXiv ID。
- 致谢、基金编号（Grant No. xxx）、作者机构/邮箱：保留原文。

## 九、数字、日期、单位

- 数字保留原格式：`1,024`、`3.5%`、`10^-3`、`1e-4`。
- 年份、日期保留：`2024`、`March 15, 2024`。
- 单位不译：`ms / s / kg / GB / FLOPs`。

## 十、章节标题

- 常见章节标准译法：

| English | 中文 |
|---|---|
| Abstract | 摘要 |
| Introduction | 引言 |
| Related Work | 相关工作 |
| Background | 背景 |
| Method / Methodology / Approach | 方法 |
| Experiments / Evaluation | 实验 |
| Results | 结果 |
| Discussion | 讨论 |
| Ablation Study | 消融实验 |
| Limitations | 局限性 |
| Conclusion | 结论 |
| Acknowledgments | 致谢 |
| References / Bibliography | 参考文献 |
| Appendix | 附录 |
