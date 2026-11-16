# 奶牛股

入口：**业绩选股 → 奶牛股**（首个标签及默认入口，`/cash-cow`）。管理员点击“更新全部周期”通过多源组合同时计算近3、5、10年结果，运行时可以停止整个任务。计算结果先写入 MySQL，再发布本地缓存，页面只读取本地结果。

支持近 **3 / 5 / 10 年**切换，默认10年。年限选择器只切换查看结果，不启动或拆分采集。统一读取近10年财务和行情数据，对同一全A股票池分别计算三个窗口，不从10年入选名单截取3年或5年结果。缺少较早年份资料不妨碍对资料完整的较短窗口进行判定。

## 外部采集与数据库存储

- 来源为 AShareHub、东方财富和 Tushare 组合。保留“更新全部周期”及月末定时采集，移除聚宽独立和批量采集入口。
- 使用网站系统设置中的 MySQL 配置，沿用 `cash_cow_snapshot`。一行 JSON 保存3／5／10年完整结果，三个周期同事务提交。
- 先暂存校验后的候选结果，再写入数据库；写入成功才发布本地。连接失败或本地发布中断，下次直接续写候选结果，不重新筛选全市场。
- 本地和数据库只接受外部来源；旧聚宽快照不再参与页面新旧选择。数据库同步可用于恢复外部结果，采集所在服务器无需再次同步。
- 失败保留上次完整外部结果，较旧任务不能覆盖较新的外部快照。不保存全市场原始日线到业务表。

筛选算法集中在 `backend/app/services/cash_cow_rules.py`，不再维护重复的聚宽版本。

## 筛选口径

这是当前横截面质量筛选，不是历史回测。窗口为北京时间当前年份之前的所选数量完整财年，例如 2026 年运行时使用 2016–2025 年。尚未披露或无法核验的年度不自动回退，不把缺失值当零。财务数据使用数据源当前提供的最新值，可能包含追溯重述。

每个财年均须满足：

1. 已实施现金分红总额大于零，包含中期、末期及特别分红。
2. 扣非归母净利润大于零。
3. 经营活动现金流量净额不低于该财年的现金分红总额。
4. 股利支付率＝现金分红总额÷归母净利润，介于 30% 和 100%（含边界）。

年度股息率＝该财年税前每股现金分红÷同年每日不复权收盘价的算术平均值。所选3、5或10个年度股息率等权平均，必须**严格大于 4%**，不使用四舍五入后的显示值判断。

结果同时展示当前静态股息率，供比较而不参与筛选：上一财年已实施每股现金分红÷最新价格；提示中列出最近三个完整财年，各年均使用同一最新价格。年表分别列出**年度股息率**和**股利支付率**，避免混用“分红率”。金额单位为人民币元，页面金额换算为亿元。

送转、拆并股、配股等会改变每股口径。适配器不能可靠对齐的情况标为待核验，不将未对齐的分红与价格混算。差异化派息的股东对象、实施日期缺失、方案冲突等也不强行拼接。当前价格过旧时不展示可误认为实时的静态股息率。

## 来源与校验

- 沪深北 A 股池：AShareHub `reference/stocks?list_status=L`，校验唯一代码、上市日期及三个市场；不额外剔除银行或 ST。同时请求 东财 selector 完整分页名单并要求两边代码集合完全一致；独立校验不可用或名单不一致时停止发布。
- 年度已实施分红总额：`RPT_F10_DIVIDEND_HISTOGRAM.DIVIDEND_IMPLE`。**不**用 `PRETAX_BONUS_RMB × TOTAL_SHARES` 猜测现金总额，也不累加分红明细中重复的年度总额。
- 年度财务：东方财富 `RPT_F10_FINANCE_MAINFINADATA` 完整分页结果提供扣非归母利润、归母利润、经营现金流；AShareHub 年度利润表及现金流量表对已有记录逐项交叉核验。AS缺少单只记录时保留东财权威值并记录日志；东财字段缺失按原规则计待核验，不填零。接口错误、分页异常、已取得记录数值冲突仍停止发布。
- 每股实施分红：`RPT_F10_DIVIDEND_MAIN`，按 `REPORT_TIME` 财年归集，核验实施状态、支付日期及股东对象。
- 历史价格：Tushare `daily` 不复权收盘，单候选一次获取最长窗口，共用3/5/10年计算；6000条上限、重复日期、代码错误、越界及无效价格均拒绝。

