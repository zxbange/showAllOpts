# 行业动量与微盘新高新低：聚宽完整历史恢复

## 范围与真实性

这两个入口现在能执行历史采集并写入配置的数据库，不再只给出恢复清单：

- `collectors/行业动量计算_数据库版.py` → `industry_momentum`。
- `collectors/微盘新高新低数量统计_数据库版.py` → `microcap_breadth`。

**这是用聚宽当前可见、可能已修订的历史数据重建，不是原始时点快照（PIT），也不是已经完成生产数据库恢复的声明。** 历史成员、复权价格和市值都按目标日期请求，但源供应商后来的修订仍可能影响结果。没有原始源数据时不会伪造 PIT 或实盘校验。

两者直接运行和调用 `run()` 均默认普通增量，与批量版一致：行业动量使用最近180个交易日窗口，微盘新高新低首次使用近180个自然日，之后从最新入库日续采。完整恢复须显式调用 `run_history()` 或 `run(history_mode=True, start_date=None)`。完整历史不使用2025年固定起点、不受180日窗口限制，也不依赖数据库 MAX 日期判断全部完成。

## 配置与执行

使用者自行将所需脚本和 `collectors/mysql_connector.py` 放到聚宽研究目录。只在自己的私有 `mysql_connector.py` 配置数据库地址、名称、用户和密码；脚本不依赖项目其他策略文件或 `rules/`、`tools/` 目录。不要把凭据提交到仓库或粘贴到日志中。

先选择明确的目标数据库、备份已有数据并确认账号有建表权限，再执行所需入口：

```python
%run -i 行业动量计算_数据库版.py
```

或者：

```python
%run -i 微盘新高新低数量统计_数据库版.py
```

上述命令执行日常增量。显式历史模式未指定 `start_date` 时，才从 `get_all_trade_days()` 提供的最早交易日开始，截止北京时间已收盘日期。16:00 前不写当天；显式请求未来日期会报错。历史模式的 `get_trade_days()` 范围结果须与完整交易日历完全一致，不能把返回截断误认成最早历史边界。

如需控制单次工作量，可在 Notebook 中先加载定义而不运行文件底部的默认调用，再分批执行：

```python
import runpy
industry = runpy.run_path(
    '行业动量计算_数据库版.py', init_globals=dict(globals()), run_name='industry_history_definitions')
industry['run_history'](start_date=None, end_date=None, industry_type='sw_l2', max_days=250)
```

```python
import runpy
breadth = runpy.run_path(
    '微盘新高新低数量统计_数据库版.py', init_globals=dict(globals()), run_name='breadth_history_definitions')
breadth['run_history'](start_date=None, end_date=None, micro_num=400, max_days=250)
```

- 相同参数重跑会检查整个请求日历，只计算未认证的日期，补齐 MAX 之前的旧洞。`max_days` 限制本次新检查的日期数，不把已认证或已记录的不可用前缀重复算进额度。
- 可以指定 `start_date`/`end_date` 恢复一个区间，之后再次使用 `start_date=None` 检查更早缺口。
- 配额、行情、ST、行业归属、市值或数据库错误会立即停止。之前独立事务已提交的结果保留；失败日期不获得完成标记。修复原因后用同样参数重跑。
- `retry_unavailable=True` 重新检查已记录的不可用历史前缀，适用于供应商补齐了更早数据的情况。不要在每次普通续跑中开启，否则会重新消耗前缀检查额度。
- 不允许 `skip_existing=False` 历史覆盖。已存值与当前重建结果冲突时停止并保留原值，不能混合两个修订版本后宣称完整。

## 行业动量的验证

计算口径保留原有定义：上市超过 150 个自然日且满足存续条件的股票，比较连续 21 个交易日的起止收盘价，严格涨幅大于 20% 计入前排；每个行业得分为前排数量平方 / 行业总成员数，保留两位小数后按得分、行业代码排序。

历史模式额外检查：

