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
├── config/                  # 生产配置 app.config（不提交）与配置模板
├── deploy/                  # Linux systemd 与 Nginx 配置
├── docs/                    # 架构、数据口径与 Linux 部署指南
├── log/                     # 启动时生成，运行日志与每日归档，不提交 Git
├── start.cmd / start.ps1 / start.sh
└── compose.yaml
```

## 数据库配置与 GitHub 提交

数据库配置与代码分开管理，前端只通过 `/api` 访问后端，不接触数据库凭据。

| 环境 | 配置位置 | 读取方式 |
| --- | --- | --- |
| 本地开发 | `backend/.env` | 默认自动读取 |
| Linux 生产部署 | `config/app.config` | `start.sh` 和 systemd 默认选择此文件 |
| 自定义部署 | 自行指定的配置文件 | 启动前设置 `APP_ENV_FILE` |

仓库只提交 **`backend/.env.example`** 和 **`config/app.config.example`** 配置模板。真实 `config/app.config` 及 `.env` 文件已被 Git 忽略；生产配置位于项目根目录的 `config/` 下，发布时须保留该目录中的真实配置。文件采用 UTF-8 的 `KEY=VALUE` 格式。

本地首次使用，复制模板并用编辑器填写 `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`：

```powershell
# Windows；已有配置时保留原文件
if (-not (Test-Path backend/.env)) { Copy-Item backend/.env.example backend/.env }
```

```bash
# Linux；已有配置时保留原文件
if [ ! -e backend/.env ]; then (umask 077; cp backend/.env.example backend/.env); fi
```

生产部署先将 `config/app.config.example` 复制为 `config/app.config`（已有文件不要覆盖），填写数据库连接信息。使用生产配置手动启动：

```bash
export APP_ENV_FILE=config/app.config
bash start.sh
```

```powershell
$env:APP_ENV_FILE = 'config/app.config'
.\start.cmd
```

配置优先级为 **进程环境变量 > 选中的配置文件 > 程序默认值**。设置 `APP_ENV_FILE` 后只读取指定文件，不与 `backend/.env` 合并；直接运行 Python 或 Windows 启动器且未设置时读取 `backend/.env`；Linux `start.sh` 会自动选择 `config/app.config`。相对路径以项目根目录为基准，systemd 模板使用相对项目根目录的 `config/app.config`。显式指定的文件不存在、不可读或路径为空时，启动会报错停止。配置修改后需要重启应用。

`APP_ENV_FILE` 必须设置在启动进程的环境中，不能写在它所选择的配置文件里。Windows 双击启动不会继承另一个 PowerShell 窗口临时设置的变量，应在设置变量的同一窗口启动。

Docker Compose 默认读取 `config/app.config`，也支持 `APP_ENV_FILE` 覆盖：在宿主机读取文件，将配置值注入容器环境，不把配置文件打包进镜像。项目根目录可选的 `.env` 仅用于 Compose 参数（`APP_ENV_FILE` 路径、`WEB_BIND_ADDRESS`、`WEB_PORT`）；Python 原生启动不读取这个根目录文件。

生产目录权限、已有配置迁移和 Docker 用法见 [配置管理说明](docs/configuration.md)。

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

初始化脚本发现 `APP_ENV_FILE` 时，只校验指定配置，不创建本地副本。否则保留已有 `backend/.env`；缺失时从同级 `../original/mysqltrade.py` 解析连接常量，若没有该文件则复制 `.env.example` 模板，等待填写。整个过程不执行原策略，也不输出密码。

## Linux 快速启动（Ubuntu / Red Hat）

安装 Python 3.11+、Node.js 22.12+ 和 npm 后，在项目根目录执行：

```bash
# Ubuntu 24.04
bash scripts/setup.sh --python python3
# RHEL 9.2+ 则将上一条命令改为：bash scripts/setup.sh --python python3.11
# 配置 config/app.config 的数据库连接，然后启动
bash start.sh
```

访问 `http://服务器IP:5173`，按 `Ctrl+C` 停止。默认监听所有 IPv4 网卡；仅本机访问时使用 `bash start.sh --host 127.0.0.1`。

服务器长期运行提供 **systemd + Nginx** 模板，支持开机启动、失败重启和日志查看；也可以用 Docker Compose。完整安装、权限、SELinux、端口和升级步骤见 [Linux 部署指南](docs/linux-deployment.md)。从 Windows 复制代码时，不要复制 `.venv` 和 `node_modules`。

### setup.sh 提示缺少 Node.js / npm

如果出现 `Install Node.js 22.12+ and npm first.`，表示当前用户的 PATH 中没有找到 `node` 命令。项目需要 Node.js 构建前端；`setup.sh` 只检查环境，不会自动安装系统软件。

