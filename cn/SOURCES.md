# cn/ 数据源核验记录（A 股）

核验时间：2026-10-21 ~ 2026-10-22（本机，Python 3.13 标准库）。个人研究工具，不构成投资建议。
运行 `python -m cn.smoke`（在 `industry-radar/` 根目录）复现；最近一次结果 **71/71 检查通过**（2026-10-23）。

## 通用约定

| 项 | 说明 |
|---|---|
| 请求头 | Eastmoney：`User-Agent: Mozilla/5.0`、`Referer: https://data.eastmoney.com/`；cninfo 另需 `Origin`、`X-Requested-With: XMLHttpRequest`（见 g） |
| 限速 | 全局锁，任意两次请求间隔 ≥ 0.5 s（≤ 2 次/秒）；失败重试 3 次，退避 1.5 s / 3 s |
| 缓存 | `data/cn/cache/<sha1(url+body)>.json`，TTL 参数单位为天（默认 1）；失败不缓存 |
| datacenter-web 分页 | `pageSize` 服务端上限 **500**（传更大也只返回 500）；必须带确定性排序（以 `SECURITY_CODE` 收尾）避免翻页漂移 |
| 空结果 | datacenter 返回 `success=false, code=9201, message=返回数据为空` 视为正常空结果，不报警 |
| 单位 | 输出统一换算为 **元 / 股**；`_pct` 结尾为百分数（12.3 即 12.3%）。原始字段里的"万股""万元""小数比例"已在代码中换算 |
| 口径 | 利润表、现金流量表项目为**年初至今累计**（一季报 = Q1，半年报 = H1，三季报 = 1–9 月，年报 = 全年）。单季需相减。资产负债表为时点数 |
| 日期 | Eastmoney 的 `NOTICE_DATE` 对盘后公告常记为下一交易日（本机日期 10-21 时已出现 10-22 的记录） |
| 失败处理 | 所有公开函数不抛异常：失败时向 stderr 打印 `WARNING: ...`（英文）并返回 `[]` 或已取得的部分数据 |

---

## a. `quotes(ttl=1, live=False)` — 全 A 行情与估值快照

**结果：works（EOD 快照）/ partial（push2 实时分页）**。实测 5,576 行（as_of 2026-10-21），五个板块齐全：
沪主板 1,700、科创板 618、深主板 1,495、创业板 1,411、北交所 352。

**默认源（EOD）**：`https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET&filter=(TRADE_DATE='<最新交易日>')`，约 12 次请求。

| 输出键 | 字段 | 备注 |
|---|---|---|
| price | CLOSE_PRICE | 收盘价 |
| change_pct | CHANGE_RATE | % |
| total_mcap / float_mcap | TOTAL_MARKET_CAP / NOTLIMITED_MARKETCAP_A | 元 |
| pe_ttm / pe_static / pb / ps_ttm | PE_TTM / PE_LAR / PB_MRQ / PS_TTM | |
| industry | BOARD_NAME | 东财行业，与 push2 f100 同一分类（如"白酒Ⅱ"） |
| total_shares / float_shares | TOTAL_SHARES / FREE_SHARES_A | 股 |
| market / board | 由代码前缀推导 | 60→SH_MAIN，688/689→STAR，00→SZ_MAIN，300/301→ChiNext，92/8/4→BSE |

**实时源（live=True）**：`https://push2.eastmoney.com/api/qt/clist/get?pn=&pz=100&po=0&np=1&fltt=2&invt=2&fid=f12&fs=<板块>&fields=...`

板块 fs：`m:1+t:2` 沪主板、`m:1+t:23` 科创板、`m:0+t:6` 深主板、`m:0+t:80` 创业板、`m:0+t:81+s:2048` 北交所（`m:0+t:81` 不带 s 会返回 6,806 只含新三板）。

| f 代码 | 含义 | 核验（600519，2026-10-21） |
|---|---|---|
| f12 / f14 / f13 | 代码 / 名称 / 市场（1=沪，0=深/京） | 600519 / 贵州茅台 / 1 |
| f2 / f3 | 最新价 / 涨跌幅 % | 1310.49 / 0.06 |
| f20 / f21 | 总市值 / 流通市值（元） | 1.638e12，与 EOD TOTAL_MARKET_CAP 差 0.06% |
| f115 | PE(TTM) | 20.12 vs EOD PE_TTM 20.10 ✓ |
| f114 | PE(静态，上一年报) | 19.90 vs PE_LAR 19.89 ✓ |
| f9 | PE(动态，按年初至今年化) | 18.40 ≈ 1310.49 /(H1 EPS 35.57×2) ✓ |
| f23 | PB(MRQ) | 6.52 vs PB_MRQ 6.516 ✓ |
| f8 / f6 | 换手率 % / 成交额（元） | 0.12 / — |
| f26 | 上市日期 yyyymmdd | 20010827 ✓ |
| f100 | 东财行业 | 白酒Ⅱ |

