> 当前独立脚本与批量脚本默认 `skip_existing=True`，已有日期不覆盖、不回查刷新。需要人工修订或回补时才显式使用 `skip_existing=False`；以下回查说明针对该显式模式。

# ETF 份额仪表盘

> 默认启用本地缓存：采集入库后，等待每天北京时间 07:00 自动同步，或在网页“数据同步”/服务器命令中手动同步。页面刷新读取当前快照，详见[本地缓存说明](local-cache.md)。


聚宽研究环境采集 → MySQL `etf_share_daily` → Python 只读 API → React 仪表盘。入口为首页的“ETF份额仪表盘”或 `/etf-shares`。

## 跟踪范围

| 分组 | 基金代码（聚宽） | 展示简称 |
| --- | --- | --- |
| 沪深300 | 510300.XSHG | 沪深300ETF华泰柏瑞 |
| 沪深300 | 510310.XSHG | 沪深300ETF易方达 |
| 沪深300 | 510330.XSHG | 沪深300ETF华夏 |
| 沪深300 | 159919.XSHE | 沪深300ETF嘉实 |
| 沪深300 | 515330.XSHG | 沪深300ETF天弘 |
| 中证500 | 510500.XSHG | 中证500ETF南方 |
| 中证500 | 510510.XSHG | 中证500ETF广发 |
| 中证500 | 510580.XSHG | 中证500ETF易方达 |
| 中证500 | 512500.XSHG | 中证500ETF华夏 |
| 中证500 | 159922.XSHE | 中证500ETF嘉实 |

沪深300组按 2026-10-16 的规模排名选定华泰柏瑞、易方达、华夏、嘉实、天弘，并按此顺序展示。这是固定跟踪清单，不会在运行时重新排名或自动替换基金，不代表指数下全部 ETF。展示名称是易读简称；基金代码是数据关联依据。扩展基金时同时修改采集器 `ETF_GROUPS` 和 `backend/app/core/etf_catalog.py`，测试会检查两者一致。

升级跟踪清单后，在聚宽正常运行新版脚本即可：嘉实、天弘若尚无记录，会分别补采最近 300 个交易日；已有基金仍按各自最新日期增量更新。中金、工银停止采集并从本组展示中移除，已入库历史保留。更新网页项目后需重启后端以加载新清单。

## 首次运行与配置

脚本为 [ETF份额_数据库版.py](../collectors/ETF份额_数据库版.py)，独立运行于聚宽研究环境，不依赖网页项目。公开脚本只有脱敏连接模板；请在聚宽私有副本的 `MysqlETF` 类填写 host、port、dbname、dbuser、dbpassword。连接方式沿用现有采集器的 SQLAlchemy + mysqlclient（mysql+mysqldb），不要把真实凭据提交 GitHub。网页继续使用现有 `config/app.config`（Linux）或 `backend/.env`（Windows），不新增数据库配置。

- **首次采集最近 300 个交易日**：`START_DATE = None`、`INITIAL_TRADE_DAYS = 300`，通过聚宽交易日历 `get_trade_days(end_date=end, count=300)` 确定起点。周末和休市日不计数；运行终点为非交易日时，截止到此前最近交易日。数据源覆盖不足的基金仅保存实际返回的数据，不补零。
- `END_DATE = None` 表示每次运行取北京时间当天；这不代表当天份额已经发布。
- 后续从每只 ETF 的最新入库日期续传，含该日回查最近 5 个交易日，覆盖近期源数据修订。
- 首次采集后的历史持续积累，**不自动删除早于 300 个交易日的记录**。长时间未运行时仍从已入库水位追补，不跳过断档。

如果已经按旧版“近一年”入库，在加载脚本定义后执行一次 `run(incremental=False)`（使用下文独立命名空间时为 `collector['run'](incremental=False)`），补采当前最近 300 个交易日；正常增量运行只续传尾部，不会自动回补更早的起点。网页默认仍显示近一年，选择“全部”可查看完整已入库历史。

直接粘贴脚本到 Notebook 运行，或者上传文件后运行：

```python
%run -i ETF份额_数据库版.py
```

为了先核对账号的数据权限与字段口径，可以在新的 Notebook 单元格执行下列代码，仅加载定义并查询最近样本，不连接数据库：

```python
from jqdata import *
from pathlib import Path
collector = dict(globals())
collector['__name__'] = 'etf_share_probe'
source = Path('ETF份额_数据库版.py').read_text(encoding='utf-8')
exec(compile(source, 'ETF份额_数据库版.py', 'exec'), collector)
collector['probe']('510300.XSHG')
collector['probe']('159922.XSHE')
# 确认接口返回、日期与份额单位正确后执行入库：
etf_df = collector['run']()
```

