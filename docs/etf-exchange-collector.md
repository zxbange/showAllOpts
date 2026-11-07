# ETF 交易所采集器

`collectors/etf_exchange_collector.py` 是独立CLI入口，不依赖聚宽；核心实现打包在 `backend/app/services/etf_exchange.py`，原生部署和Docker使用同一实现。成员名单统一读取 `backend/app/core/etf_catalog.py`；不修改旧聚宽脚本。

## 运行

使用 backend 依赖环境（httpx、SQLAlchemy、PyMySQL、pydantic-settings），配置遵循 backend Settings：环境变量、默认 `backend/.env` 或 `APP_ENV_FILE` 指定配置。dry-run 不加载数据库配置、不连接数据库。

```sh
python collectors/etf_exchange_collector.py --start 2026-11-06 --end 2026-11-06 --probe
python collectors/etf_exchange_collector.py --lookback 5 --write
python collectors/etf_exchange_collector.py --start 2026-11-02 --end 2026-11-06 --shares-only --write
python collectors/etf_exchange_collector.py --start 2026-11-06 --end 2026-11-06 --prices-only --write
```

默认 dry-run；默认日期为官方日历核验过的最近5个已收市交易日（盘中不包含当天）。`--write` 才创建所需表并写入；按基金代码和交易日主键幂等更新，允许交易所同日修订。全部网络请求及响应校验完成才启动数据写入事务；完整已发生交易日及真实前一交易日同步写入独立 `etf_trading_day` 表，和份额、价格共用事务，不改共享 holdings 日历。份额和价格独立补采，不因份额已入库而跳过价格。

退出码：0 请求范围完整；1 请求/格式/日历/写库错误；2 存在缺失日期（有效观测仍可按 `--write` 写入，缺失绝不填补）。脚本输出缺失总数及前20个代码/日期，不打印数据库密码或连接串。普通服务器无需 SOCKS 依赖；若运行环境使用 SOCKS_PROXY/ALL_PROXY，则该环境需安装 httpx 的 socks 可选依赖。

## 来源、单位和边界

- 上交所份额：`query.sse.com.cn/commonQuery.do`，`sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L`。按 `STAT_DATE` 查询，开启 `isPagination=true`，核对 `pageHelp.pageNo/pageCount/total` 并完整分页；不能用未开启分页时总数0判断无数据。
- 深交所份额：`www.szse.cn/api/report/ShowReport/data`，`CATALOGID=scsj_fund_jjgm`、`TABKEY=tab1`、`jjlb=ETF`；每只基金90日窗口，按 `PAGENO` 分页，核对总页数和总记录数。
- 两所份额原始单位均为万份；通过 Decimal 精确乘10000转换为整数份，拒绝负数、非整数和BIGINT溢出，不以成交量或资产净值替代。
- 上交所价格：`yunhq.sse.com.cn:32042/v1/sh1/dayk/{code}`，`select=date,close&begin=0&end=-1`，取同日日K close，单位元/份。
- 深交所价格：`getHistoryData?marketId=1&code={code}&cycleType=32` 实测只返回约201条，额外历史分页参数无效，且历史复权规则尚未证实。为避免污染市值计算，只保存与 `getTimeData` 返回的**同日期、15:00以后**行情 `data.now` 完全一致的日K收盘价，来源标记 `SZSE.CLOSE_VERIFIED`。`data.close` 为昨收，绝不能当当日收盘。其余历史不写可信价格表，报告缺失。深市历史价格回填仍待验证数据源。
- 深交所明确说明 T日晚更新规模仅供参考，以T+1日早间更新为准，因此需每日回查并覆盖修订；CLI本身不设置自动任务，后台应用按[自动采集计划](etf-collection.md)调用同一实现。
- 交易日与真实前一交易日使用上交所上证指数000001完整已发生日K日期（`select=date&begin=0&end=-1`）。校验代码、首日1990-12-19、total/实际行数/begin/end、严格递增无重复，以及最新官方指数快照日期一致。不是用ETF自身有缺口的历史推断交易日。
- 自动读取深交所官方整月开休市日历 `api/report/exchange/onepersistenthour/monthList?month=YYYY-MM` 的当前月和前月；要求 `nowdate` 为北京时间今天、月内每个自然日恰好一行、`jybz` 为明确0/1，并与上交所已发生日期交叉验证。按官方标记判定最新已收市日，发现指数陈旧、日历截断/缺日/未发布立即失败，不能把缺源等于休市。跨年无需代码中追加固定年份，但官方未发布月份不能猜测。
- 只接受已发生完整日历覆盖范围；不接受未来日期或超过已确认收市末日的显式范围。首个历史交易日没有可核实前日，采集该日会报错。不会用weekday猜节假日，亦不会把数据库中跨缺口上一条记录冒充前一交易日。
- 对代码、日期、重复行、数据类型、分页完整性与价格精度进行校验；缺失不填零、不前向填充、不生成虚假市值。