以下步骤在 Bash 中执行。安装 nvm、Node.js 和运行项目时使用同一个部署用户，不要对这些命令加 `sudo`，否则可能切换用户并丢失 nvm 的环境。使用 [nvm 官方安装方式](https://github.com/nvm-sh/nvm#installing-and-updating) 安装 Node.js 22：

```bash
export NVM_DIR="$HOME/.nvm"
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.8/install.sh | bash

source "$NVM_DIR/nvm.sh"
nvm install 22
nvm use 22
nvm alias default 22

node -v
npm -v
```

确认 `node -v` 为 22.12 或更高版本，且 `npm -v` 正常输出版本号。上面的下载步骤需要能够访问 GitHub 和 Node.js 下载站点。

如果提示 `curl: command not found`，先按系统选择安装 curl，再回到上面的步骤：

```bash
# Ubuntu
sudo apt update
sudo apt install -y curl ca-certificates

# Red Hat：使用这条替代上面的 Ubuntu 命令
sudo dnf install -y curl ca-certificates
```

如果已经通过 nvm 安装过 Node.js，但新终端仍找不到命令，通常只需加载原来的 nvm 环境并选择版本：

```bash
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
nvm use 22
node -v
npm -v
```

回到项目根目录，重新执行安装命令（按系统选择一条）：

```bash
# Ubuntu 24.04
bash scripts/setup.sh --python python3

# RHEL 9.2+
bash scripts/setup.sh --python python3.11
```

安装成功后执行 `bash start.sh`，其他机器访问 `http://服务器IP:5173`。若仍失败，提供新的错误信息和 `cat /etc/os-release` 输出，以便按具体发行版排查。

## 分别运行前后端

后端（在 `backend` 中）：

```powershell
.\.venv\Scripts\python.exe run.py --host 127.0.0.1 --port 8000
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

## 运行日志（按天轮转）

通过 `start.cmd`、`start.ps1`、`bash start.sh` 或后端 `python run.py` 启动后，自动在**项目根目录**创建 `log/`，不随终端当前所在目录改变。日志同时输出到终端和 UTF-8 文件：

| 文件 | 内容 |
| --- | --- |
| `log/app.log` | 启动、关闭、API 访问、应用告警和错误；一键开发启动还收集 Vite 的标准输出与错误输出 |
| `log/error.log` | ERROR / CRITICAL 级别日志及异常堆栈，错误也会出现在 app.log 中 |
| `log/app.log.2026-10-16`、`log/error.log.2026-10-16` | 按日期归档的历史日志，日期为示例 |

采用**运行环境本地时区的每日零点**作为轮转边界，跨日后的第一条日志触发归档；没有日志时不会空转创建归档。默认分别保留 30 份历史日文件，超出数量后在轮转时删除最旧文件。重启服务继续追加当天日志；多进程使用文件锁协调写入和轮转。不要手动删除运行中的锁文件（`.__*.lock`），也不要让外部 logrotate 同时轮转这两份文件。

标准格式为：`ISO 8601 时间（毫秒与时区） | 级别 | 进程号 | 日志来源 | 内容`。例如：

```text
2026-10-16T15:30:00.123+08:00 | INFO     | pid=12345 | app.main | Application ready
2026-10-16T15:30:01.456+08:00 | INFO     | pid=12345 | uvicorn.access | 192.168.1.20:50123 - "GET /api/v1/health HTTP/1.1" 200
```

异常堆栈每行也有相同前缀。日志会屏蔽已配置的数据库密码、连接 URL 中的凭据以及常见 password/token 字段；不主动记录请求体、Cookie 或 Authorization 请求头。`log/`（包含归档和锁文件）已被 Git 忽略。

可在当前选中的配置文件（本地 `backend/.env` 或生产 `config/app.config`）中添加以下选项；不添加时使用这些默认值：

```dotenv
LOG_DIR=log
LOG_LEVEL=INFO
LOG_RETENTION_DAYS=30
```

`LOG_LEVEL` 支持 DEBUG / INFO / WARNING / ERROR / CRITICAL；INFO 包含常规访问日志。相对 `LOG_DIR` 以项目根目录为基准，所有写入同一目录的进程应使用相同轮转设置；修改后需重启。Docker 容器的本地时区通常是 UTC，日志时间会明确显示时区偏移。

升级旧环境时，先安装新增依赖（项目根目录，按系统选择一条），再重启：

```bash
# Linux
backend/.venv/bin/python -m pip install -r backend/requirements.lock.txt
```

```powershell
# Windows
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock.txt
```

查看日志：

```bash
# Linux
tail -n 100 -f log/app.log
tail -n 100 -f log/error.log
```

```powershell
# Windows
Get-Content .\log\app.log -Tail 100 -Wait
```

systemd 继续同步写入 journal，但需要先给服务账号准备可写的项目日志目录；Docker 会把后端应用日志映射到宿主机项目的 `./log/`。具体权限和升级步骤见 [Linux 部署指南](docs/linux-deployment.md#日志目录与服务升级)。Nginx 自身的访问/错误日志仍由 Nginx 或 Docker 管理；浏览器 JavaScript 错误尚未集中上报。

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

访问 http://服务器IP:8080（本机也可使用 127.0.0.1）。前端由 Nginx 服务，后端在内部网络运行；数据库继续使用 `config/app.config` 指定的现有 MySQL。Docker 配置未在没有 Docker 的环境下实际构建验证。

本版本默认允许通过服务器 IP 访问网页，没有用户登录。开发模式放行 5173，Docker 模式放行 8080，原生 Nginx 部署放行 80；同时检查云安全组。API 通过前端代理访问，无需对外开放 8000。若要开放到外网，应在部署层添加认证和 HTTPS。建议数据库使用仅有目标表 SELECT 权限的账号；已有采集账号可用于本地联调。`.env` 已被 Git 和 Docker 构建忽略。

架构及扩展方式见 [docs/architecture.md](docs/architecture.md)。
