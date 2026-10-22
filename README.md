# Industry Radar

> Sector → segment → leaders & emerging companies → research shortlist. A single-file HTML dashboard built from SEC EDGAR and Yahoo Finance data, inspired by Peter Lynch's "fast grower" checklist. **Research tool, not investment advice.**

一个按"行业整体分析 → 细分领域拆解 → 龙头与新兴公司 → 研究机会清单"组织的看板。用 SEC EDGAR 和 Yahoo Finance 的公开数据，按彼得·林奇"快速增长股"的思路筛选，最后生成一个离线可打开的 HTML 页面。

**这是研究工具，不是投资建议。** 页面上的"机会""风险""结论"都是按数据规则自动生成的，用来决定先研究谁，不能代替对业务、竞争和管理层的判断。

## 看板结构

| 步骤 | 内容 |
|---|---|
| ⓪ 全市场热度 | 全部美股申报公司按 SIC 大类 / 4 位行业分组：营收加权增速、增速中位数、增速 ≥20% 占比、加速占比、利润率变化；标出本工具已覆盖的行业，用来发现下一个值得配置的行业 |
| ① 行业总览 | 行业判断与需求证据、行业 ETF 对比大盘、细分环节市值树图 |
| ② 细分拆解 | 环节"增长 × 估值"象限图、环节一览、环节内公司市值占比树图 |
| ③ 龙头分析 | 每个环节手动配置的龙头（可跨美股、台股、日股、韩股）：增长、利润率、估值、EPS 实际与预期、下季预期、一年走势 |
| ④ 新兴机会 | 增速 × 毛利率气泡图、研究优先池 Top 5、候选表（可按环节、市场、股价区间、结论筛选）、评估卡 |
| ⑤ 机会清单 | 按环节汇总"先研究谁"，全部行业的研究优先池，可导出 CSV |

**评估卡**（龙头和新兴公司共用同一种版式）：一句话介绍 → 综合结论（数据面 + 综合面）→ 增长 / 盈利 / 估值 / 财报 vs 预期四个指标块 → 机会与风险（业务层面、数据层面分开列）→ 六维综合评估 → 因子雷达图、52 周价格区间与目标价、林奇清单 → 下一步要验证的问题和研究笔记。

**六维综合评估**：业务与护城河、客户与集中度、管理层、行业形势、产品与创新、风险。每个维度有两层：

- **调研判断**：来自 `config/research/<代码>.json`，每条判断都附出处（年报、季报、财报新闻稿、电话会、监管公告）。`status` 为 `draft` 的是未经人工审核的调研草稿，页面会明确标出。
- **申报原文摘录**：`python -m radar research` 自动从 SEC 申报里摘出业务描述、提到客户占营收比例的原句、竞争段落、风险因素小标题、近一年高管变动公告（8-K 5.02），并从 XBRL 算出研发费用占营收比例；海外公司摘 Yahoo 的业务描述和高管名单。这一层只摘原文，不做判断。

仓库里已经附带 20 家研究优先池公司的调研草稿（美股 16 家，台湾、韩国 4 家），用作示例；它们是某一时点的草稿，使用前请自行核对。

内置八个行业：AI 基础设施与半导体、医药与医疗、能源与电力、软件与网络安全、工程建设与专业承包、工业机械与航空零部件、支付与金融软件、券商、交易平台与放贷科技。最后一个行业用单独的金融类评估口径（见下文"方法"）；银行、保险和资产管理公司不在覆盖范围。行业、环节、龙头、门槛都在 `config/` 里配置，可以自行增加。

`deep-dive/intel/` 是一个单公司深度看板示例（Intel：公司事件、财报与预期、行业层信号、分析结论），从龙头卡片可以直接跳转。

## 快速开始

需要 Python 3.9+，只用标准库，不需要安装依赖。

```bash
git clone https://github.com/xiaodaopax-ship-it/industry-radar.git
cd industry-radar

# SEC 要求在请求头里提供联系方式
export SEC_USER_AGENT="industry-radar you@yourmail.com"        # Windows PowerShell: $env:SEC_USER_AGENT="..."

python -m radar fetch    # 抓取数据并生成 site/index.html（首次约 30~60 分钟，之后有缓存）
python -m radar build    # 只用最近一次快照重新生成页面，不联网，几秒钟
python -m radar research # 为研究优先池、龙头和有调研文件的公司摘录申报原文（10-K、8-K、XBRL），然后重新生成页面
python -m radar heat     # 抓取全市场行业热度（SEC，首次较慢，有缓存），然后重新生成页面
```

然后用浏览器打开 `site/index.html`。

单公司深度看板：