## 验证

离线测试位于 `backend/tests/test_etf_exchange_collector.py`，覆盖分页、截断、分页期间源数据变化、重试、单位、交易日历、错码/错日/重复数据、幂等更新、同日修订、默认只读、独立补价及深市收盘交叉验证。

2026-11-07 实际只读接口验证：上交所2026-11-06份额923行（分页总数923）；深交所159919在2026-11-02至11-06五日数据5行，11-06为1,405,461.66万份。深交所159919行情时间2026-11-06 15:30，now=4.304，close=4.373（昨收）。实测不代表交易所未来永不变更接口或历史数据完整。

同日完成全目录集成 `--probe --start 2026-11-06 --end 2026-11-06`：18只/7组份额18行、可信价格18行，两类缺失均0，退出码0，未连接生产数据库。两所日K均拒绝保存本地北京时间15点前的当日日K及未来日期，防止将盘中价格称为收盘价。

深交所真实分页补测：159919在2026-09-01至11-06返回43行，已跨3页完整取回；11-02至11-06五日只读采集为5行份额、1行已核实价格、4行历史价格缺失，符合安全限制。拒绝未来截止日期；重复回查若业务字段完全一致则保留原更新时间，仅真正修订更新 `updated_at`。

## 动态交易日历与内部调用

`ExchangeClient.calendar()` 返回 `ExchangeCalendar`，包含 `days`（已收市date元组）、`official_days`（当前月/前月date→开休市bool）。

- `calendar.default_range(5)`：校验新鲜度并返回默认起止日。
- `calendar.range(start, end)`：覆盖范围内实际交易日。
- `calendar.previous(day)`：真实前一交易日；不跨缺口猜测。
- `collect(client, start, end, funds=None, include_prices=True, include_shares=True, calendar=calendar)`：返回不变的 `(shares, prices, missing)`。
- `write_records(engine, shares, prices, calendar=calendar)`：同事务写份额、价格和已发生交易日；完全相同记录保留 `updated_at`。日历批量读取已有行，避免每次回查产生数千次数据库往返。

本文只描述采集器与只读验证。后台调度已接入应用生命周期，见[自动采集说明](etf-collection.md)；开发验证未配置生产写入账号或进行生产写库；发布不代替独立写入账号配置。

动态日历实测（2026-11-07）：上证指数完整8758个已发生交易日，范围1990-12-19至2026-11-06，`total=end=8758, begin=0`；默认5日范围为11-02至11-06。官方11月日历30天、10月31天均完整且 `nowdate=2026-11-07`，2027-01返回空数组，按“尚未发布”失败。跨年只读采集510300于2025-12-31和2026-01-05，获得2行份额和2行价格，缺失0，2026-01-05前日正确为2025-12-31。前者份额88,829,587,700，后者89,058,187,700，来自真实交易所返回，未生产写库。

盘中兼容两种官方日K形态：若指数快照已是今天，日K可以已含今天盘中记录（过滤掉），也可以仅到官方月历确认的前一已收市日。后一种仅在北京时间15:00前且官方明确今天开市时接受，必须精确匹配前一实际交易日。更早的陈旧尾日、收市后仍缺今天日K，继续报错等待重试，不能静默跳过。