注意事项：
- push2 `pz` 上限 100，全市场约 60 次请求；本机实测**分页请求大部分返回 HTTP 502 或断开连接**（换 `82.`、`33.`、`push2delay.` 子域、间隔 2.5 s 均无明显改善），`fid=f20` 排序几乎必 502。单次 `ulist.np/get?secids=1.600519,0.300750` 正常。因此默认 `live=False`；`live=True` 时任何一页失败即**整体丢弃实时数据**，全部回退 EOD，避免混合时点。
- push2 清单总数 5,925 > EOD 5,576：多出的约 350 只为已退市/吸收合并代码（价格为 `-`，如 600001 邯郸钢铁、"退市××"），EOD 快照是更准确的在市股票池。
- EOD 模式下 `turnover_pct`、`amount`、`pe_dynamic`、`list_date` 为 None。

## b. `financials(report_date)` — 单报告期全市场财务

**结果：works**。2026-06-30：5,741 行（营收 5,579 / 经营现金流 5,583 / 资产负债表 5,641）；2026-03-31：5,619 行（5,574 / 5,592 / 5,519）。每期约 35 次请求。

三张表按代码合并，每表同一代码保留 `UPDATE_DATE`/`NOTICE_DATE` 最新的版本（更正/追溯调整覆盖原值）。

| 输出键 | reportName | 字段 |
|---|---|---|
| revenue, revenue_yoy_pct | RPT_LICO_FN_CPD（业绩报表） | TOTAL_OPERATE_INCOME, YSTZ |
| parent_net_profit, net_profit_yoy_pct | 同上 | PARENT_NETPROFIT, SJLTZ |
| gross_margin_pct, eps, eps_deducted | 同上 | XSMLL, BASIC_EPS, DEDUCT_BASIC_EPS |
| roe_pct（加权，年初至今，未年化） | 同上 | WEIGHTAVG_ROE |
| bps, operating_cash_flow_per_share | 同上 | BPS, MGJYXJJE |
| board, risk_warning, industry | 同上 | TRADE_MARKET（含"风险警示板"）, PUBLISHNAME |
| operating/investing/financing_cash_flow, capex | RPT_DMSK_FN_CASHFLOW | NETCASH_OPERATE, NETCASH_INVEST, NETCASH_FINANCE, CONSTRUCT_LONG_ASSET |
| accounts_receivable, inventory, goodwill | RPT_F10_FINANCE_GBALANCE | ACCOUNTS_RECE, INVENTORY, GOODWILL |
| parent_equity, cash, total_assets, total_liabilities | 同上 | TOTAL_PARENT_EQUITY, MONETARYFUNDS, TOTAL_ASSETS, TOTAL_LIABILITIES |
| short_term_loans, long_term_loans, bonds_payable | 同上 | SHORT_LOAN, LONG_LOAN, BOND_PAYABLE |
| current_portion_noncurrent_liab | 同上 | NONCURRENT_LIAB_1YEAR（单列，未计入有息负债） |
| interest_bearing_debt | 计算 | 短期借款 + 长期借款 + 应付债券（缺失项按 0；三项全缺为 None） |
| share_capital, contract_liabilities | 同上 | SHARE_CAPITAL（股）, CONTRACT_LIAB |

交叉核验（2026-06-30，smoke 自动执行）：

| 检查 | 600519 贵州茅台 | 300750 宁德时代 |
|---|---|---|
| 营收：CPD vs RPT_DMSK_FN_INCOME | 92,278,072,083.21 = 一致 | 276,916,580,000 = 一致 |
| 归母净利：CPD vs 利润表 | 44,516,880,421.86 = 一致 | 43,284,002,000 = 一致 |
| 经营现金流：每股 × 股本 vs 现金流量表 | 70,690,750,119.06 = 一致 | 60,216,851,000 = 一致 |
| 归母权益：BPS × 股本 vs 资产负债表 | 251,253,594,419.50 = 一致 | 379,353,710,000 = 一致 |
| 年初至今累计：H1 营收 > Q1 营收 | 922.8 亿 > 547.0 亿 | 2,769.2 亿 > 1,291.3 亿 |
| 有息负债 | None（无借款/债券） | 208.86 + 742.63 + 285.12 = 1,236.61 亿 ✓ |

