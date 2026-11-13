# ShowAllOpts · 市场研究工作台

市场数据采集、增量同步与可视化。React 前端通过 FastAPI 读取本地快照；聚宽脚本和公开接口按各自数据口径采集，计算结果写入数据库。

## 功能入口

| 页面 | 内容 |
| --- | --- |
| 市场情绪 | 市场热度、抱团拥挤度、国家队动态、两融担保比例、估值标尺、创红比 |
| 动量观察 | 行业动量、ETF / LOF 动量 |
| 微盘观察 | 微盘温度计、新高新低 |
| 业绩选股 | 奶牛股、高增长、净利润断层、三类高股息筛选 |
| 机构持仓 | 机构披露、股东户数、社保新进与加仓 |
| 大宗商品 | A00 铝、CCI 动力煤 |
| 博主动态 | 微博、雪球与组合调仓，Telegram / 飞书通知 |
| 管理 | 用户、个人持仓、统一配置、采集计划、数据同步 |

## 快速开始

环境：Python 3.11+、Node.js 22.12+、MySQL。Python 与 Node 依赖使用仓库锁定文件；不要复制其他系统的虚拟环境。

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

- 聚宽批量入口：[`collectors/策略批量采集_数据库版.py`](collectors/策略批量采集_数据库版.py)，内置 12 个策略；与 `mysql_connector.py` 一起上传。各策略独立可运行，不相互导入。
- 公开接口采集由项目服务调度，管理员在「系统配置」设置时间与凭据。功能页保留更新、停止和任务状态。
- 页面「刷新」读取本地；「数据同步」从 MySQL 更新快照。同步不重新计算策略，也不修改源库。
- 日常同步优先结果变更日志、日期增量或小窗口复核；旧采集器兼容对账。首次建立基线，失败保留旧快照。
- 手工修订源历史、超出 ETF 最近三日窗口的补采，使用对应模块的「完整重新同步」。详情见[同步策略](docs/operations/cache-sync-strategies.md)。

[采集脚本清单](collectors/README.md) · [历史补采覆盖](docs/data/joinquant-history-coverage.md) · [运维工具清单](scripts/README.md)

## 验证与发布

```bash
# backend：隔离测试，不使用生产库
python -m pytest -q

# frontend
npm test
npm run build
npm run test:e2e
```

修改聚宽公共规则后，在项目根目录执行对应 `collectors/tools/sync_*.py --check`；修改前端后验证桌面和移动端。开发约定见[工程规范](docs/development/engineering.md)。

推送 main 会运行 GitHub Actions。只有应用、依赖或部署配置发生变化才执行服务器部署；仅修改采集脚本、测试、文档会跳过部署。检查失败不发布，发布失败走恢复流程，见[自动部署](docs/operations/github-auto-deployment.md)。

## 文档

完整入口：[文档目录](docs/README.md)。数据定义只在模块文档维护，运行方式在运维文档维护；README 保留导航与最小启动步骤，不再累计历史变更说明。
