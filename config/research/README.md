# 综合调研文件

每家公司一个 JSON 文件，文件名就是股票代码（海外股票用 Yahoo 代码，如 `067310.KQ.json`）。看板会把这里的内容显示在评估卡的"综合评估"区块里。

## 规则

1. **每条判断都要有出处。** 优先用一手资料：10-K / 10-Q / 年报、财报新闻稿、电话会、公司投资者关系页、监管公告。找不到出处就写 `unknown`，不要凭印象写。
2. **数字要带时间点**，比如"2025 财年最大客户占营收 23%"。
3. **发现互相矛盾的说法要记下来**，写进 `view` 或 `open_questions`，不要只留对自己有利的那一条。
4. **不写买卖建议和目标价。** 只描述事实和判断依据。
5. `status` 为 `draft` 表示是未经人工审核的调研草稿；人工核对后改成 `reviewed`，并填写 `reviewed_by`。

## 格式

```json
{
  "ticker": "SITM",
  "name": "SiTime Corporation",
  "as_of": "2026-10-21",
  "status": "draft",
  "author": "谁写的",
  "reviewed_by": "",
  "summary": "一两句话的综合判断，说清依据",
  "dimensions": {
    "business":   {"rating": "positive | neutral | negative | unknown", "view": "判断", "evidence": ["事实"], "sources": [{"title": "", "url": "", "date": "YYYY-MM-DD", "tier": "primary | authoritative | general"}]},
    "customers":  {"...": "同上"},
    "management": {"...": "同上"},
    "industry":   {"...": "同上"},
    "product":    {"...": "同上"},
    "risks":      {"...": "同上"}
  },
  "bull": ["综合层面的机会"],
  "bear": ["综合层面的风险"],
  "open_questions": ["还没查清的问题"]
}
```

六个维度：

| 键 | 维度 | 要回答的问题 |
|---|---|---|
| business | 业务与护城河 | 卖什么、卖给谁、靠什么赚钱、对手为什么拦不住 |
| customers | 客户与集中度 | 最大客户占多少营收，对单一客户或渠道依赖有多大 |
| management | 管理层 | 关键高管任期与背景、持股、近期人事变动、执行记录 |
| industry | 行业形势 | 所在细分市场的需求驱动、周期位置、竞争格局 |
| product | 产品与创新 | 新产品节奏、研发投入、技术位置 |
| risks | 风险 | 公司披露的主要风险、诉讼、监管、财务风险 |
