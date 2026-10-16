# ShowAllOpts · 市场研究工作台

严格前后端分离的行业动量可视化项目。Python API 只读现有 MySQL `industry_momentum` 表，React 网页展示模块首页、最新交易日的全部行业热力图，以及行业历史趋势。

## 技术栈

- 后端：Python 3.11+、FastAPI、SQLAlchemy 2、PyMySQL、Pydantic Settings。
- 前端：Node.js 22.12+、React、TypeScript、Vite、Apache ECharts。
- 测试：pytest + SQLite 隔离数据库、Vitest、Playwright。
- 部署：可独立启动，也提供 Docker Compose + Nginx 反向代理。

## 目录约定

```text
showAllOpts/
├── backend/
│   ├── app/
│   │   ├── api/             # HTTP 路由、参数校验
│   │   ├── core/            # 环境配置、数据库连接
│   │   ├── models/          # 现有数据库表映射
│   │   ├── repositories/    # 只读 SQL 查询
│   │   ├── schemas/         # API 响应结构
│   │   ├── services/        # 动量快照、区间统计、缺失记录处理
│   │   └── main.py          # 应用装配与错误处理
│   ├── tests/
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── api/             # 统一 HTTP 客户端
│   │   ├── components/      # 布局、热力图、折线图和状态组件
│   │   ├── hooks/           # 可取消的数据请求
│   │   ├── lib/             # 颜色、排序、格式化
│   │   ├── pages/           # 首页、行业全景、行业趋势
│   │   ├── styles/          # 设计变量、响应式布局、动效
│   │   └── types/           # API 类型
│   └── tests/               # 浏览器交互测试
├── scripts/                 # 环境初始化、启动、数据库只读检查
├── deploy/                  # Linux systemd 与 Nginx 配置
├── docs/                    # 架构、数据口径与 Linux 部署指南
├── start.cmd / start.ps1 / start.sh
└── compose.yaml
```

## 数据库配置与 GitHub 提交

真实数据库连接信息仅保存在 **`backend/.env`**，由 Python 后端读取；该文件已被 `.gitignore` 排除，不上传 GitHub。仓库中的 **`backend/.env.example`** 只有配置项和占位值，用于在新机器上生成配置。

首次克隆仓库后，先复制模板并填写 `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`，再安装依赖并启动：

```powershell
# Windows，在项目根目录执行；已有 .env 时保留原文件
if (-not (Test-Path backend/.env)) { Copy-Item backend/.env.example backend/.env }
```

```bash
# Linux，在项目根目录执行；已有 .env 时保留原文件
if [ ! -e backend/.env ]; then (umask 077; cp backend/.env.example backend/.env); fi
```

填写配置请使用本地编辑器，不把实际密码放入命令行、代码或 README。已有配置无需重新生成。前端通过 `/api` 访问后端，前端代码和构建文件不包含数据库账号密码。Docker 的 `env_file` 在容器运行时加载配置，不将真实 `.env` 打包进镜像。

项目根目录可选的 `.env` 仅用于 Docker 的 `WEB_BIND_ADDRESS`、`WEB_PORT` 参数，同样不上传。数据库配置固定放在 `backend/.env`；发布新版本时需保留服务器上的该文件。

## Windows 快速启动

当前工作区已经安装依赖并生成本地 `backend/.env`。双击 `start.cmd`，或在项目根目录执行：

```powershell
.\start.cmd
```

其他机器访问 **http://服务器IP:5173**，本机也可访问 **http://127.0.0.1:5173**；API 文档在 **http://127.0.0.1:8000/docs**。按 `Ctrl+C` 同时停止前后端。也提供 `start.ps1`；若 PowerShell 执行策略阻止脚本，可使用 `powershell -ExecutionPolicy Bypass -File .\start.ps1`。

首次在另一台机器安装：

```powershell
.\scripts\setup.ps1 -Python "C:\path\to\python.exe"
.\start.ps1
```

初始化脚本只在 `.env` 不存在时，从同级 `../original/mysqltrade.py` 提取连接配置。它使用 AST 解析常量，不执行原策略，也不会输出密码。独立复制本项目时，可自行把 `backend/.env.example` 复制为 `backend/.env` 并填写连接参数。

## Linux 快速启动（Ubuntu / Red Hat）

安装 Python 3.11+、Node.js 22.12+ 和 npm 后，在项目根目录执行：