使用 `finance.run_query(query(finance.FUND_SHARE_DAILY)...)` 查询 code、date、shares；shares 按整数“份”保存，展示时除以 1 亿。字段缺失或接口不可用会明确报错，不会自动改用成交量、净资产或行情数据。参考入口为 [聚宽数据文档](https://www.joinquant.com/help/api/help?name=JQData)。本地没有聚宽运行环境，真实账号权限、数据覆盖起点、发布时间及沪深两市日期口径仍需通过上述 probe 与官方基金份额核对；不人为平移源数据日期。

## 增量、缺口与修订

一次分组查询取得各基金最早和最新入库日期，不读取历史明细。最早日期用于区分上市前/覆盖前无数据和已覆盖区间丢数据；最新日期用于续传。

每只基金按最多 120 个交易日分块查询。每个窗口验证代码、日期、重复记录、整数份额和交易日连续性；中间缺口会停止该基金，窗口不写入。首条有效数据前允许为空，未发布的尾部日期不补零、不推进进度。接口明确返回 0 才保存为 0。

每个有效窗口在同一事务内替换该基金实际返回的日期。失败回滚该窗口；先前成功窗口保留，其他基金继续处理，最后汇总失败基金。再次运行可从各自水位恢复。请勿并发运行同一采集脚本。

超过 5 个交易日的历史修订不会被自动发现。需要时显式重查区间（下面例子在加载完定义后执行，不删除区间以外的数据）：

```python
collector['run'](start_date='2026-01-01', end_date='2026-01-31',
                 groups=['hs300'], incremental=False)
```

如源数据长期有缺口，应核对聚宽原始记录再处理，程序不会编造数据或跳过缺口。若全部基金均无数据，程序输出检查权限、覆盖与发布时间的提示，网页保持空状态。

## 表结构与权限

表名 `etf_share_daily`，复合主键 **(fund_code, trade_date)**，InnoDB / utf8mb4。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| fund_code | VARCHAR(16) | 聚宽基金代码 |
| trade_date | DATE | 源数据的份额日期 |
| fund_name | VARCHAR(128) | 采集时的展示简称 |
| shares | BIGINT | 总份额，单位份，非负整数 |
| previous_trade_date | DATE | 聚宽交易日历中的前一交易日 |
| source | VARCHAR(48) | jqdata.FUND_SHARE_DAILY |
| updated_at | DATETIME | 最后写入时间，北京时间 |

采集账号需 SELECT、INSERT、DELETE 以及首次 CREATE 权限。网页账号仅需该表 SELECT 权限；网页不创建表。主键覆盖每只基金的最新日期和日期范围查询，无需重复建立同列索引。

页面非空请求执行两条 SELECT：第一条取得本组各基金最新记录及其前一交易日份额，第二条只取所选区间内的代码、日期和份额。窗口筛选在数据库执行，不把全库历史拉到 Python 再裁剪，减少公网传输。实际 MySQL 查询计划仍以部署库的 EXPLAIN 为准。

## 展示口径

- 五张卡片：最新总份额、日份额增减、日变化率以及各自的数据日期。红色代表份额增加，绿色代表减少。缺少前一交易日记录时变动显示“—”，不跨缺失日计算单日变化；前一日份额为 0 时不计算百分比。
- 各 ETF 可能不同步。“待更新”仅表示落后于本组最新记录日期，不代表与今天比较后已及时更新。
- 总份额曲线：单位亿份，不同基金使用固定颜色。点击卡片聚焦单只，再次点击或“显示全部 ETF”恢复多线。
- 份额变化率曲线：以所选区间内**有数据基金共同的首个正份额日期**为起点，计算 `(当日份额 / 基准日份额 - 1) × 100%`。基准日前不显示变化率；无共同基准时禁用。改变窗口会改变基准日，聚焦单只不会改变。
- 默认近一年，可切换 30 / 90 / 180 / 365 个自然日和全部历史，以本组最新入库日为终点。横轴为组内有记录日期的并集；某基金缺失的点留空、不连线、不补零。所有基金共同缺失的日期不额外生成。
- API 中原始 BIGINT 份额和差额使用十进制字符串，避免 JSON/JavaScript 大整数截断；网页展示单位换算与两位小数会正常舍入。

份额变化不等于 ETF 价格涨跌、收益率或资金净流入金额。份额拆分/合并等基金事件可能引起跳变，当前展示原始份额，不对这些事件进行复权。页面不将不同 ETF 的份额相加作为资产规模。

接口：`GET /api/v1/etf-shares/history?group=hs300&days=365`，分组支持 hs300、zz500；days=0 表示全部。可传历史 end_date，不允许未来日期。空表返回 200 空结果，未建表/权限/连接异常返回 503，非法参数返回 422。

## 已有部署升级

1. 在聚宽运行采集脚本，创建并填充新表。给网页只读账号补充 `etf_share_daily` 的 SELECT 权限。
2. Linux 服务器执行以下命令，保留现有 `config/app.config`、Nginx 端口和 systemd 配置：

```bash
cd /home/ubuntu/showAllOpts
git pull --ff-only
bash scripts/build.sh
sudo rsync -rlt --chmod=D755,F644 frontend/dist/ /var/www/showallopts/
sudo systemctl restart showallopts-api
sudo systemctl status showallopts-api --no-pager
curl -fsS 'http://127.0.0.1:8000/api/v1/etf-shares/history?group=hs300&days=30'
```

Windows 更新代码并关闭旧启动器后重新运行 `start.cmd`。本功能无新增运行依赖。首次部署参见 [Linux 部署说明](linux-deployment.md)。新表创建前页面会显示数据库错误，完成采集后刷新即可。

## 验证边界

离线 SQLite 测试覆盖首次 300 个交易日（含周末和休市边界）、各基金独立增量、窗口分块、重复执行、缺口停止、无数据不补零、事务回滚、部分失败继续、单位和大整数、前一交易日对齐、共同基准和只读查询数量；浏览器测试覆盖桌面/手机入口、分组、卡片聚焦、指标/范围切换、错误重试和空状态。测试数据不写入生产库。本地测试不能替代聚宽真实接口及生产 MySQL 的首轮验证。