注意事项：
- `RPT_LICO_FN_CPD` 不加 `SECURITY_TYPE_CODE="058001001"` 会额外返回约 5,800 家新三板/老三板公司（单期 11,440 行），代码中已过滤。`ISNEW` 不是"最新版本"标记（300750 唯一一行为 ISNEW=0），不要用它过滤。
- **银行、保险、券商缺资产负债表字段**：`RPT_F10_FINANCE_GBALANCE` 只覆盖一般工商业模板（000001 平安银行、601318 中国平安查无记录），这些公司 `has_balance_sheet=False`。金融类有独立模板（推测为 BBALANCE/IBALANCE/SBALANCE 系列，**未核验**），且"应收/存货/有息负债"对其本无意义。
- 合并结果行数（5,741）略多于 CPD（5,580）：资产负债表里有少量 CPD 无记录的代码（新上市或刚退市），其利润字段为 None。
- `RPT_DMSK_FN_BALANCE` 可用但**没有商誉、长期借款、应付债券、归母权益**，所以改用 F10 全量资产负债表。

## c. `forecasts(since)` — 业绩预告

**结果：works**。since 2026-09-01：479 行，其中归母净利润口径（metric_code=004）177 行；类型分布：预增 108、略增 22、扭亏 14、预减 13、略减 9、减亏 5、首亏 4、续盈 2。

`reportName=RPT_PUBLIC_OP_NEWPREDICT`，`filter=(NOTICE_DATE>='since')`。

| 输出键 | 字段 |
|---|---|
| notice_date / report_period | NOTICE_DATE / REPORT_DATE |
| metric_cn / metric_code | PREDICT_FINANCE / PREDICT_FINANCE_CODE（004 归母净利润；另有扣非、营收等） |
| forecast_type_cn / forecast_type | PREDICT_TYPE（预增/略增/续盈/扭亏/预减/略减/首亏/续亏/增亏/减亏/不确定）→ 英文枚举 |
| profit_low / profit_high | PREDICT_AMT_LOWER / UPPER（元，年初至今累计） |
| yoy_low_pct / yoy_high_pct | ADD_AMP_LOWER / UPPER |
| last_year_same_period | PREYEAR_SAME_PERIOD |
| content_cn / reason_cn | PREDICT_CONTENT / CHANGE_REASON_EXPLAIN |

注意：同一公司同一期可能有多行（不同 metric，或修正公告）；需要时按 `metric_code=='004'` 过滤并按 notice_date 取最新。

## d. `holder_changes(since)` — 董监高与股东增减持

**结果：works**。since 2026-07-01：4,523 行 = 董监高 2,870（增 1,061 / 减 1,809）+ 股东 1,653（增 397 / 减 1,256）。

| 来源 | reportName | 日期过滤 | 关键字段 |
|---|---|---|---|
| executive | RPT_EXECUTIVE_HOLD_DETAILS | CHANGE_DATE | PERSON_NAME, DSE_PERSON_NAME（关联董监高）, PERSON_DSE_RELATION, POSITION_NAME, CHANGE_SHARES（股，带符号，<0 为减持）, AVERAGE_PRICE, CHANGE_AMOUNT（元）, CHANGE_REASON, CHANGE_RATIO（占总股本 %） |
| major_shareholder | RPT_SHARE_HOLDER_INCREASE | END_DATE（变动区间截止日） | HOLDER_NAME, DIRECTION（增持/减持）, CHANGE_NUM_SYMBOL（**万股**，带符号）, CHANGE_RATE（占总股本 %）, HOLD_RATIO（变动后 %）, TRADE_AVERAGE_PRICE（常为空）, CLOSE_PRICE, MARKET（方式）, NOTICE_DATE |

注意：股东表数量单位为**万股**（已 ×10,000，核验：山河智能 AFTER_HOLDER_NUM 6,276.7 万股 / 总股本 ≈ HOLD_RATIO 5.84%）；`value_cny` 仅在有成交均价时计算，否则为 None（`ref_close_price` 为公告日收盘价，仅供估算，不等于成交金额）。董监高表包含配偶、子女等关联人，看 `relation`。

## e. `pledge()` — 股权质押

**结果：works**。最新统计日 2026-10-16（周度），2,214 家有质押记录（无质押的公司不在表中）。

`reportName=RPT_CSDC_LIST`（中登数据）：先按 TRADE_DATE 倒序取 1 行得最新日，再拉该日全表（约 6 次请求）。

| 输出键 | 字段 | 单位 |
|---|---|---|
| pledge_ratio_pct | PLEDGE_RATIO | 占总股本 % |
| pledged_shares（及 unrestricted / restricted） | REPURCHASE_BALANCE（UNLIMITED / LIMITED） | 万股 → 股 |
| pledge_count | PLEDGE_DEAL_NUM | 笔 |
| pledged_mcap | PLEDGE_MARKET_CAP | 万元 → 元 |