```bash
python deep-dive/intel/build.py            # 生成 site/deep-dive/intel/
python deep-dive/intel/build.py --offline  # 用已有快照重新生成
```

## A 股看板（独立）

A 股的数据来源、行业分类、财报口径和风险事件都和美股不同，所以单独一条流水线、单独一个页面 `site/cn/index.html`（主看板顶部有入口），规则互不影响。

```bash
python -m cn fetch   # 东方财富数据中心 + 巨潮资讯，保存 data/cn/snap_<ts>.json，并抓取评估卡所需的个股资料（首次约 25 分钟）
python -m cn build   # 离线生成 site/cn/index.html
python -m cn cards   # 只重新抓取评估卡的个股资料
python -m cn.smoke   # 数据层自检
```

内容：东财行业热度 → 新兴公司筛选（营收同比 ≥20%，现金流质量、商誉、质押、减持、解禁、监管类公告、估值分位、业绩预告逐项检查）→ 价值 / 博弈标签（规则透明，列出触发原因）→ 研究优先级 Top 30 → 个股评估卡。所有阈值在 `cn/build.py` 的 `RULES` 和 `cn/cards.py` 的 `CARD_RULES` 里，页面上也会列出。接口与字段说明见 `cn/SOURCES.md`。

注意：东财和巨潮不是开放 API，有频率限制，代码串行请求并带缓存；监管类公告只按标题匹配，需要阅读原文；行业是东财分类不是申万；财务是年初至今累计口径。资源、化工原料、钢铁、航运、养殖、工程机械等周期行业（名单见 `RULES["cyclical_industries"]`）不标"偏价值"，也不参与研究优先级排序，单独列出。

## 数据来源与覆盖范围

| 数据 | 来源 | 覆盖 |
|---|---|---|
| 美国公司财务（营收、毛利、营业利润、净利润、股数、现金） | SEC XBRL frames | 所有提交 10-Q / 10-K 的公司 |
| 行业代码、股票代码、内部人交易（Form 4） | SEC submissions | 同上 |
| 台湾、韩国公司季度财务；日本公司年度财务 | Yahoo Finance 财务时间序列 | 按 Yahoo 行业筛选器取股票池 |
| 股价、市值、EPS 实际与预期、一致预期、财报日、分析师人数、业务描述 | Yahoo Finance | 全部 |

几点需要知道：

