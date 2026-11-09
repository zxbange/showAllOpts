# 采集与研究脚本

采集入口、公共规则及其本地维护工具统一放在本目录。项目环境初始化、部署和数据库运维工具仍放在 `scripts/`。

```text
collectors/
├── README.md
├── mysql_connector.py      全部采集脚本共用的 MySQL 配置
├── *.py                    采集、补采和研究入口
├── rules/                  公共代码维护源，不在聚宽运行时导入
│   ├── earnings_followup.py
│   ├── earnings_version.py
│   ├── momentum_threshold.py
│   └── result_journal.py
└── tools/                  仅在本地使用的代码同步工具
    ├── sync_earnings_version.py
    ├── sync_momentum_threshold.py
    └── sync_result_journal.py
```

## 日常入口

| 用途 | 脚本 | 运行环境 |
| --- | --- | --- |
| 10 个策略批量采集、计算并写入数据库 | [策略批量采集_数据库版.py](策略批量采集_数据库版.py) | 聚宽研究环境 |
| ETF 份额与价格日常采集 | [etf_exchange_collector.py](etf_exchange_collector.py) | 项目 Python 环境；[运行说明](../docs/etf-exchange-collector.md) |
| ETF 历史缺口补采 | [ETF历史补采_聚宽研究版.py](ETF历史补采_聚宽研究版.py) | 聚宽研究环境；填写配置后整段运行即入库，缺表自动创建 |
| ETF 分类、规模筛选及跟踪标的去重研究 | [ETF分类去重_研究版.py](ETF分类去重_研究版.py) | 聚宽研究环境 |

两融担保比例由项目后端使用东方财富接口采集，不属于聚宽批量任务。旧聚宽 ETF 份额日常采集入口已删除，历史补采入口继续保留。

## 独立策略入口

下列脚本用于单独采集、排查和与批量实现核对，继续保留：

| 策略 | 脚本 |
| --- | --- |
| 行业动量 | [行业动量计算_数据库版.py](行业动量计算_数据库版.py) |
| 抱团拥挤度 | [抱团拥挤度_数据库版.py](抱团拥挤度_数据库版.py) |
| 市场情绪跟踪 | [市场情绪跟踪_数据库版.py](市场情绪跟踪_数据库版.py) |
| 微盘温度计 | [微盘温度计_数据库版.py](微盘温度计_数据库版.py) |
| 微盘新高新低 | [微盘新高新低数量统计_数据库版.py](微盘新高新低数量统计_数据库版.py) |
| 净利润断层 | [净利润断层_数据库版.py](净利润断层_数据库版.py) |
| 高股息选股 | [高股息选股_数据库版.py](高股息选股_数据库版.py) |
| 高增长 | [高增长_数据库版.py](高增长_数据库版.py) |
| ETF/LOF 动量热度 | [ETF动量热度_数据库版.py](ETF动量热度_数据库版.py) |
| 个股机构持仓 | [个股机构持仓_数据库版.py](个股机构持仓_数据库版.py) |

聚宽脚本各自包含完整策略，但统一导入 `mysql_connector.py` 获取数据库配置。先将配置文件上传到研究目录，再运行所需策略。采集、计算后的结果写入数据库，网页再同步本地缓存。批量参数见[批量采集说明](../docs/collector-batch.md)。

## 独立策略与公共代码维护

聚宽入口之间不互相调用。批量版内置十个策略，独立版各自包含策略及辅助函数；运行时统一依赖同目录中的 `mysql_connector.py`，无需上传 `rules/`、`tools/` 或其他策略文件。

`etf_exchange_collector.py` 是项目环境中的交易所采集入口，调用后端服务，不属于聚宽脚本；该命令行入口也使用 `mysql_connector.py` 的连接配置。网页自动采集仍使用网页已保存的目标配置，不受本次脚本配置迁移影响。

| 公共代码 | 职责 | 内嵌目标 |
| --- | --- | --- |
| `rules/earnings_followup.py` | 断层后续涨幅及历史清理 | 净利润断层独立版、批量版 |
| `rules/earnings_version.py` | 财季版本及同步标记 | 净利润断层独立版、批量版 |
| `rules/momentum_threshold.py` | 行业动量严格大于20%的精确判断 | 行业动量独立版、批量版 |
| `rules/result_journal.py` | 采集结果变更记录 | 市场情绪跟踪、微盘温度计、ETF动量热度独立版及批量版 |

公共代码在本地维护阶段预先嵌入采集脚本，运行时不会加载源文件。修改公共规则后，在项目根目录执行：

```bash
python collectors/tools/sync_earnings_version.py
python collectors/tools/sync_result_journal.py
python collectors/tools/sync_momentum_threshold.py
```

只核对一致性、不改文件：

```bash
python collectors/tools/sync_earnings_version.py --check
python collectors/tools/sync_result_journal.py --check
python collectors/tools/sync_momentum_threshold.py --check
```

这些工具只同步标记的公共代码块，不会把独立策略的全部修改自动同步到批量版。修改策略时仍需同步检查两个入口。

## 行业动量20%边界

行业动量的前排股票只计入20个交易日涨幅**严格大于20%**的股票，恰好20%不计入。独立版和批量版共用 `rules/momentum_threshold.py` 的内嵌规则，按行情端点提供的十进制值精确比较 `5 × 期末收盘价 > 6 × 期初收盘价`，不先转浮点、计算除法或用容差/四舍五入改变边界。该判断保留多位小数输入精度，不受 Decimal 计算精度设置影响；空值、非有限数和非正价格不参与统计。若上游已经损失精度，本规则无法恢复原始行情。

此修复只影响之后重新计算的结果，**不会自动修正已入库的历史计数、动量或排名**。批量采集仍保留已有日期，网页重新同步缓存也不会重算源数据。修复历史需另行确认受影响日期、使用的行情口径及安全重算方案；不要为应用本修复而清空历史表。

## 配置与目录约定

所有采集脚本直接维护在本目录根层，不再保留 `本地专用/` 副本。该目录及其中的旧策略副本、超短线历史研究脚本和说明已按要求删除。

日常批量入口为 `策略批量采集_数据库版.py`。环境初始化在没有现有配置时，静态读取 `mysql_connector.py` 的 `DB_CONFIG`，不会执行采集源码；找不到脚本时使用配置模板。已有 `backend/.env`、`config/app.config` 或 `APP_ENV_FILE` 配置仍按原规则保留。

聚宽运行前需填写可用连接配置，提交 Git 前须还原为占位值。不要将数据库密码、令牌或研究输出提交 Git。目录清理不涉及数据库表、历史快照、统计结果和运行密钥。

## 配置一次，所有入库脚本共用

1. 在 `mysql_connector.py` 的 `DB_CONFIG` 填写主机、端口、库名、账号和密码。各策略及 ETF 历史补采统一使用该目标。
2. 同一文件的 `DB_OPTIONS` 管理驱动、字符集、连接超时、重试及连接池设置。`driver=auto` 优先 MySQLdb，不可用时尝试 PyMySQL。
3. 将 `mysql_connector.py` 上传到聚宽研究目录，不要只粘贴其内容到单元格；随后粘贴或上传需要运行的采集脚本。
4. 配置文件修改并重新上传后，重启 Notebook 内核，再重新运行策略，以免 Python 模块缓存继续使用旧连接。

各入口取得配置副本，不互相修改配置；策略的时间范围、筛选参数和是否写库仍在各自脚本中设置。`ETF分类去重_研究版.py` 不写数据库，因此无需导入连接配置。
