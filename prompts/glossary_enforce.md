# 术语表注入规则

## 来源优先级

1. **用户自定义**：`--glossary /path/to/glossary.json`（最高优先，覆盖内置）
2. **内置术语表**：`references/glossary_ai_ml.json`

## JSON 格式

```json
{
  "attention": "注意力机制",
  "transformer": "Transformer",
  "fine-tuning": "微调",
  "chain-of-thought": "思维链"
}
```

键为英文原文（不区分大小写匹配），值为中文译法。若值与键相同（例如 `"Transformer": "Transformer"`），表示保留英文不译。

## 注入策略

`translate.py` 对每个段落：

1. 扫描 `current + prev + next` 窗口内出现的术语（忽略大小写、忽略复数形式 `-s`/`-es`、忽略连字符变体）。
2. 仅注入**出现过的**术语，避免 prompt 过长。
3. 按行拼接为 `English -> 中文` 格式塞入 `<glossary>`。

## 首次出现的格式

- LLM 按 prompt 规则处理："中文（English）"首次出现后，后续只用中文。
- `postprocess.py` 不做二次干预，交由 LLM 的上下文感知处理（三段窗口已提供足够前文信息判断是否为首次）。

## 术语收集（v2 能力，v1 不开启）

v2 计划：从历史译文中自动抽取"中文（English）"对照，回灌至内置术语表作为扩充候选。v1 手动维护。