核验：平安银行 质押 2,438.48 万股、比例 0.13%（≈ 2,438 万 / 194 亿股）。这是**公司整体**质押比例，不是控股股东的质押率。

## f. `unlocks(start, end)` — 限售股解禁

**结果：works**。2026-10-22 ~ 2026-12-21：298 行，解禁市值合计约 5,633 亿元（按 Eastmoney 参考价）。

`reportName=RPT_LIFT_STAGE`，`filter=(FREE_DATE>='start')(FREE_DATE<='end')`。

| 输出键 | 字段 | 单位 |
|---|---|---|
| shares / tradable_shares | CURRENT_FREE_SHARES / ABLE_FREE_SHARES | 万股 → 股 |
| ratio_total_pct | TOTAL_RATIO | **小数比例** ×100（核验：元创股份 280 / 7,840 万股 = 0.0357） |
| ratio_float_pct | FREE_RATIO | 占流通股，小数 ×100 |
| unlock_value | LIFT_MARKET_CAP | 万元 → 元 |
| type_cn / holders | FREE_SHARES_TYPE / BATCH_HOLDER_NUM | 首发原股东、股权激励、定增等 |

注意：未来日期的计划可能变更（延期、承诺延长锁定），以公司公告为准。

## g. `regulatory(since)` — 问询函 / 监管函 / 立案调查

**结果：works（标题关键词检索）/ 覆盖有缺口**。since 2026-09-01：593 条（深 295、沪 280、京 18）；分类：审核问询 458、问询函 55、行政处罚 39、监管函 16、立案调查 13、警示函 6、其他立案 6。

接口：`POST https://www.cninfo.com.cn/new/hisAnnouncement/query`，表单：
`pageNum, pageSize=30, column=szse, tabName=fulltext, searchkey=<关键词>, seDate=<since>~<until>, sortName=time, sortType=desc, isHLtitle=true`，其余（plate/stock/secid/category/trade）留空。

**此前返回空的原因**：缺少 `Origin: https://www.cninfo.com.cn` 与 `X-Requested-With: XMLHttpRequest` 头（以及 `Referer` 指向 cninfo 搜索页）。加上后正常。`column=szse` 实际覆盖沪深京三市。

关键词：问询函、关注函、监管函、警示函、立案、纪律处分、行政处罚；按 announcementId 去重，标题去掉 `<em>` 高亮后分类：

| kind | 规则 |
|---|---|
| review_inquiry | 含"审核问询"——IPO/再融资审核问答，属常规流程，**不是**监管处分信号（占大多数） |
| inquiry_letter / concern_letter | 年报问询函、监管问询函、关注函（含回复，`is_reply=True`） |
| regulatory_letter / warning_letter | 监管函、监管工作函 / 警示函 |
| investigation | 含"立案"且含"调查"或"证监会" |
| case_filing_other | 含"立案"但不像监管调查（多为诉讼立案） |
| disciplinary_action / administrative_penalty | 纪律处分 / 行政处罚（含子公司、环保等一般行政处罚） |

缺口与注意：
- 只覆盖**公司自己披露**的公告。交易所直接发出、公司未单独公告的函件（上交所/深交所"监管信息公开"栏目）未接入。
- 标题关键词匹配会漏掉标题不含关键词的公告，也会误收（如"行政处罚"含环保罚款）。研究用途需人工阅读原文（`url` 指向 PDF）。
- 每个关键词最多 20 页（600 条）；超出时打印 WARNING。请求量：since 2026-09-01 约 30 次。
- `announcementTime` 为毫秒时间戳，按 UTC+8 转日期。

## h. `valuation_history(code, years=5)` + `valuation_percentiles(history)`

**结果：works**。日频，600519 与 300750 近 5 年各 1,211 行（2021-10-22 ~ 2026-10-21）；可用历史约从 2018 年起（600519 全量 2,133 行）。5 年约 3 次请求。

`reportName=RPT_VALUEANALYSIS_DET`，`filter=(SECURITY_CODE="<code>")(TRADE_DATE>='<start>')`。

字段：TRADE_DATE, CLOSE_PRICE, PE_TTM, PE_LAR（静态）, PB_MRQ, PS_TTM, PCF_OCF_TTM（市现率）, TOTAL_MARKET_CAP。

分位计算（smoke 实测，2026-10-21）：

| | PE(TTM) | PB | PS(TTM) |
|---|---|---|---|
| 600519 最新 / 5 年分位 | 20.10 / 16.6% | 6.52 / 8.4% | 9.45 / 7.8% |
| 300750 最新 / 5 年分位 | 12.02 / 0.1% | 2.73 / 0.1% | 1.81 / 5.5% |

