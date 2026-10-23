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

内置十四个行业：AI 基础设施与半导体、医药与医疗、能源与电力、软件与网络安全、工程建设与专业承包、工业机械与航空零部件、支付与金融软件、券商、交易平台与放贷科技、油气开采、油服与炼化、金属、矿业与煤炭、航运与邮轮、保险与保险经纪、银行、REIT（房地产投资信托）。券商、保险、银行和抵押贷款 REIT 用单独的金融类评估口径（见下文"方法"）；油气、金属矿业煤炭和航运的所有环节标为周期，不进研究优先池、不显示 PEG，只用来观察数据；REIT 的环节设了 `"peg": false`（净利润含大额折旧，行业看 FFO / AFFO，本工具不采集），不显示 PEG。资产管理公司、空白支票公司（SPAC）、特许权使用费和专利授权公司不在覆盖范围。行业、环节、龙头、门槛都在 `config/` 里配置，可以自行增加。

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

页面导航与备注：左侧栏列出全市场热度、PEG 筛选和全部行业（点行业切换并跳到 ①，当前行业下有 ①–⑤ 步骤链接），顶部的"公司快速跳转"输入代码或公司名回车即可打开该公司的评估卡；窄屏（< 1100px）时侧栏收进左上角"☰ 导航"。右侧"备注 (n)"打开备注面板，评估卡和 PEG 表每行都有"＋备注"按钮（自动关联公司）。备注只保存在当前浏览器的 localStorage（键 `industry-radar-notes-v1`），不写进页面文件，重新构建不会丢；换浏览器或清理浏览器数据前用面板底部的"导出 JSON"备份，"导入 JSON"按备注 id 合并（同一条保留更新时间较新的版本）。

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
python -m cn cards   # 只重新抓取评估卡的个股资料（同一快照已抓过且没失败的字段直接复用）
python -m cn.smoke   # 数据层自检
```

内容：东财行业热度 → 新兴公司筛选（营收同比 ≥20%，现金流质量、商誉、质押、减持、解禁、监管类公告、估值分位、业绩预告逐项检查）→ 价值 / 博弈标签（规则透明，列出触发原因）→ 研究优先级 Top 30 → 个股评估卡。评估卡和优先级表另列 PEG 参考项（优先用机构一致预期 EPS 的两年年化增速，没有可用一致预期时用扣非年初至今同比；周期行业、PE ≤ 0、增速 ≤ 0 不显示；基数低、增速 > 100%、一致预期大幅下降时标为不可靠），**不参与排序和标签**。所有阈值在 `cn/build.py` 的 `RULES` 和 `cn/cards.py` 的 `CARD_RULES` 里，页面上也会列出。接口与字段说明见 `cn/SOURCES.md`。

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

**PEG**

并排给两个 PEG（市盈率 ÷ 每股收益增速 %）和一个背离提示，阈值在 `config.json` 的 `flags.peg` 里。前瞻 PEG 依赖分析师对下一年的预期，历史 PEG 只看已经发生的增长；两者背离时（今年下滑、预期明年反弹，或者增长在放缓）单看一个容易误判。

- 前瞻（`d.pegf`）：Yahoo `earningsTrend` 的一致预期 EPS，本财年（`0y`）→ 下一财年（`+1y`）的增速，只有一年。要求 `+1y` 至少 `min_analysts`（3）家分析师、两个预期都大于 0。两个口径：PE TTM 口径 = 市盈率（市值 ÷ 近四季 GAAP 净利润）÷ 增速，用于判断；前瞻 PE 口径 = （股价 ÷ FY1 预期 EPS）÷ 同一增速，前瞻 PE 用按汇率换算后的 `mkt.eps_est["+1y"].yield_pct` 算（`100 / yield_pct`），所以预期币种和报价币种不同也没问题。
- 历史（`d.pegh`）：市盈率（TTM）÷ 过去 12 个月稀释 EPS 同比。美股来自 SEC XBRL frames（`us.hist_eps()`，存在 `eps_h`）：近四季净利润 = 上一年度 `NetIncomeLoss` + 今年已报季度 − 去年同期季度（CYxxxxQ4 frame 只覆盖约 1/5 的公司，第四季度用年度数字推算，和 `ni_ttm` 一样），一年前的四季同样算；除以四季平均稀释股数（`WeightedAverageNumberOfDilutedSharesOutstanding`，缺第四季度用年度加权平均，偏离中位数 3 倍以上视为单位错误剔除，不足 3 个季度就改用 TTM 净利润增速并注明）。年度净利润远低于前三季合计（推算第四季度 < −75% 前三季，如 SCHW 的年度 `NetIncomeLoss`）视为标签 / 单位错误，历史 PEG 不显示。龙头（Yahoo 取数）按 SEC 股票代码表映射 CIK，用同样的 frames；frames 缺季度时用该公司的 `companyconcept`（先 `NetIncomeLoss`，填不满两个窗口时缺的期间用 `ProfitLoss` 补）按“年度 + 年初至今 − 去年同期年初至今”算，最新 10-Q 还没进 XBRL 时用上一季并注明。只有年度数据的公司（日本）沿用最新财年净利润同比；Yahoo 季度口径（台湾、韩国）只有约 5 个季度，不显示；只交 20-F / 40-F 的公司（如 TSM）没有 us-gaap 数据，不显示。
- 不显示：TTM 亏损、增速 ≤ 0（历史：“过去 12 个月 EPS 同比 x%，不为正”）、一年前 TTM 亏损、标准口径的周期性环节、`sectors.json` 里设了 `"peg": false` 的环节（任何口径；原因写在 `peg_why`，例如 REIT）。金融类的周期环节（券商、加密、抵押贷款与产权保险）照算，加提示。
- 不可靠（显示但不参与判断和排序）：增速超过 `growth_unreliable_pct`（100%）；基数 EPS 不到股价的 `min_eps_yield_pct`（0.5%；前瞻看本财年预期 EPS，历史看一年前的 TTM EPS）。
- 背离（`d.peg_div`，评估卡里一行 ⚠，表格里 ⚠ 悬停看原因）：前瞻 ≤ `good_max` 而历史 EPS 增速 ≤ 0；前瞻 ≤ `good_max` 而历史 > `div_hist_rich`（2）；历史 ≤ `good_max` 而前瞻 > `div_hist_rich`；本财年预期 EPS 比上一财年实际（Yahoo `yearAgoEps`）低 `fy0_drop_pct`（10%）以上。
- 用在哪里：判断用前瞻（PE TTM 口径）。只有前瞻可靠且 ≤ `good_max`、历史 ≤ `rich_min` 或因非负面原因算不出（不是增速 ≤ 0）、并且没有背离时，才算“偏便宜”（清单估值项 ✓、机会里写一条，高市销率也不再单独算偏贵）；前瞻 > `rich_min` 算偏贵（不变）；前瞻 ≤ `good_max` 但有背离时估值项为“·”，不下结论，并在“要验证”里加一个问题。金融类估值排序因子：可靠且没有背离的前瞻 PEG，否则市净率。没有一致预期时：日本等年度口径沿用历史 PEG 判断（不变），美股季度口径的历史 PEG 只作参考。标准口径的排序因子仍是增速调整后的市销率，不用 PEG。
- 局限：Yahoo 一致预期是非官方汇总，常是调整后 EPS，而市盈率和历史增速用 GAAP 净利润；上一财年实际 EPS 也来自 Yahoo；frames 按日历季度对齐，非日历财年有错位，缺一季就算不出；GAAP 净利润含一次性项目（递延所得税资产确认等）；一年的增速噪声大。

**金融类评估口径**

券商、交易所、交易平台和放贷公司没有毛利，经常没有营业利润科目，资产负债表上的现金大多是客户资金，估值看市盈率、市净率和 ROE，放贷公司的主要风险是信用损失。所以环节可以在 `sectors.json` 里设 `"profile": "financial"`（默认 `standard`），这类环节换一套规则：

- 数据：营收按 `config.json` 里 `financial_tags.revenue` 的顺序取（`RevenuesNetOfInterestExpense` → `Revenues` → 合同收入；券商和放贷公司的合同收入通常只含手续费），另取税前利润、股东权益、信用损失拨备（`ProvisionForLoanLossesExpensed`、`FinancingReceivableExcludingAccruedInterestCreditLossExpenseReversal` 等）和净利息收入。这些只用于金融类环节，其他行业的数据和结果不变。金融类龙头的这几项来自 Yahoo 财务时间序列。
- 林奇清单：增长、盈利能力（税前利润率及变化）、ROE（TTM 净利润 / 平均股东权益）、估值（市盈率、市净率、PEG）、资本（股东权益同比）、信用成本（只对 `"credit": true` 的放贷环节：拨备占收入及变化）、周期性提示、稀释、内部人、财报 vs 预期、关注度。不看毛利率和现金跑道。
- 研究优先池门槛：周期性环节不剔除、只提示；用"TTM 净亏损"代替"亏损且现金不足 6 个季度"；股东权益为负剔除；缺市值、稀释超过 50%、营收同比超过 300% 照旧。
- 排序因子：增速、加速、税前利润率、税前利润率趋势、ROE、PEG（没有可靠的 PEG 时用市净率）、股东权益增长、稀释、内部人、关注度、EPS 超预期；百分位只在同一口径的公司之间计算。
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