AShareHub/Tushare请求至少间隔2秒，不自动重试权限或额度错误；东财请求至少间隔0.2秒，超时、连接中断、服务端提前断连及429/5xx仅进行一次有界重试。已完整校验的股票池、财年报表和历史日线落盘缓存，中断重试复用已完成部分，不复用未校验的半页结果。同一任务内三个周期共用一次近10年数据读取，不分别重复采集；完整缓存可在重试时复用，当前日线和实施分红缓存最多5分钟。

部分服务器网络收到行情接口的302跳转（例如 `82.push2.eastmoney.com` → `82.push2delay.eastmoney.com`）。适配器仅接受明确列出的同服务HTTPS主机，API路径及全部查询参数必须保持一致，每次请求最多3次跳转。外部域名、未知子域、HTTP降级、额外端口、嵌入凭据和循环跳转均停止采集；不自动跟随登录或验证页面。跳转后仍执行12 MiB响应上限、分页总数、唯一性及报表日期校验，401/403不重试。

报表每页固定 500 条。接口即使接受更大的分页参数也会静默截断，因此必须核对分页数、总条数、重复主键和完整结果。接口是免费的公开网页数据源，没有稳定性保证；限流、结构变化或不完整返回会报错，不会伪装成“没有合格股票”。

## 存储、权限和运行

多源采集状态和三个周期的最后成功结果统一保存在 `log/market-data/cash-cow/screen.sqlite3`，不进入版本控制；完整结果同时保存到现有 MySQL `cash_cow_snapshot` 表。三个周期全部完成后，先提交数据库，再在本地事务内原子替换快照，共享 `run_id` 和成功更新时间；失败、取消或进程中断保留旧快照和待发布结果。进程间共享锁防止重复任务，取消与最终发布互斥。升级时只读导入旧 `<3|5|10>/screen.sqlite3` 中尚未迁移的周期快照及成功时间历史，不删除或改写旧文件。旧版三个周期已完成同一月份时，同时保留该月份的调度成功标记；仅部分完成时补做一次统一任务。

`GET /api/v1/cash-cow/latest` 和 `/status` 只读，支持 `?years=3|5|10` 选择查看窗口，默认10，其他值拒绝。`POST /refresh`、`/cancel` 复用站点管理员和 CSRF 校验，前端不传年限：分别启动或停止唯一的共享任务。旧调用传入合法 `years` 参数仍兼容，但不会创建单周期任务。状态包含 `periods=[3,5,10]`、`collection_years=10` 和 `run_id`；所有周期共享运行、停止、进度和本轮成功时间。重复点击及采集中切换年限不会额外启动任务。首次安装不会追跑启用前的旧月份，首次取数可点击更新。

自动更新在北京时间每个月最后一个自然日21:00触发，不改为最后交易日。自动更新与手动更新使用同一共享任务，一次采集后同时发布3、5、10年结果，避免重复请求来源。调度状态单独持久化，重启补跑启用后漏掉的最近月份；同一月份成功后不重复执行，失败至多每天自动重试一次，进程中断可恢复。`CASH_COW_WORKER_ENABLED=false` 仅关闭自动调度，管理员手动更新仍可用。

“已判定”包括已证明至少一项不符合的股票和入选股票。“待核验”不等同于“不符合”，也不会混入入选名单。页面可展开查看对应周期的逐年数据。

## 先过滤，再计算

1. 股票池只读取一次。年度数据从最近财年向前批量读取：分红总额非正的股票先排除，不再为它请求该年财报；其余股票一次财报同时核验扣非利润、归母净利润、现金流和支付率。支付率坚持使用财报的归母净利润，不拿分红概览里的另一份利润值代替。
2. 通过最近3年初筛时保留3年候选，再继续核验更早年份；5年、10年同理。旧年份不合格或缺失，只影响包含该年份的窗口，不删除已经保留的短周期候选。缺失资料按原有规则计为待核验，不当作零值或已证明不符合。
3. 只有至少一个窗口通过年度条件的股票，才读取逐笔分红和股本历史。原始记录共用，但送转、拆并股等每股口径按各窗口分别校验；若所有候选窗口均无法核验每股分红，直接列为待核验，不再请求日线。
4. 仍有有效窗口的股票只读取一次完整历史日线，按年度汇总后计算三个窗口各自的平均股息率。短窗口平均股息率不达标，不能据此排除长窗口。只对至少一个窗口最终入选的股票计算一次当前静态展示值，复用已读取的分红、股本和行情。
5. 三个窗口的结果全部就绪后统一发布。来源分页、重试和缓存机制保持不变；一次读取指不因周期重复读取同一份来源资料，不是所有股票或所有年份只发一个HTTP请求。