1. 日期内证券列表、行业代码和名称，以及证券上市/退市日期。
2. 行业成分不为空对象、不重复、不越过当日证券范围；将成分接口与逐股 `get_industry()` 的同日归属交叉核对。只有交叉核对后确认确实无成员的行业，才允许其真实零分。
3. 每批最多 100 只股票，全部 21 个交易日行情必须完整，日期/股票不重复、不越界，价格均为正有效值。缺行或缺价不作为零涨幅。
4. 全部行业的排名、分母、分子和得分一致后，在同一个事务内补入缺失行业并登记完成凭据。已存完整凭据还会核对数据摘要；删行后的旧洞可恢复。

少于 21 个真实交易日、没有满足 150 日上市期限的股票，只能作为已验证的初始预热期记录。行业分类为空也不会被随意略过：只有当日接口明确返回空表、日期早于源行业元数据提供的最早 `start_date` 时，才记录分类起始前缀。元数据不存在、接口异常、返回 `None` 或起点之后为空都会停止；不会猜一个可用起点。

日常模式的 180 个交易日仅用于确定采集窗口，**不会自动删除任何已有历史数据**，包括旧脚本写入、没有新凭据的数据。底层旧 `cleanup_old_data()` 仍是显式维护接口，不是运行流程的一部分；恢复任务不要调用它。

## 微盘新高新低的验证

每天重新按当日股票列表、上市超过 240 个自然日、剔除指定板块和 ST、当日市值排序选取默认 400 只股票，名单保存到 `member_codes`。以当日收盘价与此前 5/10/20/30 个交易日比较，等于前期高/低点也计入，四个周期共用严格完整的 31 日行情。

- 历史证券列表须提供有效上市和退市日期。ST 必须完整覆盖本批全部股票且恰好对应当日。
- 市值必须完整覆盖去 ST 后候选池，不能缺成员、重复、为零或非有限值。行情须完整覆盖每个入选成员及全部 31 个交易日。
- 少于 31 个已验证交易日，或完整验证后的候选池仍不足 400，只记录“历史前缀不可计算”，不写一个全零结果。
- 一旦本序列已有更早成功结果，后续日期的股票池不足是异常，不得继续按前缀跳过；源接口异常也永远不能转换为“股票数量不足”。

## 检查结果、续跑与同步

新建的辅助表：

- `industry_momentum_history_status`，按日期、行业分类记录。
- `microcap_breadth_history_status`，按日期、微盘序列记录。

字段包括 `status`、`reason`、`source_basis`、摘要和更新时间。`complete` 表示该次重建已验证并与结果原子提交；`unavailable_prefix` **不是完成**，代表有证据的早期计算限制。`source_basis` 是 `joinquant_visible_revised_not_original_pit_v1`。执行结束会分别输出新增数量和不可计算前缀数量；到达 `max_days` 不是整个区间已经完成。

可在目标数据库分别查看：

```sql
SELECT status, reason, COUNT(*) AS days,
       MIN(trade_date) AS first_day, MAX(trade_date) AS last_day
FROM industry_momentum_history_status
WHERE industry_type = 'sw_l2'
GROUP BY status, reason;

SELECT status, reason, COUNT(*) AS days,
       MIN(trade_date) AS first_day, MAX(trade_date) AS last_day
FROM microcap_breadth_history_status
WHERE series_id = 'micro400_hl_v1'
GROUP BY status, reason;
```

公开结果变更与 `result_sync_head` / `result_sync_change` 日志在同一事务提交，模块名分别是 `industry-momentum` 和 `microcap-breadth`。辅助凭据不进入公开同步日志。历史缺口回填不能只用 MIN/MAX 日期判断缓存未变化；后端须使用这两个模块的结果日志同步版本，旧无日志数据走完整基线校验。

离线测试覆盖：最早日期预热、前缀分批续跑、日期范围被截断、行情或成员缺失、API/ST 异常、已有成功日期之后股票池不足、历史中间洞和行业部分缺失、原值冲突保护、每日普通写入日志、结果/凭据/日志同事务回滚、Python 3.6 语法和共享日志一致性。测试使用可控样例与 SQLite，没有登录聚宽或访问真实 MySQL，也不证明真实源最早可用日期或生产库已经补齐。
