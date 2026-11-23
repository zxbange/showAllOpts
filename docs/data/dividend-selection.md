# 高股息低估值

## 网站当前采集

公开源非银行最多 50 只、银行最多 20 只，两组独立按静态股息率降序排名，同股息率按代码排序；条件不足不补齐。剔除 ST、已确认全天停牌及上市未满三周年股票，静态股息率严格大于 3%，且 `0 < PE-TTM < 12`。默认交易日 23:00 由[业绩选股调度](../operations/earnings-schedule.md)启动，早于奶牛股。数据库及网页保留完整两组名单，最多 70 行。旧快照不自动扩充，下一次成功采集后整体替换；失败仍保留上次成功结果。

## 聚宽兼容脚本（旧口径）

独立入口 `collectors/高股息选股_数据库版.py`；已从聚宽批量入口移除，独立脚本保留。

- 剔除 ST、选股日停牌股票及上市未满三周年股票；静态股息率严格大于 3%，PE 严格大于 0 且小于 12。
- 非银行最多 25 只，银行最多 10 只；两组按股息率降序独立排名，条件不足不补齐。
- 静态股息率 = 上一财年归属且截至采集日已实施的现金分红 / 当前总市值。
- 展示最近三个完整财年的股息率，统一使用采集日市值计算；不代表各年度历史股价下的收益率。

## 聚宽旧口径采集与共享存储

配置在 `mysql_connector.py`。默认处理最近完整交易日，历史模式见[历史档案恢复](selection-history-recovery.md)。
规则版本为 `dividend_pe12_age3_active_v8`。不再查询纯高股息所需的 PEG、财务增长和三年累计分红比例。

使用现有表：

| 表 | 用途 |
| --- | --- |
| dividend_selection_snapshot | 最新运行日期、扫描数量、候选和入选总数；固定 id=1 |
| dividend_selection_stock | 当前 value（非银行）与 bank（银行）名单；主键为 method、code；旧脚本最多35行，网站最多70行 |
| dividend_selection_archive | 已有历史档案，按日期、来源、规则版本隔离 |

当日结果校验通过后事务替换当前名单；内容相同不重写，失败保留旧结果。新字段仅做可空增补，不删除旧表、旧数据或历史档案。

## 页面与同步

“业绩选股”保留奶牛股及两个低估值板块。页面展示排名、行业、静态股息率、所选财年和前两个财年的股息率提示、PE/PB、市值和收盘价。
同步读取当前小规模名单。旧规则快照提示重新采集，不将旧 PE 门槛结果冒充新版结果。

上市年限：银行与非银行共用股票池，先按选股日期过滤上市未满三个周年的股票，再读取ST、行情及财务。恰满三周年保留；2月29日上市的非闰年周年按2月28日计算，不按1095天近似。上市日期缺失或无效则停止，保留已有快照。历史补采使用各历史选股日判断，不使用今天的上市年限。

## 新预案口径与聚宽边界

网站当前公开源规则为 `dividend_dps_proposal_age3_annual_history_v10`：以年度末期董事会预案首次公开为切换财年的门槛，同一财年有效的正常季度、中期、末期税前每股现金分红合计，除以当前A股股价。中期公告不能提前切换财年；年度不分配会触发切换，末期为零，同年已有中期/季度仍计入。有效调整或取消以观察时点为准。

聚宽兼容脚本**尚不是这个新口径**，不能用 v8 已实施现金额结果冒充 v9 或 v10。官方说明 `STK_XR_XD` 每报告期记录会随方案进度更新，取消也更新原记录；现有字段与取消/调整的完整语义未经同口径实证，不能仅按首次公告日期过滤当前记录便声称可还原历史预案。参见 [聚宽技术支持](https://www.joinquant.com/community/post/detailMobile?postId=21247)。本轮不猜字段、不新增未经验证的同口径聚宽适配。

原 `mode='history'` 的旧口径修订后重算归档与已有档案保留，仍遵守 `revised_asof` 限制，不代表原始时点版本。脚本在事务锁内拒绝以旧规则覆盖已发布的 v9 或 v10 最新快照，即使旧规则日期更新也不允许；旧快照恢复到最新表同样受此保护。需要旧研究结果时使用独立历史档案，不回写网站当前结果表。

### 当前财年与前两年展示

东方财富 `RPT_F10_DIVIDEND_MAIN` 数据用于核验分红方案。不能累加重复的年度现金总额；当现有方案进度证明方案已公告时，也不以进度日期倒推原始公告日期。公开任务接受当日已完成交易，不能据此复原任意历史时点。当前财年按有效税前 A 股每股派息除以当日收盘价；对应 `dividend_per_share` 和 `dividend_fiscal_year`，不伪造旧口径的 `dividend_cash_1y`。

v10 对入选股另查前两个财年的已实施每股现金分红和 Tushare 返回的当年实际成交日不复权均价；不再以前两年分红除以当前股价。股本变化未核验、数据缺失或历史接口不可用时，历史项留空，不填零、不撤销已验证的当期排名。当前财年的分红或候选范围无法核验时仍停止发布。真实股本变化需有效 A 股除权日及可核验送转比例，不能用总股本比值代替。

### 历史复验记录：2026-11-16（旧 v9、非生产）

以下为当时的离线复验，不代表当前 50/20 名单上限下的完整运行或线上成功。

A bounded read-only run reached 276 PE-filtered candidates. All 276 current
proposal records parsed successfully. The initial equity-event cross-check
left four unresolved securities. Three had explicitly zero current-fiscal DPS
and therefore cannot pass the strict positive-yield threshold, regardless of
share-unit factors. They are excluded without inventing adjusted history.

For 601211.SH, original transaction completion announcements establish that the
2025-03-13 A-share issuance and 2025-03-14 H-share merger issuance did not multiply
original A-share holders' units. Exact security/date/reason/reported-total tuples
are bound to those documents; unspecified changes elsewhere remain blocked.
Following these checks, replay of the actual current-day source responses passed
all 276 candidate proposal/equity checks and classified 152 qualifying stocks,
producing 25 non-bank and 10 bank results. This read-only validation did not write
to the production database. It validates that historical sample, not future
source availability or arbitrary historical reconstruction.

Differential dividends are not interpreted by choosing the highest tranche or
by treating every "small shareholder" label as the public investor category.
The evidence registry binds 600989.SH's 2023–2025 final distributions to exact
published implementation versions and ordinary publicly traded A-share holder
eligibility. Its 2025 ordinary-holder annual DPS is 0.7721, including the interim
0.28. An unrecognised company, changed publication, changed amount, or changed
ex-date remains pending verification; this bounded registry is not a universal
solution and a future differential plan can block automatic publication.

## 股票池跨来源覆盖校验

公开源完整分页后核对 AShareHub 与东方财富股票池。共同证券的上市日期必须一致；单边证券仅在上市日期有效、不晚于采集日且明确未满三周年时排除，不读取其行情和财报。满三周年的单边证券、重复代码、日期缺失/无效/冲突仍停止采集并保留上次成功结果。日志输出来源数量和最多十个差异证券代码，便于排查数据源更新不同步，不记录凭据或完整响应。
