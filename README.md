# ShowAllOpts · 市场研究工作台

市场数据采集、增量同步与可视化。React 页面通过 FastAPI 读取已发布的本地快照，聚宽脚本和后端公开源采集器独立供数。页面刷新、MySQL 到本地的数据同步、重新采集是三种不同操作。

## 功能入口

| 页面 | 内容 |
| --- | --- |
| 市场情绪 | 市场热度、抱团拥挤度、国家队动态、两融担保比例、估值标尺、创红比 |
| 动量观察 | 行业动量、ETF / LOF 动量 |
| 微盘观察 | 微盘温度计、新高新低 |
| 业绩选股 | 奶牛股、高股息低估值（非银行／银行） |
| 机构持仓 | 七类机构披露持仓、股东户数、社保新进与加仓 |
| 大宗商品 | A00 铝、CCI 动力煤 |
| 博主动态 | 微博、雪球博主动态与雪球组合调仓；Telegram 支持正文和图片，飞书发文字 |
| 管理 | 账号、管理员个人持仓、系统配置、采集计划、数据同步 |

## 快速开始

环境：Python 3.11+、Node.js 22.x（≥22.13）或 24+、MySQL。Python 与 Node 依赖使用仓库锁定文件；不要复制其他系统的虚拟环境。

```powershell
# Windows，项目根目录
.\scripts\setup.ps1 -Python "C:\path\to\python.exe"
.\start.cmd
```

```bash
# Linux，项目根目录
bash scripts/setup.sh --python python3
bash start.sh
```

开发页面为 `http://127.0.0.1:5173`。首次使用按[账号与 HTTPS](docs/operations/账号与HTTPS配置.md)创建管理员。公网部署使用 HTTPS，长期运行按[Linux 部署指南](docs/operations/linux-deployment.md)配置 systemd 与 Nginx。

| 配置 | 用途 |
| --- | --- |
| `backend/.env` | 本地开发配置；参考 `.env.example` |
| `config/app.config` | Linux 生产配置；参考 `app.config.example` |
| `APP_ENV_FILE` | 显式选择配置文件，路径相对项目根目录 |
| `collectors/mysql_connector.py` | 聚宽脚本的统一数据库配置，上传至研究环境 |

真实配置、密钥、日志、缓存不提交。已有配置不覆盖；配置优先级见[配置管理](docs/operations/configuration.md)。

## 目录

```text
backend/       API、业务服务、同步及测试
frontend/      页面、共用组件、样式及浏览器测试
collectors/    聚宽独立入口、批量入口、公共规则及嵌入工具
scripts/       开发启动、发布和运维命令
config/        配置模板；真实配置仅本机保存
deploy/        Nginx、systemd、证书维护和自动部署入口
docs/          文档索引，按 data / operations / development 分类
log/           运行时数据，不提交
.runtime/      本地构建及发布恢复文件，不提交
```

## 采集与同步

- 聚宽批量入口：[`collectors/策略批量采集_数据库版.py`](collectors/策略批量采集_数据库版.py)，内置 8 项：行业动量、抱团拥挤度、市场热度、微盘温度计、微盘新高新低、ETF/LOF 动量、机构持仓和估值标尺。与 `mysql_connector.py` 一起上传；聚宽入口之间不互相导入。
- 公开接口采集由项目服务调度，管理员在「系统配置」调整时间和凭据；页面按模块提供手动更新、状态及适用的取消操作。除微博、雪球博主动态外，应用内自动任务在交易所休市或日历未知时暂停；休市日仍可手动采集，失败不进入自动补采。当前内置交易日历覆盖 2026 年。
- 手动日常采集以各模块最近已到期的配置时点及自身统计日口径为上限，点击时冻结，不因排队改变；独立历史导入及实时监控例外。配置允许时，实际运行的手动和自动采集终态使用相同 Telegram 格式，包括零变更和失败；不为跳过及缓存重试另发通知。
- 页面「刷新」读取已发布结果；「数据同步」从 MySQL 更新行业动量、ETF/LOF 动量、微盘温度计、微盘新高新低、机构持仓的本地快照。同步不重新计算策略，也不修改源库。
- 数据同步只包含数据库导入模块。创红比、市场热度、抱团拥挤度、高股息选股、奶牛股、估值标尺及国家队动态（ETF 份额）由各自外部接口采集后发布本地结果，不出现在同步列表，也不参与一键/全量/定时数据库同步。已有数据和采集后的内部快照发布保留。
- 日常同步优先结果变更日志、日期增量或小窗口复核；旧采集器兼容对账。首次建立基线，失败保留旧快照。
- 数据库导入模块手工修订源历史后，使用对应模块的「完整重新同步」。详情见[同步策略](docs/operations/cache-sync-strategies.md)。

[采集脚本清单](collectors/README.md) · [历史补采覆盖](docs/data/joinquant-history-coverage.md) · [运维工具清单](scripts/README.md)

## 架构

[架构与扩展约定](docs/development/architecture.md)集中说明五项重构的代码边界：工程门禁、任务生命周期、数据层、可选执行器、前后端共用基础设施。默认仍为 API 与任务同进程的 combined 模式；独立 ETF/市场执行器只用于隔离试点，正式双服务 CD 尚未切换。业务口径见各模块文档。

## 验证与发布

```bash
# 根目录：安装开发质量工具后检查
python backend/check_quality.py

# backend/：隔离测试，不使用生产库
python -m pytest -q

# frontend/
npm run quality:check
npm test
npm run build
npm run test:e2e
```

修改聚宽公共规则后，在项目根目录执行对应 `collectors/tools/sync_*.py --check`；修改前端后验证桌面和移动端。开发约定见[工程规范](docs/development/engineering.md)。

对 main 的 PR 运行完整 GitHub Actions 检查，不部署；main 推送由统一 CI gate 放行后才允许部署。隔离 MySQL 5.7.44 / 8.4 兼容测试与模拟浏览器用例不代表生产业务采集已验证。服务器以当前健康发布版本为基准比较累计差异；存在未发布的运行相关差异才实际部署。精确根路径 `README.md`、`docs/`、采集脚本及指定测试目录由发布影响判定排除；如果仍有未发布的运行差异，文档提交也可能触发累计发布。检查失败不发布，发布失败走恢复流程，见[自动部署](docs/operations/github-auto-deployment.md)。

## 文档

完整入口：[文档目录](docs/README.md)。数据定义只在模块文档维护，运行方式在运维文档维护；README 保留导航与最小启动步骤，不再累计历史变更说明。
