# 市场热度与抱团拥挤度：公开数据 API 核查

核查日期：2026-11-10。本文对应“市场情绪 → 市场热度”（原页面名称“市场情绪跟踪”）和“抱团拥挤度”；旧实现见[市场热度说明](market-sentiment.md)和[抱团拥挤度说明](market-crowding.md)。

## 最新需求及核验结论

用户在本轮进一步明确：两个模块只需每天取得当日数据、汇总后入库，不要求回补多年历史指标；能直接从接口取得的 MA20 等指标优先使用接口值，不默认在本项目重算。接口来源保持统一，不使用网页抓取。这些是新的接入要求，当前线上采集尚未切换。

核查原始代码后，结论如下：

| 模块 | 当天需要的数据 | 是否必须由本项目下载多年历史 |
| --- | --- | --- |
| 市场热度 | 当日 A 股名单、同一日期和复权口径的收盘价与 MA20；必要的当日状态说明 | 不需要。MA20 本身依赖过去 20 期，但提供方可以直接返回计算结果，本项目不必再下载 20 期价格自行计算 |
| 抱团拥挤度原始值 | 当日股票名单和每股完整成交额 | 不需要。按成交额排序取股票数量前 5%，计算这些股票成交额占全市场成交额的比例 |
| 抱团拥挤度 EMA 10 展示线 | 已保存的每日拥挤度结果 | 需要已累计的往日结果或等价递推状态，可以从启用日起积累，不要求回补多年个股行情 |

市场热度还存在一个明确的旧新口径差异：旧采集器计算 `above_count / eligible_count`，当日停牌和不足 20 日数据的股票不计分母；用户现在定义为 `above_count / universe_count`，分母为当日 A 股总数。接入时必须同时调整计算、状态阈值、页面说明和来源/口径标记，不能只替换 URL 后宣称二者相同。新股 MA20 为空、停牌以及技术请求失败必须分别识别；未知缺失不能冒充“未站上”，也不能悄悄缩小总股票数。

抱团拥挤度原始计算为 `K = max(floor(N * 0.05), 1)`，将当日成交额降序排序后，取前 K 只的成交额合计除以全体成交额合计。原实现将零成交额股票计入 N，因此停牌股不能因为当日成交额为零而从名单中删除。该公式见[采集脚本](../collectors/抱团拥挤度_数据库版.py)；EMA 10 只用于[展示平滑](../backend/app/services/crowding.py)。

之前将完整历史退市股票池和多年停牌记录作为本轮接入前置条件，扩大了任务范围；该前置条件撤销。下文保留的历史覆盖调查用于解释旧实现及接口能力，不再据此阻断只采当天的方案。既有历史记录保留，新来源从启用日起逐日积累；创红比从 2018 年采集的既有要求独立保留。

## 结论与当前状态

可以通过独立数据 API 直接获取收盘价、MA20 和成交额，在 Linux 上只做比较、计数、排序与汇总，再写入本项目 MySQL，技术上不必在聚宽运行。已确认 Tushare `stk_factor_pro` 文档同时提供这些字段；StockAPI 和麦蕊也有直接 MA 接口。以下区分文档支持、真实响应、日期有效性与授权要求，不将找到接口等同于已经通过全市场数据验收。

此前的日线验证中，东方财富的普通股、北交所和退市股样本成功；同花顺与腾讯的平安银行 2023 年 242 个收盘价、223 个自行计算的完整 MA20 窗口判断一致。这只是日线样本验证，不是直接 MA20 接口的验收证据，也不再作为默认生产实现。后续优先使用提供方直接返回的 MA20，不用未核验的自算值自动补缺。

本次完成页面改名及接口核查，市场热度的数据源尚未切换。现有聚宽采集结果与历史记录继续使用；没有把本次样本计算结果写入生产汇总表。

### 目前数据从哪里来，核验到什么程度

现有流程为：聚宽的逐股行情和证券资料 → 本项目采集脚本计算每日 MA20 广度 → MySQL 汇总表 `market_sentiment` → 网站本地 SQLite 快照 → 页面展示。页面更名没有改变这一链路。

[聚宽官方 SDK](https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/api.py)将 `get_all_securities(date=None)` 定义为平台支持的所有日期证券资料；按上市、退市日期还原股票池符合接口设计。[官方行情文档](https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf)也明确了 `fq='pre'` 的前复权含义和 `skip_paused=False` 的停牌填充规则。原脚本不主动排除 ST 或某个 A 股板块，但实际范围仍由上游返回决定，不能仅凭代码允许纳入就宣称北交所已经完整覆盖。

此次核验了计算实现、边界测试及接口语义，未对线上真实行情逐股逐日进行独立对账，也未取得线上证券清单证明全部历史退市股与北交所覆盖。因此，测试通过不等于全市场真实数据已经完全验真。数据时效应看页面的交易日“截至日期”；网站同步成功仅表示读到了源表已有结果。

