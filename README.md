# Industry Radar

> Sector → segment → leaders & emerging companies → research shortlist. A single-file HTML dashboard built from SEC EDGAR and Yahoo Finance data, inspired by Peter Lynch's "fast grower" checklist. **Research tool, not investment advice.**

一个按"行业整体分析 → 细分领域拆解 → 龙头与新兴公司 → 研究机会清单"组织的看板。用 SEC EDGAR 和 Yahoo Finance 的公开数据，按彼得·林奇"快速增长股"的思路筛选，最后生成一个离线可打开的 HTML 页面。

**这是研究工具，不是投资建议。** 页面上的"机会""风险""结论"都是按数据规则自动生成的，用来决定先研究谁，不能代替对业务、竞争和管理层的判断。

## 看板结构

| 步骤 | 内容 |
|---|---|
| ① 行业总览 | 行业判断与需求证据、行业 ETF 对比大盘、细分环节市值树图 |
| ② 细分拆解 | 环节"增长 × 估值"象限图、环节一览、环节内公司市值占比树图 |
| ③ 龙头分析 | 每个环节手动配置的龙头（可跨美股、台股、日股、韩股）：增长、利润率、估值、EPS 实际与预期、下季预期、一年走势 |
| ④ 新兴机会 | 增速 × 毛利率气泡图、研究优先池 Top 5、候选表（可按环节、市场、股价区间、结论筛选）、评估卡 |

**评估卡**（龙头和新兴公司共用同一种版式）：一句话介绍 → 数据面结论 → 增长 / 盈利 / 估值 / 财报 vs 预期四个指标块 → 机会与风险并排 → 因子雷达图、52 周价格区间与目标价、林奇清单 → 下一步要验证的问题和研究笔记。
| ⑤ 机会清单 | 按环节汇总"先研究谁"，全部行业的研究优先池，可导出 CSV |

内置四个行业：AI 基础设施与半导体、医药与医疗、能源与电力、软件与网络安全。行业、环节、龙头、门槛都在 `config/` 里配置，可以自行增加。

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
```

然后用浏览器打开 `site/index.html`。

单公司深度看板：

```bash
python deep-dive/intel/build.py            # 生成 site/deep-dive/intel/
python deep-dive/intel/build.py --offline  # 用已有快照重新生成
```

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

先剔除周期性环节、缺市值、稀释超过 50%、亏损且现金撑不过 6 个季度、营收同比超过 300%（通常是一次性收入）的公司。剩下的按 10 个因子在本行业候选中的百分位排名取平均：增速、加速、毛利率、毛利率趋势、营业利润率、增速调整后的市销率、稀释、内部人净买入、分析师覆盖（越少越好）、EPS 超预期。

**权重相同，没有经过回测。** 排名只说明"数据面值得先看"，不代表会涨。

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

## 项目结构

```
radar/            数据抓取、分类、打分、生成页面
  net.py          SEC 与 Yahoo 的 HTTP 封装（限速、缓存）
  us.py           SEC XBRL frames、submissions、Form 4
  yh.py           Yahoo 市场数据、海外财务、汇率换算
  classify.py     行业环节归类与门槛
  score.py        林奇清单、机会与风险、研究优先池排名
  collect.py      抓取流程，保存不可覆盖的原始快照
  build.py        汇总、生成 site/index.html
templates/        看板 HTML 模板（纯 SVG 图表，无外部依赖）
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
