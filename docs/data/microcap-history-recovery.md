# 微盘温度计历史恢复

独立入口 `collectors/微盘温度计_数据库版.py` 默认 `ARCHIVE_HISTORY=False`，批量版默认 `archive_history=False`；日常只补齐2021起的原序列，不额外扫描全历史。需要独立历史归档时，显式调用 `run(archive_history=True)`，或将批量版对应参数设为 True。关闭归档不会删除已保存的历史。两者仅依赖一起上传的 `mysql_connector.py` 获取数据库配置。

## 保留原序列，另建历史归档

先补齐原有 `micro1000_1d_20210101_v1` 日序列，旧5日序列也不删除。历史另存 `micro1000_1d_YYYYMMDD_v1_archive`；日期是首次通过全部计算校验的基点，首值1000，分位仅依赖这条归档的前序观察。算法、选股、指数续算规则沿用原程序。不把较早归档拼到2021指数，不改网页默认。

归档使用 `get_all_trade_days()` 的完整平台日历，与 `get_trade_days(start_date,end_date)` 交叉核对；逐日使用该日证券、上市250天过滤、ST状态、估值/换手率、成交额、财务可见值及前期成员价格。不是使用今天的股票名单反推过去。结束日遵循16:00前不写当天的规则。

## 边界、未知和失败

`microcap_archive_coverage` 在自选源库记录日历前缀证据，主键 `micro_num,trade_date`：

- `insufficient_calendar_warmup`：日历前4个交易日不能满足5日窗口。
- `insufficient_eligible_securities`：当日上市期过滤后的证券上界已不足样本数，或完整ST过滤后确实不足；前者无需请求更早可能尚未覆盖的ST。
- `unknown_valuation_coverage`：估值接口返回有效空表；只能称覆盖未知，不是零值、不是证明不可计算。
- `verified_anchor_current_visible`：首个完整计算成功的基点。

其他接口异常、权限/配额错误、字段不符、非有限值、成员端点缺价均停止，不越过错误生成“已完成”标记。前缀未知天不生成温度或指数。首次成功不是平台最早完整覆盖承诺；应结合前缀证据解释基点。

已存在归档时固定原基点，逐日补中间缺口并按原事务检查点修复后续派生分位/指数；不静默前移基点。若来源后续补出基点之前的未知数据，需要显式设计并审核另一条新基点序列，而不是改写旧曲线。逐日提交，中断后已完成日保留，重新运行续跑。重复完整运行不重新采集已保存归档日。

归档续采会先关闭查询基点的会话，再进入每日采集，支持 `pool_size=1, max_overflow=0`。旧版在查询会话关闭前调用续采，会在已有归档的第二次运行或中断后重跑时耗尽连接池。遇到此类 QueuePool 超时，请更新独立及批量脚本的实际运行副本后重跑；不需要清库、增加连接数或调整超时，之前提交的日期继续保留。

## 历史修订与读取

这是 **当前可见历史重建（current_visible_history）**，不是保存当年未修订输入的原始PIT档案。财务仍传真实历史 `date`，不以今天的财报当历史输入；平台后续修订的影响须由此标签披露。

- `GET /api/v1/microcap-temperature/series` 列出数据库中支持的原/归档序列。
- `GET /api/v1/microcap-temperature/history?days=0&series_id=micro1000_1d_YYYYMMDD_v1_archive` 显式读单条归档；不传参数仍是2021默认/旧采样回退。
- 返回包含 `series_id`，归档附 `reconstruction=current_visible_history`；不同基点永不混合。
- 端点沿用登录权限，非法格式/日期返回422。已有同步表的复合主键与结果日志涵盖所有series，无需替换默认缓存或新增权限。覆盖证据表为源库审计，不进入网页曲线。

## 官方接口依据与验证范围

已核对[聚宽官方API文档](https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf) 的历史 `get_fundamentals(query_object,date=...)` 契约及[官方接口源码](https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/api.py) 的 `get_all_trade_days()` / `get_trade_days()` 返回契约。运行仍仅使用平台内置jqdata，不安装SDK、不登录外部服务。文档不据此保证某账号的历史权限/字段实际覆盖。离线测试验证算法和事务；尚未用真实聚宽账号或生产MySQL回填，不能宣称已恢复线上数据。