## 接口边界

本模块的数据源替换只采用直接返回结构化数据的 API 或提供方正式 SDK。行情获取不解析 HTML、不模拟浏览器、不提取网页表格、不绕过登录或访问限制。阅读提供方的接口文档用于核查接口，并不属于将网页作为行情采集源。

需要区分两类接口：

- **提供方正式开放 API**：有接口、字段、权限与调用频率文档，例如 Tushare Pro、BaoStock。
- **公开可访问的行情端点**：请求直接得到 JSON，例如腾讯行情端点；这是数据接口调用，但未找到完整的对外服务契约，不能据此承诺长期可用或数据完整。

## 旧历史回填模式的数据需求

旧实现为同一交易日“收盘价严格高于前复权 MA20 的有效股票数 / 有效股票总数”。MA20 包含当天，按最近 20 个市场交易日计算。若完全复刻其多年历史回填，才涉及以下输入；本轮只采当天并直接使用 MA20 的方案不要求先重建整套多年资料：

| 输入 | 用途 | 不可直接替代的做法 |
| --- | --- | --- |
| 历史上市、退市日期和证券身份 | 逐日还原当时股票池，包含 ST、创业板、科创板、北交所 A 股，排除 B 股 | 用当前上市名单计算全部历史 |
| 收盘行情与复权信息 | 计算同一复权基准下的 MA20 | 将未复权价和供应商成品均线混用 |
| 完整交易日历 | 包含当天的 20 个市场交易日 | 简单取最后 20 条有成交 K 线 |
| 明确的全天停牌状态 | 历史停牌日填前收盘价，当日停牌不计入分母 | 把缺失行情一律认定为停牌，或把日内临停等同全天停牌 |
| 代码变更映射 | 证券更换代码后保留同一历史序列 | 新旧代码重复统计或重新计算新股预热期 |

上市期间无法解释的行情或状态缺口应保留为待核查项，不通过缩小分母生成看似成功的比例。

## 候选 API

### 1. Tushare Pro：可批量直接获取 MA20、收盘价和成交额

