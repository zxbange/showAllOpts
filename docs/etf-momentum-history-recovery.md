# ETF 动量：历史恢复的边界与独立档案

## 原混合 ETF/LOF 图表

原实时算法、规模门槛、分类和26期加权回归均保留。最近180个交易日仍是默认采集窗口，但运行不再自动删除更老结果；不在最新候选池的旧基金也保留。窗口内逐日核对缺口，不只追数据库最大日期。

**不能声称已恢复原混合候选池全历史。** `FUND_MAIN_INFO` 的国内股票/混合 LOF 类型没有经核实的按历史日期版本查询。仅用生效/终止日筛选今天的主档属性，无法证明过去分类；本版本不把它用于混合历史排名。历史模式默认 `include_lof=True` 会在连接数据库前明确拒绝。

## 可执行的较窄恢复

显式 `history_mode=True, include_lof=False` 启用独立 ETF 历史档案，写入：

- `etf_momentum_heat_etf_history`，主键交易日、基金代码。
- `etf_momentum_pool_etf_history`，主键交易日，保存当日完整入选快照。

这两张表不接入现有混合ETF/LOF页面、结果同步日志或指标，绝不填入原 `etf_momentum_heat` / `etf_momentum_pool`。不是原混合功能已恢复或已上图。独立历史不执行删除。

独立和批量脚本应保持同一 `_etf_heat_strategy` 实现。只依赖已有 `mysql_connector.py` 配置目标库，不新增数据库账号字段、登录外部SDK或运行其他策略。

在两入口顶部 `STRATEGY_CONFIG['ETF动量热度']` 中设置：

```python
'history_mode': True,
'include_lof': False,
'start_date': '2020-01-01',
'from_platform_earliest': False,
'end_date': None,
```

或者设 `start_date=None, from_platform_earliest=True`，从 `get_all_trade_days()` 返回的最早交易日逐日期尝试，绝非承诺数据源从该日起资料完整。截止日默认按北京时间，16点前不取当天。首个ETF上市前的空列表可以保存空候选日。

批量调用示例（需最终统一合并后使用）：

```python
run_all(strategy_names=['ETF动量热度'], options={
    'ETF动量热度': {'history_mode': True, 'include_lof': False,
                   'from_platform_earliest': True}
})
```

## 每日校验与断点

1. 当日 `get_all_securities(types=['etf'], date=day)` 列表，不用今天存续基金倒推历史。
2. `FUND_INVEST_TARGET` 同时满足公布日、生效日不晚于该日，终止日未过期；每只基金必须有唯一非空跟踪标的。缺失、冲突或无法分类会使整日失败。列表的 `display_name` 不保证历史版本，因此历史显示和分类使用已公布跟踪标的名，不宣称真实历史基金简称。
3. 当时同一方向所有候选使用同日份额×单位净值、原规模门槛和最大规模去重。缺少任何候选规模、共同有效日滞后超过3个交易日、分类冲突，均整日失败，不偷偷删除竞争者。
4. 对入选基金，原26期后复权价格加权动量算法不变；原始收盘价另存。缺口/停牌标识异常失败。上市不足26期或合法停牌按原算法保存缺失得分，不造零。`COMPLETE`表示当日请求事务完成，不等于每条得分可计算。
5. 每日结果与候选快照同事务提交。失败日不存新检查点；其他成功日保留。重跑每次遍历整个范围，包括已有最新日期以前的中间缺口。已有有效得分不覆盖，`repair_missing=True`仅把空得分修复为有效值。已存当日候选快照与重新计算不同则拒绝覆盖。
6. 任何失败日期使本次整体报错，异常 `.report` 包含全部日期状态、写入条数和脱敏错误。批量入口仍报告FAILED，控制台逐日说明原因。配额/权限/缺失元数据都不是零数据。

## 数据依据与未验证项

- 聚宽官方API文档：https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf 。支持 `get_all_securities(types, date)` 的日期列表和ETF/LOF类型，及行情接口基本结构。
- 聚宽官方SDK源码：https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/api.py 。用于交叉检查全量交易日历和行情接口参数；采集仍在研究平台使用jqdata，不运行此SDK。
- 基金表字段和份额单位沿用项目现有采集器 `FUND_INVEST_TARGET`、`FUND_SHARE_DAILY`、`get_extras('unit_net_value')`。本次没有可用聚宽账号验证完整年份、字段字典及授权，运行时严格校验，不承诺该账号能返回全历史。`FUND_MAIN_INFO`未获得历史分类版本证据，故原混合历史跳过。

离线测试涵盖日期候选变化、规模赢家变化、禁止当前名称回填、独立表隔离、断点续跑、中间缺口、缺竞争者份额、缺历史标的、旧快照变动拒绝覆盖、平台最早日期与非法参数。未运行真实聚宽采集或生产MySQL。
