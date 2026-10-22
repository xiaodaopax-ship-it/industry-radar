# A 股综合调研文件

每家公司一个 JSON 文件，文件名就是 6 位股票代码（如 `300191.json`）。`python -m cn build` 会把这里的内容显示在 A 股看板评估卡的"定性六维"区块里；没有文件时六个维度都显示"待研究"，只附上东方财富 F10 的原文摘录。

格式与美股的 `config/research/README.md` 一致，只是用 `code` 代替 `ticker`。本目录**不放示例数据**，每个文件都必须是真实调研的结果。

## 规则

1. **每条判断都要有出处。** 优先用一手资料：定期报告（年报 / 半年报 / 季报）、招股说明书、交易所公告（巨潮资讯）、投资者关系活动记录表、业绩说明会、交易所问询函及回复。找不到出处就写 `unknown`，不要凭印象写。
2. **数字要带时间点**，比如"2025 年报前五名客户销售额占 41.2%"。
3. **发现互相矛盾的说法要记下来**，写进 `view` 或 `open_questions`，不要只留对自己有利的那一条。
4. **不写买卖建议和目标价，不评价政策。** 只描述公司层面的事实和判断依据。
5. `status` 为 `draft` 表示是未经人工审核的调研草稿；人工核对后改成 `reviewed`，并填写 `reviewed_by`。
6. `code` 必须与文件名一致，否则构建时忽略该文件并打印 WARNING。

## 格式

```json
{
  "code": "300191",
  "name": "公司简称",
  "as_of": "YYYY-MM-DD",
  "status": "draft",
  "author": "谁写的",
  "reviewed_by": "",
  "summary": "一两句话的综合判断，说清依据",
  "dimensions": {
    "business":   {"rating": "positive | neutral | negative | unknown", "view": "判断", "evidence": ["事实"], "sources": [{"title": "", "url": "https://...", "date": "YYYY-MM-DD", "tier": "primary | authoritative | general"}]},
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

省略某个维度时，该维度显示"待研究"。`sources[].url` 只接受 `http://` / `https://` 链接。

六个维度：

| 键 | 维度 | 要回答的问题 |
|---|---|---|
| business | 业务与护城河 | 卖什么、卖给谁、靠什么赚钱、对手为什么拦不住 |
| customers | 客户与集中度 | 前五名客户占营收多少，对单一客户或渠道依赖有多大 |
| management | 管理层 | 关键高管任期与背景、持股、近期人事变动、执行记录 |
| industry | 行业形势 | 所在细分市场的需求驱动、周期位置、竞争格局 |
| product | 产品与创新 | 新产品节奏、研发投入、技术位置 |
| risks | 风险 | 公司披露的主要风险、诉讼、问询函事项、财务风险 |