注意：`valuation_percentiles` 剔除 ≤0 的值（亏损期 PE 为负，不代表"便宜"）；最新值本身 ≤0 时分位返回 None。分位只描述"相对自身历史的位置"，不是估值结论。

## i. 评估卡用的个股 F10 数据（`python -m cn cards`）

核验时间 2026-10-22，样本 300191 潜能恒信 / 688297 中无人机 / 300750 宁德时代；随后对 36 只评估卡股票全量抓取，0 次请求失败。全部走 datacenter-web `api/data/v1/get`，请求头、限速、缓存与上文相同。F10 报表按 `SECUCODE`（如 `300191.SZ`，由 `em.secucode()` 根据代码前缀推出）过滤。每只股票约 4 次请求 + 估值历史（同日已缓存）。

| 函数 | reportName | 过滤 / 排序 | 字段 → 输出键 | 结果 |
|---|---|---|---|---|
| `company_profile(code, ttl=30)` | RPT_F10_BASIC_ORGINFO（公司概况） | `(SECUCODE="…")` | MAIN_BUSINESS → main_business，ORG_PROFILE → profile，BUSINESS_SCOPE，EM2016 → industry_em（三级东财行业），INDUSTRYCSRC1 → industry_csrc，LISTING_DATE，FOUND_DATE，EMP_NUM → employees，CHAIRMAN，PRESIDENT，SECRETARY，ACTUAL_HOLDER，PROVINCE，ORG_WEB，ACCOUNTFIRM_NAME | works，36/36 |
| `main_business_composition(code, ttl=7)` | RPT_F10_FN_MAINOP（主营构成） | `(SECUCODE="…")`，`REPORT_DATE desc, MAINOP_TYPE, RANK`，取 120 行 | MAINOP_TYPE 1=按行业 2=按产品 3=按地区 → type；ITEM_NAME → item；MAIN_BUSINESS_INCOME → revenue（元）；MBI_RATIO（小数）×100 → share_pct；GROSS_RPOFIT_RATIO（原字段拼写如此，小数）×100 → gross_margin_pct；MAIN_BUSINESS_COST → cost；REPORT_NAME（如"2026中报"） | works，36/36。**每种分类各取自己最新的报告期**：中报常只有按产品，按地区多来自上一年报 |
| `float_holders(code, ttl=7)` | RPT_F10_EH_FREEHOLDERS（前十大流通股东） | `(SECUCODE="…")`，`END_DATE desc, HOLDER_RANK`，取 30 行（3 期） | END_DATE，NOTICE_DATE，HOLDER_RANK，HOLDER_NAME，HOLDER_TYPE，HOLD_NUM → shares，FREE_HOLDNUM_RATIO → float_ratio_pct（占流通 A 股 %），HOLD_NUM_CHANGE（"不变"/"新进"/带符号股数）+ XZCHANGE → change_shares / status，CHANGE_RATIO → change_pct（持股变动 %） | works，35/36（688825 返回空，推测上市后尚无含流通股东的定期报告）。上期在前十、本期不在的股东以 status=`exited` 附上 |
| `analyst_consensus(code, ttl=1)` | RPT_WEB_RESPREDICT（机构盈利预测汇总） | `(SECUCODE="…")` | RATING_ORG_NUM → org_num；RATING_BUY/ADD/NEUTRAL/REDUCE/SALE_NUM → rating_counts；YEAR1–4 + YEAR_MARK1–4（A 实际 / E 预测）+ EPS1–4 → eps；DEC_AIMPRICEMAX/MIN → target_max/min | works，28/36（8 只无机构覆盖，返回空）。汇总时间窗口未公开，**未核验**。看板只显示覆盖家数与 EPS，不显示评级用词和目标价 |
| 价格走势 | 复用 `valuation_history()` 的 CLOSE_PRICE | — | 近 365 天日收盘价 | works。**未复权**（送转、分红会跳变）。未使用 push2his K 线 |
| `deducted_history(code, start, end, ttl=1)`（PEG 历史口径） | RPT_LICO_FN_CPD + RPT_DMSK_FN_INCOME，按代码各 1 次请求 | `(SECURITY_CODE="…")(REPORTDATE>='上年同期')(REPORTDATE<='最新期')`；利润表用 REPORT_DATE；同一期多行时取 UPDATE_DATE / NOTICE_DATE 最新的一行（含追溯调整的上年比较数） | BASIC_EPS → eps，DEDUCT_BASIC_EPS → eps_deducted（**只到两位小数**），PARENT_NETPROFIT，DEDUCT_PARENT_NETPROFIT → deducted_net_profit（元）。年初至今累计 | works，40/40（2026-10-22，0 失败）。688297：2026-06-30 扣非 3,491 万 vs 2025-06-30 2,195 万，EPS 0.05 vs 0.03（舍入导致 EPS 口径 +67%、利润口径 +59%），所以两期扣非 EPS 都 ≥ 0.5 元时才用 EPS，否则用扣非归母净利润（股本变化未调整） |