- **为什么台日韩公司以前不在里面**：它们在本地交易所上市，按本地规则披露（台湾 MOPS、日本 EDINET / TDnet、韩国 DART），不向 SEC 提交季报。台积电这类公司在美国挂的是 ADR，提交的是 20-F 年报，不在 10-Q 体系里。本项目用 Yahoo 的数据补上了这一块。
- **日本公司只有年度数据**：Yahoo 免费数据里没有日本公司的季度财务。要做季度分析，可以接入 EDINET API（需要免费注册 API key）。
- **更官方的海外数据源**（欢迎贡献）：台湾 [TWSE OpenAPI](https://openapi.twse.com.tw/) / MOPS，韩国 [DART OpenAPI](https://opendart.fss.or.kr/)，日本 [EDINET API](https://disclosure2.edinet-fsa.go.jp/)。
- **Yahoo Finance 是非官方接口**，可能随时变化或限流；它的数据受 Yahoo 服务条款约束。本仓库**不包含**任何抓取下来的数据，`data/` 和 `site/` 都在 `.gitignore` 里。如果你要把生成的页面公开发布，请先确认是否符合数据提供方的条款。
- SEC 数据属于公开数据，但请遵守 [SEC fair access](https://www.sec.gov/os/accessing-edgar-data) 规定：带联系方式、每秒不超过 10 次请求（代码已经限速）。

## 方法

**新兴公司筛选**

1. 美国：从全部 SEC 申报公司里，按近 12 个月营收规模（默认 0.5 亿 ~ 30 亿美元）和最新季度营收同比（默认 ≥ 20%）预筛。
2. 海外：从 Yahoo 行业股票池里，按市值区间预筛，再取财务数据。
3. 按 SIC 代码（美国）或 Yahoo 行业（海外）加公司名称关键词，归到细分环节；可以在 `overrides` 里人工指定。
4. 每个环节可以有自己的门槛，例如公用事业的增长门槛是 10%。

**研究优先池**

先剔除周期性环节、缺市值、稀释超过 50%、亏损且现金撑不过 6 个季度、营收同比超过 300%（通常是一次性收入）的公司（金融类口径的门槛不同，见下）。剩下的按 10 个因子在本行业候选中的百分位排名取平均：增速、加速、毛利率、毛利率趋势、营业利润率、增速调整后的市销率、稀释、内部人净买入、分析师覆盖（越少越好）、EPS 超预期。

**权重相同，没有经过回测。** 排名只说明"数据面值得先看"，不代表会涨。

**金融类评估口径**

券商、交易所、交易平台和放贷公司没有毛利，经常没有营业利润科目，资产负债表上的现金大多是客户资金，估值看市盈率、市净率和 ROE，放贷公司的主要风险是信用损失。所以环节可以在 `sectors.json` 里设 `"profile": "financial"`（默认 `standard`），这类环节换一套规则：

- 数据：营收按 `config.json` 里 `financial_tags.revenue` 的顺序取（`RevenuesNetOfInterestExpense` → `Revenues` → 合同收入；券商和放贷公司的合同收入通常只含手续费），另取税前利润、股东权益、信用损失拨备（`ProvisionForLoanLossesExpensed`、`FinancingReceivableExcludingAccruedInterestCreditLossExpenseReversal` 等）和净利息收入。这些只用于金融类环节，其他行业的数据和结果不变。金融类龙头的这几项来自 Yahoo 财务时间序列。
- 林奇清单：增长、盈利能力（税前利润率及变化）、ROE（TTM 净利润 / 平均股东权益）、估值（市盈率、市净率、PEG）、资本（股东权益同比）、信用成本（只对 `"credit": true` 的放贷环节：拨备占收入及变化）、周期性提示、稀释、内部人、财报 vs 预期、关注度。不看毛利率和现金跑道。
- 研究优先池门槛：周期性环节不剔除、只提示；用"TTM 净亏损"代替"亏损且现金不足 6 个季度"；股东权益为负剔除；缺市值、稀释超过 50%、营收同比超过 300% 照旧。
- 排序因子：增速、加速、税前利润率、税前利润率趋势、ROE、PEG（没有时用市净率）、股东权益增长、稀释、内部人、关注度、EPS 超预期；百分位只在同一口径的公司之间计算。
- 已知局限：拨备超过收入的视为标签错误不使用；按公允价值计量贷款的公司（如 Upstart）XBRL 里没有拨备科目；6 月财年的公司（如 Affirm）第四财季只在年报里，SEC 季度数据缺这一季；有大量少数股东权益的公司（如 IBKR）归母净利润、股东权益与市值口径不一致，市盈率和市净率需要回到原始财报核对。

**自动生成的文字**

- 机会、风险、买入理由、不买的理由、结论：按数据规则生成，见 `radar/score.py`。
- 一句话介绍：优先用 `config/notes.json` 里你写的描述，其次是 `config/profiles_zh.json` 中的中文概述，都没有时显示 Yahoo 英文业务描述的首句。

## 配置

| 文件 | 作用 |
|---|---|
| `config/config.json` | 全局门槛、排序因子、缓存时间、汇率代码 |
| `config/sectors.json` | 行业、细分环节、分类规则、龙头、需求证据、人工调整和排除 |
| `config/notes.json` | 你的研究笔记：两分钟故事、反证条件、客户集中度、业务层面的理由 |
| `config/profiles_zh.json` | 公司的中文一句话介绍 |
| `config/research/` | 六维综合调研，每家公司一个 JSON，格式和规则见该目录的 README |

## 项目结构

```
radar/            数据抓取、分类、打分、生成页面
  net.py          SEC 与 Yahoo 的 HTTP 封装（限速、缓存）
  us.py           SEC XBRL frames、submissions、Form 4
  yh.py           Yahoo 市场数据、海外财务、汇率换算
  classify.py     行业环节归类与门槛
  score.py        林奇清单、机会与风险、研究优先池排名
  collect.py      抓取流程，保存不可覆盖的原始快照
  research.py     申报原文自动摘录（10-K / 20-F、8-K 5.02、XBRL 研发费用、Yahoo 公司资料）
  heat.py         全市场行业热度
  build.py        汇总、生成 site/index.html
cn/               A 股流水线（em.py 数据层、build.py 筛选与页面、cards.py 评估卡）
templates/        看板 HTML 模板（dashboard.html 美股、cn_dashboard.html A 股；纯 SVG 图表，无外部依赖）
config/           配置与研究笔记
deep-dive/intel/  单公司深度看板示例
.github/workflows 每周自动构建（只上传构建产物）
```

## 已知局限

- SIC 和 Yahoo 行业分类都很粗，归类需要人工复核。
- 同一家公司可能同时出现在两个行业。
- 海外公司没有内部人交易数据；免费数据里没有营收的历史预期，所以"财报 vs 预期"只比较 EPS。
- XBRL 和 Yahoo 偶有数据错误，异常值以公司原始财报为准。

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 免责声明

本项目仅用于研究和学习，不构成任何投资建议。作者不对使用本项目产生的任何损失负责。