```bash
# Ubuntu 24.04
bash scripts/setup.sh --python python3
# RHEL 9.2+ 则将上一条命令改为：bash scripts/setup.sh --python python3.11
# 配置 backend/.env 的数据库连接，然后启动
bash start.sh
```

访问 `http://服务器IP:5173`，按 `Ctrl+C` 停止。默认监听所有 IPv4 网卡；仅本机访问时使用 `bash start.sh --host 127.0.0.1`。

服务器长期运行提供 **systemd + Nginx** 模板，支持开机启动、失败重启和日志查看；也可以用 Docker Compose。完整安装、权限、SELinux、端口和升级步骤见 [Linux 部署指南](docs/linux-deployment.md)。从 Windows 复制代码时，不要复制 `.venv` 和 `node_modules`。

## 分别运行前后端

后端（在 `backend` 中）：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

前端（另一个终端，在 `frontend` 中）：

```powershell
npm run dev
```

前端通过 `/api` 开发代理访问后端，不含数据库凭据。Linux 请使用上面的安装和启动脚本。

## 页面与数据口径

- 首页：研究模块入口、已有数据概览；当前开放行业动量模块。
- 热力图：申万二级，取数据库中不晚于今天（北京时间）的最大交易日期；包含零分行业，按动量降序、行业代码降序逐行排列。左上最高，右下最低；最后一行未满时向右对齐。
- 每个行业使用等大小矩形，名称、分数、排名可见。色阶采用 `log(1 + 分数)`，避免少数极大值让其余行业颜色无法区分；颜色只影响显示，不修改分数或排名。
- 行业下钻：默认最近 365 个自然日，终点为进入时的最新快照日；支持 30 / 90 / 180 / 365 日范围，刷新页面保留行业和快照日期。
- 行业标识使用分类 + 行业代码。名称变化不把同一行业拆成多条线。
- 零值为有效记录。某天其他行业有数据、该行业缺失时返回 `null`，折线保留断点；不为周末或节假日填零。全市场整日缺失无法仅凭这张表识别。
- 区间变化是动量分数的绝对差，不是收益率；当日数据是否完整仍取决于采集程序。界面不会声称数据实时或已经收盘确认。
- 本应用不重新计算动量、不补历史数据、不修改表结构。已有数据的两位小数精度保持原样。

## API

| 请求 | 用途 |
| --- | --- |
| `GET /api/v1/health` | 服务存活状态，不代表数据库连通 |
| `GET /api/v1/modules` | 模块目录 |
| `GET /api/v1/industry-momentum/latest` | 最新完整读取的日期快照及前一个有记录日期的差值 |
| `GET /api/v1/industry-momentum/industries/{code}/history?days=365&end_date=2026-10-16` | 单行业历史及区间统计 |

数据接口支持 `industry_type=sw_l1/sw_l2/sw_l3`，网页当前固定使用 `sw_l2`。数据库连接失败返回 503，未知行业返回 404，非法参数返回 422；响应不泄露连接信息。

## 验证

```powershell
# 项目根目录，只读检查真实数据库
.\backend\.venv\Scripts\python.exe .\scripts\check_database.py

# backend 目录：测试用 SQLite，不连接真实数据库
.\.venv\Scripts\python.exe -m pytest -q

# frontend 目录
npm test
npm run build
npm run test:e2e
```

浏览器测试使用本机 Chrome。若没有 Chrome，可执行 `npx playwright install chromium`，并用 `PLAYWRIGHT_BROWSER=chromium` 运行测试。测试行情由路由拦截提供，属于测试夹具，不会写进数据库，也不会替代正常页面的真实 API。

## 构建与部署

```powershell
docker compose up --build -d
```

访问 http://服务器IP:8080（本机也可使用 127.0.0.1）。前端由 Nginx 服务，后端在内部网络运行；数据库继续使用 `.env` 指定的现有 MySQL。Docker 配置未在没有 Docker 的环境下实际构建验证。

本版本默认允许通过服务器 IP 访问网页，没有用户登录。开发模式放行 5173，Docker 模式放行 8080，原生 Nginx 部署放行 80；同时检查云安全组。API 通过前端代理访问，无需对外开放 8000。若要开放到外网，应在部署层添加认证和 HTTPS。建议数据库使用仅有目标表 SELECT 权限的账号；已有采集账号可用于本地联调。`.env` 已被 Git 和 Docker 构建忽略。

架构及扩展方式见 [docs/architecture.md](docs/architecture.md)。