PEG（评估卡参考项，规则与阈值在 `cn/cards.py` 的 `CARD_RULES["peg_*"]` 和 `peg()`）：前瞻口径用 RPT_WEB_RESPREDICT 的 EPS 均值（当年 = 第一个 ≥ 最新报告期年份的 E 年度，到两年后年化；覆盖机构 ≥ 3、所用预期 > 0），否则用上表的扣非年初至今同比。一致预期的统计窗口与机构名单东财未公开，**未核验**；EPS 是简单均值。PEG 不参与排序、标签和 ⚠ 数。

个股研报列表（2026-10-23 接入，只用于目标价）：见下节 j。

## j. 股价、52 周区间与机构目标价（只展示，不进入任何评分 / 标签 / 排序）

**股价**：快照 `quotes` 的 `price`（RPT_VALUEANALYSIS_DET CLOSE_PRICE，行情日 = `as_of`），build 时写入 ② 每行的 `price` / `px_date`，不新增请求。评估卡的 `fin.price` 来自同一字段。

**52 周区间**（`python -m cn ranges`，`cn/ranges.py`）。窗口 = (行情日 − 52 周, 行情日]，另多取 30 天用来区分"上市不足 52 周"和长期停牌。

| 来源 | 接口 | 字段 | 结果 |
|---|---|---|---|
| ① push2his 前复权日 K 线 `em.daily_kline_qfq()` | `push2his.eastmoney.com/api/qt/stock/kline/get?secid=<1=SH / 0=SZ,BJ>.<code>&klt=101&fqt=1&beg=&end=&fields2=f51,f52,f53,f54,f55` | f51 日期，f52 开，f53 收，f54 高，f55 低（前复权，最新一根 = 实际成交价） | 2026-10-22/23 核验：HTTPS 每次 RemoteDisconnected，HTTP 约 30 次只成功 1 次（600519，与下面 ② 的换算结果一致）；push2 / push2delay 返回 502。所以每次运行先用 3 个代码试探，失败则整次改用 ② |
| ② 数据中心换算 `ranges.derived_qfq_closes()`（实际使用） | RPT_VALUEANALYSIS_DET `(SECURITY_CODE="…")(TRADE_DATE>=…)(TRADE_DATE<=…)` 每代码 1 次 + RPT_SHAREBONUS_DET `(EX_DIVIDEND_DATE>=…)(EX_DIVIDEND_DATE<=…)` 全市场 1 组（约 11 页） | CLOSE_PRICE（未复权）；EX_DIVIDEND_DATE，PRETAX_BONUS_RMB（每 10 股派现）→ cash_per10，BONUS_IT_RATIO（每 10 股送转）→ bonus_it_per10 | 除权除息日之前的每个收盘价 P' = (P − 每股派现) / (1 + 每股送转)。与独立的前复权收盘序列（腾讯 fqkline qfq，仅一次性核对，不接入）对比：600519 / 301396（10 送转 4）/ 688808（10 转 4.8）/ 300475 最大相对误差 ≤ 1e-5。**只有收盘价**，basis=`qfq_close`；配股不在该表里，未调整 |

缓存 ttl 1 天（`em.fetch_json`），串行 + 每代码额外 0.2 s；顺序为评估卡 → ④ → 估值短名单 → 其余 ② 行；每 100 个代码写一次检查点；同一行情日 / 窗口的上一份 ranges 文件中已有结果的代码直接复用（续跑）；连续 8 个代码失败即停止并保存已有结果。

`ranges_<ts>.json`：`fetched_at, basis:"qfq", item_bases, as_of, window_start, snap, rules, complete, requested, ok, failed, stopped_early, push2his_used, items{code: {hi52, lo52, hi_date, lo_date, n_days, first_date, last_date, as_of, window_start, basis, last_close, short_history, stale, basis_mismatch, src_url}}, http`。build 读最新的 ranges 文件；缺失代码显示"—"。派生：pos52 = (price − lo52)/(hi52 − lo52)×100，off_hi = price/hi52 − 1，off_lo = price/lo52 − 1；hi52 = lo52 时 pos52 为空；标记：`short_history`（窗口前无交易且首个交易日晚于窗口起点 14 天以上 → "上市不足 52 周，区间按上市以来"），`stale`（最后交易日早于行情日 3 天以上 → 停牌），`basis_mismatch`（末日前复权收盘与快照价差 > 0.5%），`outside`（现价不在区间内）。