提供方有正式的 [HTTP POST / JSON 文档](https://tushare.pro/document/1?doc_id=130)。服务入口为 `api.tushare.pro`，请求体包含 `api_name`、`token`、`params`、`fields`，不需要在聚宽执行代码。

#### 1.1 优先验证直接指标接口

[股票技术面因子专业版](https://tushare.pro/document/2?doc_id=328)的 `stk_factor_pro` 支持按 `trade_date` 批量获取。文档明确列出 `close_qfq`、`ma_qfq_20`、`amount`、`ts_code` 和 `trade_date`，分别为前复权收盘价、20 期简单移动平均、成交额（千元）、股票代码和交易日；另有不复权和后复权字段，不能交叉配对。

请求体示例（未执行授权请求）：

```json
{
  "api_name": "stk_factor_pro",
  "token": "YOUR_TOKEN",
  "params": {"trade_date": "20261110"},
  "fields": "ts_code,trade_date,close_qfq,ma_qfq_20,amount"
}
```

本项目直接比较 `close_qfq > ma_qfq_20`，不重复计算 MA20；同一批数据的 `amount` 可用于抱团拥挤度排序汇总。服务端 MA20 的期间与停牌处理以提供方定义为准，不能仅凭同名字段认定与旧实现的 20 个市场交易日完全一致；这需要核实指标说明，不要求重新下载价格自算。成交额入库若沿用现有“元”单位，必须由接口的千元统一换算。股票分母使用同一提供方的当日完整股票名单，不能将指标响应行数直接当作全 A 股票总数，停牌、新股、未返回记录须逐项核查。

官方文档规定单次最多 10000 条，5000 积分每分钟 30 次，8000 积分以上每分钟 500 次。因此字段及批量方式已有官方依据，但需要有效 Token 和相应权限；本次没有授权账号，未实测该接口的当日全市场数据、北交所覆盖或 18 点就绪情况。不能将同名普通版 `stk_factor` 或客户端 `pro_bar(ma=...)` 直接当作上述服务端 MA20 结果。

#### 1.2 证券资料与旧方案参考接口

| API 名称 | 本模块所需内容 | 提供方文档 |
| --- | --- | --- |
| `stock_basic` | 上市、退市、暂停上市证券资料，`list_date` / `delist_date`，SSE / SZSE / BSE | [股票基础信息](https://tushare.pro/document/2?doc_id=25) |
| `daily` | 按 `trade_date` 获取当日全市场未复权收盘价；停牌期间不返回行情行 | [日线行情](https://tushare.pro/document/2?doc_id=27) |
| `adj_factor` | 按日期获取复权因子，在本地统一计算复权价格 | [复权因子](https://tushare.pro/document/2?doc_id=28) |
| `suspend_d` | 停复牌类型与日内停牌时间段 | [停复牌信息](https://tushare.pro/document/2?doc_id=214) |
| `trade_cal` | 交易日历 | [交易日历](https://tushare.pro/document/2?doc_id=26) |
| `bse_mapping` | 北交所新旧代码连续性 | [北交所代码映射](https://tushare.pro/document/2?doc_id=375) |

2026-11-10 的文档中，`stock_basic`、`adj_factor`、`suspend_d` 均要求至少 2000 积分。因此仅能试用未复权 `daily` 的最低档权限不足以完成这一模块。具体频率以各接口限制和账号权限为准，见[权限说明](https://tushare.pro/document/1?doc_id=290)。此次未提供 Token，未进行授权行情查询，也未购买或开通任何服务。

旧历史回填需合并 `list_status=L/D/P` 等适用状态后按历史日期筛选；只采当天则核对当天应在统计范围内的名单，无需回建多年退市主表。北交所、全天停牌、除权日和新上市不足 20 日的当日样本仍需实测。`suspend_d` 文档注明不定期更新，成功返回空列表本身不能证明行情缺口就是停牌或该市场状态已经完整。

原始行情与因子 API 仅作为旧历史方案或核验工具参考。新的日常采集优先采用 1.1 的现成 MA20，并明确采用用户要求的总股票分母；不默认重算均线。

### 2. BaoStock：免费正式数据接口，北交覆盖待验证

[官方 Python 包](https://pypi.org/project/baostock/)提供自有数据服务器接口。主要调用包括：

```python
bs.login()
bs.query_stock_basic()
bs.query_all_stock(day="2026-11-09")
bs.query_trade_dates(start_date="2023-01-01", end_date="2026-11-09")
bs.query_history_k_data_plus(
    "sh.600000", "date,code,close,tradestatus,isST",
    start_date="2023-01-01", end_date="2026-11-09",
    frequency="d", adjustflag="2",
)
```

SDK 支持前复权、`tradestatus`、上市及退市资料，协议为直接数据服务调用，并非网页抓取。已检查的 0.9.4 版还包含 `query_daily_history_k_AStock(date)` 全市场日线方法；未验证该方法权限及覆盖，不能据旧版本资料断言每日必须逐股请求。

[官方证券资料文档](https://www.baostock.com/mainContent?file=stockBasic.md)列出 `ipoDate` / `outDate` 等字段，代码格式明确描述了 `sh` / `sz`，未据此确认北交所支持。此次仅尝试一次官方 SDK 匿名登录，当前执行环境返回网络接收错误 `10002007`，未能获取证券目录。该结果不能用来断言平台服务故障，也不能证明北交所一定不支持。

因此，BaoStock 是值得进一步验收的免费 API 候选，当前证据不足以将原有含北交的指标静默改为其数据口径。[官方复权算法说明](https://www.baostock.com/helpdocs/pdf/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf)也应纳入跨来源对账。

### 3. 腾讯直接 JSON 行情：个股样本可用，缺少完整状态链路

直接请求地址：

```text
GET https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get
param=sz000001,day,2026-10-01,2026-11-10,100,qfq
```

同一路径使用 `param=bj920599,day,2023-11-01,2023-11-10,100,qfq` 查询北交所股票，使用 `param=sz000046,day,2023-11-01,2024-02-07,100,qfq` 查询已退市证券历史。此次三次样本请求均返回 HTTP 200、业务码 0 和日线 JSON；按请求日期裁剪后分别取得 24、8、41 行。北交及退市样本均取到了 2023 年的历史，不能笼统声称这些市场的历史取不到。

但返回日线没有足以复现旧规则的明确逐日停牌状态。接口请求窗口还需要对返回日期严格裁剪，不能认为请求参数就证明返回记录完整。完整历史证券主表、新旧代码映射及缺失行原因均未完成验证；这些旧历史回填问题不再作为新需求的前置条件。新的直接指标方案仍需验证实际 MA20 字段，不能仅凭 K 线取数成功就视为满足。

另外依据 [AKShare 腾讯当前行情源码](https://github.com/akfamily/akshare/blob/main/akshare/stock/quote/stock_zh_a_tx.py)，在 2026-11-10 10:22 UTC 对以下名单接口做了一次正常匿名请求：

```text
GET https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList
_appver=11.17.0&board_code=aStock&sort_type=price&direct=down&offset=0&count=5
```

返回 HTTP 200、`code=0`，`data.rank_list` 为 5 条、`data.total=5580`。有 `code`、`name`、`zxj`、`turnover`、`volume`、`state`、`stock_type` 等字段，本次响应没有 MA20 数值字段或明确交易日；`zdf_d20` 不能作为 MA20，空字符串 `state` 也没有被认定为已验证的正常交易标记。源码描述“沪深京”，但本次 5 条均为沪市，声明总数与同期东财 5928 不同，不能据此确认全 A 或北交所完整覆盖。当前可记录名单端点和分页方式，不能据此宣称已取得可入库的当日全市场 MA20。

### 4. StockAPI：有文档接口，本次匿名额度已耗尽

提供方公开了以下 GET / JSON 接口：

- `https://www.stockapi.com.cn/v1/base/day`：前复权日线，[接口文档](https://www.stockapi.com.cn/menu/11)。
- `https://www.stockapi.com.cn/v1/quota/ma2`：指定周期均线，[接口文档](https://www.stockapi.com.cn/menu/24)。

此次以 `code=600000`、`startDate=2026-10-01`、`endDate=2026-11-09`、`calculationCycle=100` 直接请求两个接口，MA 请求额外带 `ma=20`。两者均 HTTP 200，但 JSON 业务码为 `88886`，提示当日匿名免费调用额度已耗尽，未返回行情。

MA 文档明确列出 `ma20`，设置 `ma=20`、`calculationCycle=100` 并按当天查询即可请求日线 MA20，无需本项目先下载 20 天价格。接口需要逐股请求，文档列出普通请求每日免费 1000 次；实际匿名额度已耗尽。尚未确认 MA20 与其日线的复权基准及当日完整性，因此“接口存在”成立，“本次已取得可入库的 MA20”不成立。

### 5. 东方财富：公开 JSON 已取得日线，Choice 另有正式 SDK

#### 5.1 可直接请求的公开 JSON 端点

| 用途 | GET 地址 | 核心参数及字段 | 本次状态 |
| --- | --- | --- | --- |
| 历史日线 | `https://push2his.eastmoney.com/api/qt/stock/kline/get` | `secid`、`klt=101`、`fqt=1`、`beg`、`end`；`data.klines` | 普通股、北交所和已退市样本成功 |
| 当前证券列表/行情 | `https://82.push2.eastmoney.com/api/qt/clist/get` | `fs`、`pn`、`pz`；`data.total`、`data.diff`，代码 `f12`、市场 `f13`、名称 `f14` | 两页各 5 条成功，无重复，含 920 北交所代码 |
| 个股资料 | `https://push2.eastmoney.com/api/qt/stock/get` | `secid`；`f57` 代码、`f58` 名称、`f189` 上市时间 | 字段映射由源码确认；本次请求 HTTP 502 |
| 停复牌资料 | `https://datacenter-web.eastmoney.com/api/data/v1/get` | `reportName=RPT_CUSTOM_SUSPEND_DATA_INTERFACE`、`columns=ALL`、分页和 `filter` | JSON 成功，历史筛选及完整性未通过验证 |

调用方式参考 [AKShare 官方日线源码](https://github.com/akfamily/akshare/blob/main/akshare/stock/quote/stock_hist_em.py)、[个股资料源码](https://github.com/akfamily/akshare/blob/main/akshare/stock/quote/stock_info_em.py)、[分页实现](https://github.com/akfamily/akshare/blob/main/akshare/utils/func.py)和[停复牌源码](https://github.com/akfamily/akshare/blob/main/akshare/stock/issue/stock_tfp_em.py)。AKShare 是调用封装，不是这些行情的原始提供方；也不能因为某个函数来自 AKShare 就默认它一定不解析 HTML，应逐个检查实现。本节端点均直接返回结构化数据。

日线可复现请求参数：

```python
{
    "secid": "0.000001",
    "klt": "101",
    "fqt": "1",
    "beg": "20231001",
    "end": "20231110",
    "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
    "ut": "7eea3edcaed734bea9cbfc24409ed989",
}
```

`ut` 是上述公开源码使用的常量，不是用户账号密钥。`fqt=0/1/2` 对应不复权/前复权/后复权；`klines` 每条是逗号分隔的日期、开盘、收盘、最高、最低、成交量、成交额等。本次请求含 `f116`，实际每行仍只有 11 列，解析时必须按实际结构验证。

2026-11-10 09:45–09:47 UTC 的样本：

| `secid` | 请求区间 | 实际日期区间 | 条数 | 结果 |
| --- | --- | --- | --- | --- |
| `0.000001` | 2023-10-01～2023-11-10 | 2023-10-09～2023-11-10 | 25 | HTTP 200，`rc=0` |
| `0.920599` | 2023-11-01～2023-11-10 | 同请求交易日区间 | 8 | HTTP 200，`rc=0` |
| `0.000046` | 2023-11-01～2024-02-07 | 2023-11-01～2023-12-27 | 41 | HTTP 200，`rc=0` |

北交所样本证明这个现代码可取到该段旧代码时期的历史；退市样本证明仍能取到部分退市证券行情。两者都不能推广为全部证券完整覆盖。

当前名单筛选为 `fs=m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048`。本次返回声明 `total=5928`，只核验前两页各 5 条，未下载全部并与交易所对账，不能将该总数当作已验证的全市场数量。未核实该接口有历史日期筛选能力，也未核实完整退市证券主表。

**停复牌接口不能按成功响应直接使用。** 按公开源码传入 `filter=(MARKET="全部")(DATETIME='2023-12-28')`，两页共 962 条，对应 962 个不同证券，但停牌开始日期跨 2023-12-21～2026-11-10；查询 2024-01-02 的第一页与前述第一页相同。返回字段包含 `SUSPEND_START_TIME`、`SUSPEND_END_TIME`、`SUSPEND_EXPIRE`、`PREDICT_RESUME_DATE` 等，并混合盘中、一日和连续停牌。本次结果既不是已验证的指定历史日完整停牌布尔表，也未证明是完整历史事件流。不能用“没有记录”判断正常交易，或直接据此填补缺失日线。

同一北交所样本 920599 的 2023-11-01 / 11-10，东方财富前复权收盘为 6.10 / 6.88，上次腾讯样本为 6.115 / 6.895。未判断哪个来源错误；这提示不能将不同来源的前复权价格直接混入同一个均线窗口，应保留来源和复权基准，使用统一序列计算。

#### 5.2 东方财富 Choice / EMQuantAPI

东方财富另有正式的 [Choice 量化 API](https://quantapi.eastmoney.com/)和[Python 手册](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Python.html)，与前述网站 JSON 端点不同。支持 Ubuntu/CentOS，`c.csd` 获取日序列，`AdjustFlag=3` 为前复权；`c.css` 获取基础资料，`c.sector` 获取板块证券，另有交易日历。需要账号授权，当前版本支持 API Key 登录，官网可申请试用；本次未使用授权账号查询。

手册将 `sector` 的历史成分查询明确限定为沪深股票、上交所期权，其他板块只取最新成分。因此，不能仅凭“有板块接口”就认定北交所历史股票池也已解决。历史停牌字段、退市覆盖和对应权限还须在授权账号中验收。

### 6. 同花顺：iFinD 正式 HTTP API 与公开行情端点

#### 6.1 iFinD 正式数据接口

[官方手册](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/manual.html)明确支持 JSON 请求的 HTTP API：

| 用途 | POST 路径（前缀 `https://quantapi.51ifind.com/api/v1/`） | 对应 SDK |
| --- | --- | --- |
| 历史行情 | `cmd_history_quotation` | `THS_HQ` |
| 基础资料 | `basic_data_service` | `THS_BD` |
| 日期序列 | `date_sequence` | `THS_DS` |
| 历史板块/专题资料 | `data_pool` | `THS_DR` |

请求头使用 `access_token`。本次无凭证 POST 返回 HTTP 401、业务码 `-1302`，没有获得授权行情。

历史行情支持多股票、日周期和非交易间隔处理。`CPS=2` 为分红再投前复权，`CPS=6` 为现金分红前复权，二者不能只因都叫“前复权”就当作相同口径。[官方示例](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/example.html)展示 `THS_DR("p03291", "date=20250514;blockname=001005010;iv_type=allcontract", ...)` 获取指定历史日全部 A 股名单；具体停牌/上市退市字段和北交覆盖仍需授权查询确认。

[权限页](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/permission.html)列出免费档历史行情每月 100 万单元格、单次 5 万单元格，并有历史年限限制。该页称有 iFinD 账号即可使用免费档，[FAQ](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/faq.html)则将免费账号描述为购买终端附带的少量接口权限。应以实际账号权限为准，不能承诺新注册即能匿名免费回填全市场。

#### 6.2 同花顺公开 JSONP 行情：已完成单股全年交叉验证

实际成功的直接 GET：

```text
https://d.10jqka.com.cn/v6/line/hs_000001/01/2023.js
https://d.10jqka.com.cn/v6/line/hs_000001/01/last.js
```

这是 JavaScript 回调包裹的结构化 JSON（JSONP），不需要解析 HTML 或模拟浏览器；解析时应验证固定回调格式并解析 JSON，不能执行返回脚本。本次正常请求未携带浏览器 Cookie、签名或伪装浏览器 UA。

2023 年平安银行样本与腾讯 `param=sz000001,day,2023-01-01,2023-12-31,640,qfq` 比较结果：

| 检查 | 结果 |
| --- | --- |
| 同花顺实际唯一日线 | 242 条，2023-01-03～2023-12-29 |
| 独立交易日历对齐 | 242 个交易日，缺失和额外日期均为 0 |
| 腾讯裁剪后同区间日线 | 242 条 |
| 242 个共同日期的收盘价差 | 最大绝对差、平均绝对差均为 0 |
| 仅使用 2023 年数据的完整 MA20 窗口 | 223 个 |
| 两源 `close > MA20` 判断差异 | 0 个 |

这是“该股票、该年度、该次取数”的一致性证据，不是所有股票或与聚宽完全等价的证明。未找到同花顺对 `/v6/line/.../01/` 供第三方使用的正式接口文档或复权算法说明，不仅凭路径编号断言完整算法。

本次抓取时，`last.js` 的元数据 `today=20261110`，但实际最后日线是 2026-11-09，返回 140 条；必须检查数据行日期，不能用 `today` 或总历史条数判断当天或全历史已经到齐。同一路径下北交所 `hs_920599/01/2023.js` 本次返回 404；这只说明该样本路径未成功，不代表同花顺所有产品均不支持北交所。完整历史股票池和停牌状态也未由此端点解决。

#### 6.3 Fuyao：直接 REST 与批量历史文件，提供方归属待确认

另发现 [HiThink-Tech/Financial-API](https://github.com/HiThink-Tech/Financial-API) 项目及 `fuyao.aicubes.cn` 文档站，二者自述为“同花顺金融数据 API”。本次已核对其公开接口文档，并完成一次无凭证请求；尚未通过同花顺官方主站的交叉链接独立核实该服务的官方归属，因此将其列为待确认提供方身份的候选服务。它与 iFinD 的接口地址和授权方式不同。[REST 契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/README.md)规定基础地址 `https://fuyao.aicubes.cn`，通过 `X-api-key` 鉴权，公开数据端点采用 GET。它还提供[全市场批量数据接口](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/market-dumps.md)：

| 路径 | 文档描述的用途 |
| --- | --- |
| `/api/a-share/prices/historical` | 个股历史日线 |
| `/api/meta/tickers/list` | 当前证券目录 |
| `/api/dump/market-dumps/daily-k/download-url` | 约十年全市场未复权日线 Parquet |
| `/api/dump/market-dumps/daily-k-10d/download-url` | 最近十个交易日增量日线 Parquet |
| `/api/dump/market-dumps/adjustment-factors/download-url` | 分红、送股、配股事件 Parquet，供本地推算复权因子 |

文档描述的是结构化文件下载接口：下载端点返回短期授权地址，再通过该地址取得文件，不需要解析网页表格。批量初始化和增量维护适合本地数据库方案；上述覆盖范围是提供方文档声明，本次没有取得授权全量文件。

本次不带 Key 请求证券目录，返回 HTTP 200，但业务码 `2003`、`Missing X-api-key`、`data=null`，所以没有取得证券数据。当前目录及 `end_date` 等通用字段不能未经验证就当作完整历史退市主表；本次也未找到可直接证明全市场历史全天停牌覆盖的字段。需有效 Key 和权限，未注册、开通或承诺匿名免费使用。

### 7. 通达信：应区分官方 TdxAiData 和第三方协议客户端

#### 7.1 官方 TdxAiData

[通达信官方说明](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1hjbgqpdhv114.html)及其推荐的 [tdxaidata Python 包](https://pypi.org/project/tdxaidata/)支持 Windows/macOS/Linux；后台数据模式无需启动通达信客户端，需在官方商城会员中心创建数据服务 Key。行情及数据服务默认使用 `aihs.tdx.com.cn:7709`、`aids.tdx.com.cn:7727`，不是网页抓取。本次未创建账号、取得 Key、购买额度或调用授权服务。

[历史 K 线文档](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1ctuhthaq5qmg/mindoc-1h10g60jt68sc.html)支持多证券、起止日期、日线及前后复权。可使用的接口包括：

```python
from tdxaidata import tqs

tqs.get_market_data(
    field_list=["Close", "Volume"],
    stock_list=["600000.SH", "000001.SZ"],
    period="1d",
    start_time="20230101",
    end_time="20231231",
    dividend_type="front",
    fill_data=False,
)
```

此为按文档整理的授权调用示例，未执行。先保留缺失行，再由已验证的停牌资料处理；`fill_data` 的一般空缺填充不能代替明确停牌状态。

另有 `get_stock_list`、`get_stock_info`、`get_trading_dates`、`get_divid_factors`。全 A 股票目录须显式使用 `get_stock_list(market="5", list_type=0)`，北证 A 股为 `market="53"`；已检查的 1.3.1 源码默认目录为上证主板，与使用文档的默认描述不同，不依赖省略参数。[基础信息文档](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1ctuhthaq5qmg/mindoc-1h10jj7r7jol4.html)的 `HSStockKind=2` 明确列出北证 A 股、`J_start` 为上市日期；`IsQuitGP` 只是退市整理板标记，不是完整历史退市日期。

已检查的 `get_stock_list` / `get_stock_list_in_sector` 无历史日期参数，标准日线字段也未列出逐日 `paused`。当前快照 `TPFlag` 与最近停牌日期不能直接替代历史状态。专业历史接口 `get_gpjy_value(stock_list, field_list, start_time, end_time)` 的 `GP29` 则提供复牌事件及此前停牌交易日数，可辅助重建已结束区间；仍停牌、停牌中退市、历史范围及北交覆盖需另行验收。因此不能断言“没有任何历史停牌资料”，也不能据此宣称完整逐日状态已解决。

本次下载并仅静态读取 1.3.1 官方推荐包的使用文档和源码，校验了 PyPI SHA-256，未加载动态库。`get_global_data(type=0)` 没有历史日期参数，其均线上下方家数字段是 `MA5_UP` / `MA5_Down`，不是本模块的历史 MA20，不能直接替换。

#### 7.2 pytdx / mootdx 第三方客户端

这类客户端直接访问通达信行情协议，不是 HTML 爬虫，但应与官方 TdxAiData 区分。[pytdx 项目文档](https://pytdx-docs.readthedocs.io/zh-cn/latest/pytdx_hq/)列出 `get_security_bars`、`get_security_list`、`get_xdxr_info` 等方法。已检查的 [mootdx 0.11.7 股票目录实现](https://github.com/mootdx/mootdx/blob/e99ae34382d970c68654c6d17c45512e728f130d/mootdx/quotes.py)及[市场映射](https://github.com/mootdx/mootdx/blob/e99ae34382d970c68654c6d17c45512e728f130d/mootdx/utils/__init__.py)仍有沪深限制，其中 920 前缀会误判为沪市；不能因出现北交所常量便认定完整支持。历史日线、除权除息、历史证券主表和停牌状态仍须组合验收。本次一次普通 TCP 连通尝试因当前环境网络不可达而结束，不能据此判断通达信服务器故障。

### 8. 米筐 RQData：正式 HTTP / Python 接口，状态语义贴近现有口径

[官方 HTTP 文档](https://www.ricequant.com/doc/rqdata/http/data-process)给出 `POST https://rqdata.ricequant.com/auth` 获取凭证，再向 `https://rqdata.ricequant.com/api` 发送方法和参数，结果是结构化 CSV 文本；这仍是数据 API，并非 HTML。需要账号和相应数据权限，本次未执行授权查询。

[通用接口](https://www.ricequant.com/doc/rqdata/python/generic-api)包括 `all_instruments(type="CS", date=...)`、上市与退市日期、`get_price(adjust_type="pre", skip_suspended=False)` 和交易日历。[A 股接口](https://www.ricequant.com/doc/rqdata/python/stock-mod.html)另有 `get_ex_factor`，以及语义明确为“全天停牌”的 `is_suspended`。从公开接口功能看，适合进一步验证原 MA20 规则；沪深北实际样本、历史退市和账号覆盖仍须实测，不将文档能力等同于全量数据已对账。

### 9. 麦蕊智数：有沪深京日线与停牌字段，演示请求未通过区间校验

提供方有[沪深 API 文档](https://mairui.club/hsdata)、[北交所 API 文档](https://mairui.club/bjdata)和[接入说明](https://mairui.club/linkmeans)。直接 GET 路径分别为：

```text
https://api.mairuiapi.com/hsstock/history/{code}/d/f/{licence}
https://api.mairuiapi.com/bj/history/{code}/d/f/{licence}
```

支持起止日期及条数参数；文档列出 `c` 收盘价、`sf` 停牌标记，`f` 为前复权，需有效 licence。未取得其完整历史退市主表的验证证据。

本次仅使用官网明确列出的公共演示凭证，向平安银行日线请求 2026-11-01～2026-11-10、`lt=30`。返回 HTTP 200 / JSON，但实际是 2025-09-15～2025-12-01 的 50 条，区间和条数均与请求不符。只能视为取得演示响应，不能据此确认授权接口当前行情可用，也没有将演示数据入库。

补充核查了直接 MA 接口：沪深路径为 `/hsstock/history/ma/{code}/d/f/{licence}`，北交所为 `/bj/history/ma/{code}/d/f/{licence}`；两份官方文档均明确列出 `ma20`，其中 `d` 为日线、`f` 为前复权。2026-11-10 10:16 UTC 使用官网公共演示凭证，请求 `st=20261110&et=20261110&lt=1`：平安银行返回 HTTP 200 和一条 `ma20=11.055`，但日期为 `2025-12-01 00:00`，未通过当天日期校验；北交所 920599 请求 `ReadTimeout`，未重复请求。不能将这个旧日期 MA20 用于当天市场热度。

### 10. 迅投 xtquant / MiniQMT：有正式 Linux 行情路线，须区分权限

[官方 Linux 指南](https://dict.thinktrader.net/nativeApi/Linux版xtquant快速开始指南.html)要求投研专业版及以上权限，Linux 支持 `xtdata` 行情模块、不支持 `xttrade` 交易模块。常见券商 MiniQMT 的客户端授权与这一后台数据服务权限不是同一回事，不能默认普通券商账号具备 Linux 数据服务权限。

[XtData 官方 API](https://dict.thinktrader.net/nativeApi/xtdata.html)包括历史行情下载、`get_market_data_ex`、`get_divid_factors`、`download_history_contracts`、`get_instrument_detail`、交易日历及证券板块。基础资料有 `OpenDate`、`ExpireDate`；日线 `suspendFlag` 区分 `0` 正常、`1` 停牌、`-1` 当日起复牌，不能把非零值一律当停牌。复权提供 `front` 和 `front_ratio` 等不同选项，仍要固定算法并对账。

相比只返回 OHLC 的接口，它的历史合约、上市退市日期和停牌字段在功能上更贴近本项目。[官方历史板块说明](https://dict.thinktrader.net/dictionary/industry.html)可辅助查询过期证券；实际沪深北历史覆盖、代码连续性及账号数据完整性尚未实测。本次没有对应授权、没有连接其数据服务，不能报告已经可直接替换聚宽。

## 入库方案与迁移边界

### 统一来源约束

用户补充要求“用同样的接口”。接入方案按统一数据来源处理：两个模块优先共用同一提供方的当日股票名单、收盘价、直接 MA20 和成交额，并核对该提供方的日期、复权与必要状态说明。一个提供方可以有多个专门 API 路径。跨来源数据可以用于验收比较，不在生产输入中自动混拼，也不在接口失败时改用未经核验的自算 MA20。

创红比当前使用腾讯 `newfqkline/get` 为主行情、东方财富为缺口备用、新浪为独立交易日历。已验证的腾讯日线响应不等于已验证的直接 MA20 响应；是否能沿用同一端点，要按真实返回字段判断。当前任务不再以腾讯完整多年历史证券主表缺失作为阻碍，但也不能将“能取 K 线”写成“已直接取得 MA20”。

### 计算与写入

确定并验收来源后，采集器在 Linux 直接请求当日快照，在本地汇总并写 MySQL。新的数据链路应保留以下内容：

1. **当日股票名单**：代码、市场、名称、名单日期和来源，保存启用日起每日实际名单；不为初始化重建多年退市股票池。
2. **接口原始指标**：当日收盘、提供方返回的 MA20、成交额、状态、复权标识与取数时间。保留当天必要证据便于检查，不默认保存多年逐股价格或重算均线。
3. **每日汇总**：市场热度保存站上数、总股票数、可判断数量、缺失原因和比例；抱团拥挤度保存 N、K、前 K 成交额、总成交额和比例。数据齐全且校验通过后按交易日事务提交。
4. **失败补采**：从启用日起记录未完成日期、证券和原因，在接口可查询对应日期且已保存当日名单时重试；不能用今天的名单冒充此前失败日名单。未解释的缺失不补零、不缩小总数、不写成功标记。

首次采集只处理启用日，之后按交易日累计；现有历史记录不删除。网站当前源库账号为读取用途，新增独立写入任务需相应写入权限。若采用北京时间 18:00 触发，仍必须以返回记录日期与完整性判断是否就绪，接口晚更新则进入后续重试，不能把昨日指标当今日值；此新链路及定时任务尚未上线。

切换前验证当日名单分页/完整性、记录日期、数值单位、收盘价与 MA20 复权一致性，并核查新股、停牌、ST 和北交所样本。总股票分母与旧有效样本分母的差异须有口径标记，展示状态计算也应随之调整。当前市场热度旧表只有日期主键及汇总值，不能把不同分母计算的历史曲线无标识拼接。

## 本次建议

优先采用同一提供方直接返回 MA20、匹配收盘价和成交额的接口，再做用户定义的横截面汇总。已核实文档中，Tushare `stk_factor_pro` 在字段与按日批量形式上最直接，但当前缺少授权 Token；这是待账号实测的候选，不是已选定购买或已上线来源。StockAPI 与麦蕊的直接 MA 接口已记下，但本次分别受匿名额度和返回日期不符影响，不能以 HTTP 200 就判为可用。

东方财富、同花顺和腾讯的原始行情端点已有成功样本；其直接 MA20 字段需另行验证，不能把普通日线接口算作成品指标接口。iFinD、Choice、通达信等正式服务也保留为候选，但只有实际核实到所需指标字段、账号权限与当日数据后才接入；不靠猜测字段或下载网页实现。

此次结论是：**两个模块均可按日采集并逐日积累，市场热度可以直接使用 API 的 MA20；本项目只做约定的比较、计数与成交额汇总。**完整多年历史不再是上线前提。接口和核查结果已记录，当前尚未取得通过当日全市场验收的直接 MA20 数据源，也没有上线新的数据源或分母口径。