最近年份优先、先批量年度条件后逐股日线，是在现有接口下减少昂贵请求的安全顺序，不声称所有市场状态下都达到数学最少请求数。旧版已有落盘缓存，因此不能简单宣称请求必然减少三倍；实际节省取决于候选数量、命中缓存和5分钟缓存有效期。离线测试分别校验候选结果等价、单轮来源复用与暖缓存零额外请求。

页面展示所选周期的最近、上一次和上上一次**成功入库**时间（北京时间）。统一更新成功后，三个周期本轮时间一致；旧版独立更新留下的历史时间可能不同。更新时间历史与结果在同一事务提交并持久保存；失败、取消、仅触发任务均不会推进这些时间，历史不足时显示“—”。

旧结果的年度筛选和明细继续保留，但不把旧TTM数值当作静态股息率展示；重新更新全部周期后显示新值。读取失败会显示股票列表、历史行情或分红财报的具体阶段和错误类别，日志不记录凭证和响应正文。


## v3 multi-source collection

The payout ratio is now 30–100% inclusive. Historical v2 results remain visible
with an outdated-rule notice until a complete new collection succeeds. The three
periods still share one collection and publish atomically.

AShareHub supplies the listed directory and annual income/cash-flow batches.
Eastmoney supplies absolute adjusted earnings, income and cash-flow reconciliation values, implemented annual dividend totals,
per-share dividend events and capital-change reasons. Annual income and cash flow
are checked against Eastmoney; missing/conflicting coverage prevents publication.
Tushare `daily` supplies unadjusted prices once per candidate for the longest
required period. Requests at the 6000-row ceiling are rejected rather than silently
truncated. Tokens come only from system settings. No Tencent fallback is enabled.

The directory is cross-checked against a fresh Eastmoney selector complete-page
security set. Both sets must match exactly; unavailable verification fails closed.
AShareHub list responses are not cached before semantic verification. Credentials
are loaded once per task from the unified live integration configuration. Corporate-event and
financial reconciliation can deliberately prevent publication when providers differ.


### Price coverage safeguard

A validated calendar includes every natural date in the widest ten completed
years. Price dates must match its open sessions after listing. SH/SZ/BJ share
the Tushare documented calendar. It is fetched once and cached only after full
validation. A missing session is not automatically a source error or a suspension:
without verified suspension evidence the task stops, retains the previous result,
and logs the affected symbol and dates. The tested token lacks `suspend_d` rights.
Thus legitimate suspended candidates can block publication until their missing
dates are verified; no fixed observation-count threshold or filled prices is used.

### 上市年限预筛

完整股票池对账后，以采集当天为基准先排除未满三周年的证券，再请求分红、财务和行情。2月29日上市的三周年按第三年2月28日计算，不用365×3天近似。股票总数仍保留全池，年龄排除计入排除数。

上市日期要求 AShareHub 与东财 selector 一致；北交所精选层日期、转板或代码变更日期不能直接当作交易所上市日，来源冲突时停止发布并记录代码、两边日期。两源一致不等于已完成官方上市沿革核验，该口径仍需真实全量验证。

三周年不等于具备三个完整上市财年。3/5/10年各自要求每个财年1月1日前已上市；较长窗口不足不会撤销已完成短窗口候选。

沪深交易日历使用 AShareHub `reference/trade-calendar`，分别按 SSE/SZSE 校验完整自然日、唯一日期、交易所及开市标记后缓存，3/5/10年共用同市场完整窗口。未验证北交所日历，不能自动借用沪深日历。日历仅用于覆盖审计，不因缺少开市日期就推断停牌或中止均价计算。日历不可用时记录覆盖未核验，同任务同市场不重复调用失败的日历。

### 实际成交日均价（用户确认）

年度价格按 Tushare 成功返回的实际成交日不复权收盘价算术平均，缺日不填充、不推断停牌，不要求年内首尾交易一定发生于1月/12月。仍拒绝请求失败、错误证券、日期越界/重复、非有限或非正价格、6000行截断上限及所需年度完全无行情。年度结果记录成交行数、最后成交日、价格口径、可审计缺失开市日数量（不可审计为null）和停牌未独立核验标记。该选择不是所有缺日均已证明停牌，也不是无上游漏数风险。