**机构目标价**（`em.research_reports()` + `em.share_bonus()`，`python -m cn cards` 的 `reports` / `bonus` 字段，仅评估卡公司）：

| 接口 | 参数 | 字段 → 输出键 | 结果 |
|---|---|---|---|
| `reportapi.eastmoney.com/report/list` | `qType=0&code=<code>&beginTime=<抓取日−180天>&endTime=<抓取日>&pageSize=100` | publishDate → date，orgCode / orgSName → org_code / org，indvAimPriceT / indvAimPriceL → target_hi / target_lo（无目标价时为空串，多数研报如此），infoCode → url（data.eastmoney.com/report/info/<infoCode>.html），title，emRatingName（只存数据层，页面不显示评级用词） | works（600519 近 180 天 38 篇，只有群益证券 2 篇给目标价 1430） |
| RPT_SHAREBONUS_DET | `(SECURITY_CODE="…")(EX_DIVIDEND_DATE>=窗口起点)` | 同上 | works |

规则（`cards.target_median()`，`CARD_RULES["target_window_days"]=180`、`["target_min_org"]=3`）：每篇目标价 = 上下限只给一个或相等时取该值，否则取中值；窗口内（且不晚于行情日）有送转除权时，除权前发布的目标价股本口径不同，剔除（该机构除权后若有新研报则用新的）；每家机构取最新一篇；≥ 3 家才给中位数，同时记录 n、min、max、窗口、最新日期和逐篇来源；不足 3 家显示"机构目标价样本不足，不显示"，**不用** RPT_WEB_RESPREDICT 的 DEC_AIMPRICEMAX/MIN 代替（统计窗口未公开）。现金分红不调整，只在卡片注明除息日。

`cards_<ts>.json` 结构：`fetched_at, snap（来源快照文件名）, codes, rules, cards{code: {profile, mainop, float_holders, consensus, pe_series（PE(TTM) 降采样至约 260 点）, pe_band{p20,p50,p80,n}, price, hist_range, deducted{report_date: {eps, eps_deducted, parent_net_profit, deducted_net_profit}}, reports{window, n_reports, with_target[]}, bonus[], errors}}, failed_fields, http`。快照文件不被修改；`build()` 读取最新的 cards 文件，只合并当前评估卡名单内的代码。

## k. 机构动向：跟踪机构在前十大流通股东中的 12 个月变化（`python -m cn holders`，只展示）

核验时间 2026-10-23。**不进入** ④ 综合分、价值 / 博弈标签、warn_n 和对比池 bkScore；build 在 `analyze()` 之后才挂到 ② 行（`r.inst`）和评估卡（`c.inst`）。

| 接口 | 参数 | 字段 → 输出键 | 结果 |
|---|---|---|---|
| RPT_F10_EH_FREEHOLDERS（前十大流通股东），**全市场** | `(HOLDER_NAME="香港中央结算有限公司")(END_DATE='2026-06-30')`，`SECURITY_CODE,HOLDER_RANK` 升序，500 行/页 | SECURITY_CODE，SECURITY_NAME_ABBR，END_DATE → period，UPDATE_DATE，HOLDER_RANK → rank，HOLDER_NAME，HOLDER_TYPE，HOLDER_NEWTYPE，SHARES_TYPE（只保留含"A股"的行），HOLD_NUM → shares（股），FREE_HOLDNUM_RATIO → float_pct（占流通 A 股 %），HOLD_NUM_CHANGE（带符号股数文本 / "新进" / "不变"）→ change_shares，HOLDNUM_CHANGE_NAME（新进 / 增加 / 减少 / 不变）→ status，CHANGE_RATIO | works：HKSCC 2026-06-30 2,844 行（无重复代码；新进 388 / 增加 1,270 / 减少 1,185 / 不变 1），6 页 |
| 同上，按股东类型 | `(HOLDER_TYPE="全国社保基金")` / `(HOLDER_TYPE="基本养老基金")` + END_DATE | 同上，再在本地按名称前缀匹配 | works（2026-06-30：社保 778 行、养老 207 行）。`like` 过滤被拒（code 9501"不支持like查询"），所以前缀规则只能先按类型取再本地匹配 |
| 同上，披露覆盖 | `(END_DATE='…')(HOLDER_RANK=1)` | SECURITY_CODE | works：各期有名单的公司 5,494 / 5,521 / 5,557 / 5,582 / 5,612（2025-06-30 … 2026-06-30），2026-09-30 697 家（部分披露） |
| RPT_SHAREBONUS_DET | `(EX_DIVIDEND_DATE>='2025-06-30')(EX_DIVIDEND_DATE<='2026-09-30')` | BONUS_IT_RATIO（每 10 股送转）用于股数调整 | works，6,332 行，449 家有送转 |
| RPT_MUTUAL_HOLDSTOCKNORTH_STA（北向持股） | — | TRADE_DATE，HOLD_SHARES，FREE_SHARES_RATIO，MUTUAL_TYPE（001 沪股通 / 003 深股通） | **未使用**：仍有数据，但每只股票只有一个日期（2026-10-23 时为 2026-09-30，`TRADE_DATE<'2026-09-30'` 返回空），没有历史，算不出 12 个月变化。港交所 2024 年 8 月起已停止逐日逐股披露北向持股 |

一共约 140 次请求（6 期 × 6 条规则 + 6 次覆盖查询，HKSCC 每期约 6 页），逐代码查询则要 1236 × 5 次。

跟踪名单 `cn/holders.py` `HOLDER_RULES`（匹配方式逐条声明）：香港中央结算有限公司（精确；陆股通名义持有人，汇总所有境外投资者，不代表单一机构；**香港中央结算(代理人)有限公司是 H 股名义持有人，名称不同，不计入**）；中央汇金投资有限责任公司、中央汇金资产管理有限责任公司、中国证券金融股份有限公司（精确）；全国社保基金（前缀"全国社保"，数据里有 1 处"全国社保一一零组合"缺"基金"二字；另含"XX基金管理有限公司-社保基金N组合"；名称含"划转"的国有股划转账户排除）；基本养老保险基金（前缀，"XX养老金产品"是企业年金 / 职业年金产品，不计入）。同一公司同一期多个社保 / 养老组合合计。

期间：法定披露截止日（一季报 04-30、中报 08-31、三季报 10-31、年报次年 04-30）已过的最近 5 个季末 = 4 个环比，约 12 个月；之后已有部分披露的季末作为 `partial_periods`（带已披露家数）单独显示，不计入状态。2026-10-23：2025-06-30 … 2026-06-30，2026-09-30 部分披露 697 家。

派生（`holders.derive()`，纯函数，smoke 有合成数据测试，含送转用例）：期初 = 5 期中公司第一个有名单的期，期末 = 最后一个有名单的完整期。两期都在前十：期初股数 × Π(1 + 每股送转)（除权日在 (期初, 期末]）后与期末比较 → 增持 / 减持 / 不变（相对差 ≤ 1e-4 记不变）；送转数据取不到时改比 float_pct（±0.005pp）并标 basis=float_pct。期初不在、期末在 = 新进前十；期初在、期末不在 = 跌出前十；只在中间出现 = 期间进出前十。另给 net_shares、net_pp（期末 − 期初占流通 %，解禁会影响）、相邻两期都在时的增 / 减季数、first_seen。**不在前十 ≠ 没有持股**，页面统一用"进入 / 跌出前十"措辞。

核验：smoke 中 000001 HKSCC 2026-06-30 全市场查询 = 逐代码 F10 查询（736,878,410 股）；无送转公司的相邻季方向与东财 HOLDNUM_CHANGE_NAME 一致 10,919 / 10,919；与 `cards_*.json` 的 float_holders 逐行比对（季末期）27 / 27 一致（HKSCC 23 行）。评估卡的 float_holders 取的是最新 END_DATE，个别公司是非季末（招股 / 上市公告书，如 2026-07-31），这些不在本数据里。

`holders_<ts>.json`：`fetched_at, today, periods[5], partial_periods[{period, n_reported, deadline}], deadlines, n_reported, reported{period: [codes]}, rules, fetch_rules, complete, failed, items{code: {rule_key: [{period, shares, float_pct, change_shares, status, n, rank, update, parts?}]}}, bonus{code: [{ex_date, bonus_it_per10, cash_per10}]}, bonus_ok, unmatched_top, src, http`。缓存 ttl：完整期 7 天、部分期 0.5 天；每次查询后写检查点；中断后重跑从缓存续上。

---

## 请求量参考

冷缓存完整跑一遍 smoke 约 140–160 次请求（≈ 2 req/s，约 1.5–2 分钟）；之后 1 天内基本全部命中缓存。
financials 每个报告期约 35 次；quotes（EOD）约 12 次；live=True 额外约 60 次。

## 未覆盖 / 需人工

- push2 实时分页在本机不稳定（大量 502），实时换手率/成交额/上市日期默认缺失；如需，可在非交易高峰时段 `quotes(live=True)` 再试。
- 金融类公司（银行/保险/券商）资产负债表字段缺失。
- 交易所官网监管信息公开栏目未接入；cninfo 标题检索结果需人工阅读原文判断性质。
- 申万/中信行业分类未接入（目前仅东财行业）。
