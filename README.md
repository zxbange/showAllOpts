# ShowAllOpts · 市场研究工作台

严格前后端分离的市场研究项目。聚宽独立采集脚本负责写入 MySQL，Python 后端每天同步到本地只读快照，React 网页提供行业动量、抱团拥挤度、微盘温度计、ETF 份额、净利润断层、微盘新高新低和高股息选股七个模块。

## 技术栈

- 后端：Python 3.11+、FastAPI、SQLAlchemy 2、PyMySQL、Pydantic Settings。
- 前端：Node.js 22.12+、React、TypeScript、Vite、Apache ECharts。
- 测试：pytest + SQLite 隔离数据库、Vitest、Playwright。
- 部署：开发启动器、Linux systemd + Nginx、Docker Compose + Nginx。

## 目录约定

```text
showAllOpts/
├── backend/
│   ├── app/
│   │   ├── api/             # HTTP 路由、参数校验
│   │   ├── core/            # 环境配置、数据库连接、本地快照与调度
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
│   │   ├── pages/           # 总览与各业务模块
│   │   ├── styles/          # 设计变量、响应式布局、动效
│   │   └── types/           # API 类型
│   └── tests/               # 浏览器交互测试
├── scripts/                 # 环境初始化、启动、发布与缓存同步
├── collectors/              # 聚宽独立采集脚本（仅脱敏模板）
├── config/                  # 生产配置 app.config（不提交）与配置模板
├── deploy/                  # Linux systemd 与 Nginx 配置
├── docs/                    # 架构、数据口径与 Linux 部署指南
├── log/                     # 运行日志、每日归档与 cache/ 本地快照，不提交 Git
├── deploy.sh                # Linux 一键更新发布
├── start.cmd / start.ps1 / start.sh
└── compose.yaml
```

## 数据库配置与 GitHub 提交

数据库配置与代码分开管理，前端只通过 `/api` 访问后端，不接触数据库凭据。

| 环境 | 配置位置 | 读取方式 |
| --- | --- | --- |
| Windows 开发 / 直接运行 Python | `backend/.env` | 未指定 `APP_ENV_FILE` 时读取 |
| Linux 启动 / 生产部署 | `config/app.config` | `start.sh`、systemd 和 Compose 默认选择此文件 |
| 自定义部署 | 自行指定的配置文件 | 启动前设置 `APP_ENV_FILE` |

仓库只提交 **`backend/.env.example`** 和 **`config/app.config.example`** 配置模板。真实 `config/app.config` 及 `.env` 文件已被 Git 忽略；生产配置位于项目根目录的 `config/` 下，发布时须保留该目录中的真实配置。文件采用 UTF-8 的 `KEY=VALUE` 格式。

本地首次使用，复制模板并用编辑器填写 `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`：

```powershell
# Windows；已有配置时保留原文件
if (-not (Test-Path backend/.env)) { Copy-Item backend/.env.example backend/.env }
```

```bash
# Linux；在项目根目录执行，已有生产配置时保留原文件
if [ ! -e config/app.config ]; then
    (umask 077; cp config/app.config.example config/app.config)
fi
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

使用 `/home/ubuntu/showAllOpts` 部署时，目录权限和服务配置按下方 [systemctl 管理指引](#linux-后台服务systemctl-管理) 操作；其他部署目录的配置迁移和 Docker 用法见 [配置管理说明](docs/configuration.md)。

## Windows 快速启动

当前工作区已经安装依赖并生成本地 `backend/.env`。双击 `start.cmd`，或在项目根目录执行：

```powershell
.\start.cmd
```

其他机器访问 **http://服务器IP:5173**，本机也可访问 **http://127.0.0.1:5173**；API 文档在 **http://127.0.0.1:8000/docs**。按 `Ctrl+C` 同时停止前后端。也提供 `start.ps1`；若 PowerShell 执行策略阻止脚本，可使用 `powershell -ExecutionPolicy Bypass -File .\start.ps1`。

Windows 启动器已增加进程树保护：关闭启动终端或启动器被结束时，Windows 会回收本次启动的 API、Vite 及其后代进程。正常停止仍建议先按 `Ctrl+C`，让退出日志写完。更新代码后需重新启动，保护机制不会追溯接管旧版本已经启动的进程。

### Windows 提示端口已被占用

先检查是否仍有本项目的启动窗口；有则在原窗口按 `Ctrl+C`。如果旧版本关闭窗口后留下进程，在 PowerShell 查看监听者：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000,5173 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

只针对确认属于本项目的 PID 清理。下面命令先展示进程路径及命令行，请确认是当前项目的 `backend/run.py` 或 `frontend/node_modules/vite/bin/vite.js`，再手动执行最后一条；不要结束其他服务或批量结束所有 Python / Node 进程。

```powershell
$serviceProcessId = Read-Host '输入上一步要核对的 OwningProcess'
Get-CimInstance Win32_Process -Filter "ProcessId = $serviceProcessId" |
    Select-Object ProcessId,Name,ExecutablePath,CommandLine
# 确认属于本项目后，按 PID 清理该进程及其后代；另一个端口如仍占用则重复核对
taskkill /PID $serviceProcessId /T /F
```

清理旧进程后，使用更新后的 `start.cmd` 或 `start.ps1` 启动。启动器不会自动结束占用端口的未知程序。机制及验证说明见 [Windows 进程管理](docs/architecture.md#windows-开发启动器的进程管理)。

### Windows 首次安装

首次在另一台机器安装：

```powershell
.\scripts\setup.ps1 -Python "C:\path\to\python.exe"
.\start.ps1
```

初始化脚本发现 `APP_ENV_FILE` 时，只校验指定配置，不创建本地副本。否则保留已有 `backend/.env`；缺失时从同级 `../original/mysqltrade.py` 解析连接常量，若没有该文件则复制 `.env.example` 模板，等待填写。整个过程不执行原策略，也不输出密码。

## Linux 快速启动（Ubuntu / Red Hat）

本节是依赖终端的开发启动方式。服务器后台运行和开机启动请使用 [systemctl 管理指引](#linux-后台服务systemctl-管理)。

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

## 本地缓存与定时同步

默认已启用本地 SQLite 快照，覆盖七个模块的列表、历史曲线、筛选与详情。页面请求只查询本机磁盘，不再逐次访问公网 MySQL；操作系统会缓存常用磁盘页。首次启动后台准备快照，之后重启可继续使用已有快照。每天**北京时间 18:00** 自动从 MySQL 同步，停机错过时间后会在下次启动补做一次。

网页右上角的“本地数据 / 数据同步”可查看各模块的上次成功同步时间、失败状态，以及手动同步一个或全部模块。同步失败保留旧数据；无初始快照时显示“本地数据尚未准备好”，不会让大量页面请求回源数据库。缓存记录的是同步时数据库内的数据，不能代替聚宽采集。

已有配置无需修改即可启用默认缓存。可在 Linux `config/app.config` 或 Windows `backend/.env` 中显式设置（修改后重启后端）：

```dotenv
CACHE_ENABLED=true
CACHE_AUTO_REFRESH=true
CACHE_REFRESH_TIME=18:00
# 网页手动同步的私有随机密钥；留空则禁用网页触发，仍可使用下面的命令
CACHE_REFRESH_TOKEN=
```

如需网页手动更新，给 `CACHE_REFRESH_TOKEN` 填入自行生成的随机值，重启后在同步面板输入相同密钥。它不会进入前端构建或浏览器存储；真实配置继续不提交 GitHub。密钥通过请求头发送，公开访问建议使用 HTTPS。

也可直接在服务器命令行同步，无需网页密钥：

```bash
cd /home/ubuntu/showAllOpts
# Linux：默认读取 config/app.config，使用与服务相同的部署账号
backend/.venv/bin/python scripts/refresh_cache.py
# 仅更新高股息；支持七个模块 ID
backend/.venv/bin/python scripts/refresh_cache.py --module dividend-selection
# 仅检查本地状态，不连接 MySQL
backend/.venv/bin/python scripts/refresh_cache.py --status
```

Windows PowerShell：

```powershell
.\backend\.venv\Scripts\python.exe scripts/refresh_cache.py
# 若启动时指定了生产配置，手动同步也选择同一文件
.\backend\.venv\Scripts\python.exe scripts/refresh_cache.py --env-file config/app.config
```

缓存保存在 `log/cache/`（自定义 `LOG_DIR` 时随其变化），已被 Git 忽略；现有 systemd 日志可写权限和 Docker 日志卷可直接复用。多 worker、自动同步与手动同步共用文件锁，同一缓存目录同时只执行一个任务。同步过程写入既有应用日志。

**采集入库后，需等待定时同步或手动同步，页面才会看到新数据。** 各模块原来的刷新按钮只重新读取当前快照，不直接查询 MySQL。若需恢复原来的数据库直连模式，设 `CACHE_ENABLED=false` 并重启。

更新项目仍执行 `bash deploy.sh`。详细的首次准备、失败重试、数据范围和运维说明见 [本地缓存说明](docs/local-cache.md)。

## Linux 后台服务：systemctl 管理

systemd 管理后端 `showallopts-api`，Nginx 提供构建后的前端网页并代理 `/api`。安装完成后使用 `systemctl` 启停后端，关闭终端不会结束服务；不再同时运行 `bash start.sh`，避免占用同一个 8000 端口。

### 首次部署准备

本节按现有 Ubuntu 部署环境编写：代码位于 `/home/ubuntu/showAllOpts`，使用已有的 `ubuntu` 账号运行服务。项目文件夹保留 `showAllOpts` 的大小写；如果您的实际文件夹名不同，请统一替换以下路径。

| 项目 | 本节使用的值 |
| --- | --- |
| 代码目录 | `/home/ubuntu/showAllOpts` |
| 服务账号 / 组 | `ubuntu` / `ubuntu` |
| 数据库配置 | `/home/ubuntu/showAllOpts/config/app.config` |
| 日志目录 | `/home/ubuntu/showAllOpts/log` |
| 后端监听 | `127.0.0.1:8000` |
| Nginx 前端目录 / 访问端口 | `/var/www/showallopts` / `80` |

以 `ubuntu` 用户在现有项目目录安装依赖。若已经安装且没有依赖更新，可跳过 setup：

```bash
cd /home/ubuntu/showAllOpts
bash scripts/setup.sh --python python3
```

Python 需要 3.11+，Node.js 需要 22.12+。首次克隆只包含配置模板，setup 会初始化缺失的 `config/app.config`；请用编辑器填入真实数据库连接信息，保留已有配置。

仓库中的基础 service 模板仍适用于独立账号和 `/opt` 目录部署；本节通过 systemd 的覆盖配置适配现有 `/home/ubuntu/showAllOpts`，无需迁移代码或创建 `showallopts` 账号。不要只安装基础模板后就启动，必须同时完成下一节的覆盖配置。[Linux 部署指南](docs/linux-deployment.md#4-systemd--nginx-后台服务) 与本节使用相同的路径、账号和覆盖配置。

先在原终端按 `Ctrl+C` 停止 `bash start.sh`。若之前已启动 systemd 服务，先执行 `sudo systemctl stop showallopts-api`。在进程停止后准备配置和日志权限：

```bash
cd /home/ubuntu/showAllOpts

# 适配服务运行账号；真实配置仅 ubuntu 可读写
sudo chown ubuntu:ubuntu config config/app.config
sudo chmod 0700 config
sudo chmod 0600 config/app.config

# 新日志目录及旧日志、锁文件统一交给 ubuntu
sudo install -d -o ubuntu -g ubuntu -m 0750 /home/ubuntu/showAllOpts/log
sudo chown -R ubuntu:ubuntu /home/ubuntu/showAllOpts/log

# 验证实际服务账号可以读取配置、执行 Python 并访问数据库
sudo -u ubuntu env APP_ENV_FILE=config/app.config \
    backend/.venv/bin/python scripts/check_database.py
```

### 安装并启动后端服务

数据库检查成功后，安装基础模板并写入本机覆盖配置：

```bash
cd /home/ubuntu/showAllOpts
sudo install -m 0644 deploy/systemd/showallopts-api.service \
    /etc/systemd/system/showallopts-api.service
sudo install -d -m 0755 /etc/systemd/system/showallopts-api.service.d

sudo tee /etc/systemd/system/showallopts-api.service.d/99-ubuntu-home.conf > /dev/null <<'EOF'
[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/showAllOpts/backend
Environment=APP_ENV_FILE=config/app.config
ExecStart=
ExecStart=/home/ubuntu/showAllOpts/backend/.venv/bin/python /home/ubuntu/showAllOpts/backend/run.py --host 127.0.0.1 --port 8000 --workers 2
ProtectHome=read-only
ReadWritePaths=
ReadWritePaths=/home/ubuntu/showAllOpts/log
EOF

sudo systemctl daemon-reload
# enable 设置开机启动；restart 同时适用于首次启动和已有服务更新
sudo systemctl enable showallopts-api
sudo systemctl restart showallopts-api
sudo systemctl status showallopts-api --no-pager
curl --fail http://127.0.0.1:8000/api/v1/health
```

`ExecStart=` 和 `ReadWritePaths=` 空行用于清除基础模板中的旧启动命令和旧日志路径，不能省略。覆盖文件将 `ProtectHome=true` 改成 `read-only`，允许服务读取 `/home` 中的代码，并通过 `ReadWritePaths` 单独允许写入项目日志目录；保留基础模板的 `ProtectSystem=strict`。这些设置遵循 [systemd 文件系统保护规则](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml)。

`APP_ENV_FILE=config/app.config` 由应用按项目根目录解析。终端中的临时 `export` 不会改变 systemd 服务配置。覆盖文件只包含路径和运行参数，不要把数据库密码写入 service。

查看最终合并的配置，确认账号和启动路径已经生效：

```bash
sudo systemctl cat showallopts-api
sudo systemctl show showallopts-api \
    -p User -p Group -p WorkingDirectory -p ExecStart -p ProtectHome -p ReadWritePaths
```

`systemctl cat` 会同时显示基础模板中的旧值和覆盖文件中的新值，以 `systemctl show` 的最终结果为准。如果机器已有其他覆盖文件，也需检查是否冲突。`/health` 仅验证 API 存活，数据库连通性以上面的检查脚本为准。

### 发布前端网页

首次部署还需按 [Nginx 网页和反向代理](docs/linux-deployment.md#nginx-网页和反向代理) 发布 `frontend/dist`、配置实际 IP 或域名，并完成 RHEL 的 SELinux 设置。Nginx 配置准备好后执行：

```bash
sudo nginx -t
# 仅在配置检查通过后执行
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

访问 `http://服务器IP` 或配置的域名，默认端口 **80**，放行对应防火墙和云安全组规则。此部署方式不使用开发服务器的 5173 端口；8000 只供本机 Nginx 访问。

### 日常启动、停止和查看日志

| 操作 | 命令 |
| --- | --- |
| 启动后端 | `sudo systemctl start showallopts-api` |
| 停止后端 | `sudo systemctl stop showallopts-api` |
| 重启后端 | `sudo systemctl restart showallopts-api` |
| 查看状态 | `sudo systemctl status showallopts-api --no-pager` |
| 查看最近 100 条日志 | `sudo journalctl -u showallopts-api -n 100 --no-pager` |
| 实时查看日志 | `sudo journalctl -u showallopts-api -f` |
| 开机启动并立即启动 | `sudo systemctl enable --now showallopts-api` |
| 停止并取消开机启动 | `sudo systemctl disable --now showallopts-api` |

应用同时保留按天轮转的日志文件：

```bash
sudo tail -n 100 -f /home/ubuntu/showAllOpts/log/app.log
sudo tail -n 100 -f /home/ubuntu/showAllOpts/log/error.log
```

`systemctl stop` 会正常停止服务，不会因为模板的 `Restart=on-failure` 自动重启；若保留开机启动设置，下次开机仍会启动。停止后端后，Nginx 静态页面仍可打开，数据接口不可用。只有 Nginx 专门服务本项目时，才使用 `sudo systemctl stop nginx` 一并关闭网页；承载其他网站时应单独停用本项目站点。

### 修改配置或更新代码

以下步骤适用于已经部署好的 **systemd 后端 + Nginx 前端**，以 `ubuntu` 用户运行。项目目录为 `/home/ubuntu/showAllOpts`，Nginx 的 `root` 为 `/var/www/showallopts`；若实际配置不同，请替换对应路径。Ubuntu、RHEL 等 Linux 服务器均可沿用，前提是现有依赖及服务已配置完成。

#### 一键更新发布（推荐）

首次获取新增的发布脚本，先执行一次 `git pull --ff-only`。之后每次项目新增功能或修复问题，只需以部署账号执行：

```bash
cd /home/ubuntu/showAllOpts
bash deploy.sh
```

脚本会自动检查运行环境和本机服务配置 → 拉取 `origin/main` 最新代码（仅快进）→ 安装并构建前端 → 停止 API、同步后端锁定依赖 → 使用 systemd 配置检查数据库读取 → 重启 API、等待健康检查 → 发布网页。**无需再手动运行 setup、复制前端或重启后端。** 第一次需要输入 sudo 密码时按提示输入；不要用 `sudo bash deploy.sh`，避免以 root 安装项目依赖。

默认服务为 `showallopts-api`，Nginx 静态目录为 `/var/www/showallopts`，API 检查地址为 `http://127.0.0.1:8000`。网页使用自定义端口时，可同时检查 Nginx 到后端的代理，例如：

```bash
# 按实际 HTTP/HTTPS 协议和访问端口填写；不会修改 Nginx 监听端口
bash deploy.sh --web-url http://127.0.0.1:8081

# 自定义部署目录/服务/后端端口的示例
bash deploy.sh --web-root /var/www/my-market --service my-market-api \
  --api-url http://127.0.0.1:8001 --web-url https://market.example.com

# 已自行更新到目标提交，跳过联网拉取（仍要求干净的 main 分支）
bash deploy.sh --skip-pull
bash deploy.sh --help
```

使用域名匹配的 Nginx 站点时，`--web-url` 填该站点实际域名；不指定此参数时只检查后端直连接口和 Nginx 运行状态，不验证网站代理链路。`--web-root` 必须对应 Nginx 已配置的 `root`。项目路径自动识别，支持 Ubuntu、RHEL 等已完成原生部署的 Linux；使用 nvm 时先加载当前用户的 Node.js 22.12+ 环境。

进度输出到控制台和项目 `log/deploy-YYYY-MM-DD.log`，同一天追加。并发运行会拒绝第二次发布；存在本地代码改动、未跟踪文件、分支不匹配或本地未推送提交时停止，不自动丢弃修改。被 Git 忽略的真实配置和日志不会阻止发布。

脚本保留 `config/app.config`、本机 systemd 覆盖配置、Nginx 配置及旧哈希资源；先发布资源，最后原子替换 `index.html`。此流程**不是零停机发布，也没有自动回滚**：后端安装依赖期间暂停服务；任一步骤失败会停止后续操作，代码或依赖可能已更新、API 可能仍处于停止状态。修复日志中指出的问题后重新运行；不要仅重启一个依赖安装不完整的服务。详细失败阶段和处理方式见 [Linux 自动发布说明](docs/linux-deployment.md#自动更新发布)。

执行成功后刷新浏览器，必要时 `Ctrl+Shift+R`。数据库预检检查行业动量表的读取，健康接口检查 API 存活；新增模块的数据表和数据仍需先由聚宽采集程序生成，并授予网页账号读取权限。脚本不会自动执行采集、建表或数据库迁移，也不会修改系统服务模板、Nginx 配置、端口、防火墙或 SELinux 策略；这些配置变更按下表单独操作。

#### 仅更新前端

修改总览卡片、图片、文案、样式等纯前端内容时，可以使用简化命令：

```bash
(
  set -e
  cd /home/ubuntu/showAllOpts
  git pull --ff-only
  bash scripts/build.sh
  sudo rsync -rlt --chmod=D755,F644 frontend/dist/ /var/www/showallopts/
)
```

仅当此次拉取的全部变更都属于前端时使用；如果还包含后端修改，执行上面的完整更新流程。**仅重启后端不会更新网页；仅拉取代码或构建前端，也不会让正在运行的后端加载新接口。**

#### 按变更类型选择操作

| 变更内容 | 生效操作 |
| --- | --- |
| 网页样式、图片、文案 | 拉取代码 → 构建前端 → 发布前端 → 刷新浏览器 |
| 后端接口或数据库查询逻辑 | 拉取代码 → 同步后端依赖 → 重启 `showallopts-api` |
| `config/app.config` 内容 | 重启 `showallopts-api`，无需 `daemon-reload` |
| 已安装的 systemd 服务或覆盖配置 | `sudo systemctl daemon-reload` → 重启 `showallopts-api` |
| Nginx 配置 | `sudo nginx -t` 通过后执行 `sudo systemctl reload nginx` |
| 仅在聚宽增量采集并写入数据库 | 无需重新部署；等待定时同步或手动同步本地数据，再查看网页 |

仓库内的 service 模板修改后，需重新安装到 systemd 对应位置，并保留本机覆盖配置，再执行 `daemon-reload` 和重启。仅修改仓库文件不会更新已安装的服务。新增模块如果引入新的数据表，还需先运行对应采集程序，并确认网页数据库账号具有新表的读取权限。

#### 发布后排错

```bash
sudo systemctl status showallopts-api --no-pager
sudo journalctl -u showallopts-api -n 80 --no-pager
```

应用日志位于项目下的 `log/app.log` 和 `log/error.log`，按天轮转。页面“数据暂时没有抵达”表示请求失败，不等同于数据库没有数据：

| 返回结果 | 优先排查 |
| --- | --- |
| 404 / Not Found | 后端是否已重启并加载新接口、Nginx 转发路径是否正确 |
| 503 | 数据库连接、新表是否存在、查询账号的表权限 |
| 502 / 504 | 后端是否正常运行、代理连接或请求是否超时 |
| 后端直连正常，网页仍报错 | 浏览器实际请求地址、Nginx 代理及已发布前端是否为最新版本 |

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

## 聚宽采集策略与数据保留

推荐在聚宽使用完整单文件 [`collectors/策略批量采集_数据库版.py`](collectors/策略批量采集_数据库版.py)：依赖统一导入，顶部 `DB_CONFIG` 填写数据库连接、`DB_OPTIONS` 设置连接重试、`STRATEGY_CONFIG` 按策略设置日期/参数/ETF 名单。将全部代码直接粘贴到 Notebook 执行，或只上传该文件后执行 `%run -i 策略批量采集_数据库版.py`。文件内七个独立策略按“行业动量 → 抱团拥挤度 → 微盘温度计 → ETF份额 → 净利润断层 → 微盘新高新低 → 高股息选股”串行调用，不依赖其他策略/配置文件，不使用 `__file__` 或源码字符串动态执行。原六个策略固定只新增：已有日期/采样期跳过，ETF 不回查覆盖，净利润断层只新增本季度未入库股票；行业动量仍清理超过 180 个交易日的数据。控制台默认显示每个策略的进度条、当前日期/批次和耗时，每 5 秒刷新，结束后展示结果、新增数据预览及最终汇总，不生成采集日志文件；可通过 `PROGRESS_ENABLED` 和 `PROGRESS_INTERVAL_SECONDS` 调整。高股息选股按单独约定每次重算、事务替换最新结果。参数和失败重跑见 [批量采集说明](docs/collector-batch.md)。

`collectors/行业动量计算_数据库版.py` 是独立运行于聚宽研究环境的脱敏采集脚本。每次运行先清理整张行业动量表中过期记录，仅保留以运行当天交易日历确定的最近 **180 个交易日**，然后在该范围内增量更新。原有 `START_DATE` 不会导致过期历史被重新补回。

网页后端继续只读数据库；启用采集保留策略后，“近一年”曲线只能显示尚未清理的数据。运行方法、清理范围、事务及失败处理见 [采集策略说明](docs/collector-retention.md)。真实数据库连接只填写在聚宽私有副本中。

`collectors/抱团拥挤度_数据库版.py` 从 **2020-01-01** 采集至每次运行的北京时间当天，保持 `TOP_PCT=0.05`，写入独立的 `market_crowding` 表。默认仅查询最新入库日期，从该日（含）增量更新至当天，支持按日事务；完整保留历史。更早缺口需显式手动补录，不自动扫描。运行方法、表结构、指标口径与服务器升级命令见 [抱团拥挤度说明](docs/market-crowding.md)。

`collectors/微盘温度计_数据库版.py` 保留 **2021-01-01 起、每 5 个交易日采样、微盘 1000 只**，写入 `microcap_temperature`。行情和快照增量更新，另读取必要的历史分位基准与上期成分股，保证温度和指数连续；完整保留历史。指标、运行与升级说明见 [微盘温度计说明](docs/microcap-temperature.md)。

## ETF 份额采集与仪表盘

`collectors/ETF份额_数据库版.py` 首次采集截至运行当天 **最近 300 个交易日** 的可用份额，后续按每只 ETF 的最新日期增量更新并回查最近 5 个交易日，保留已入库历史。默认跟踪沪深300、中证500各 5 只 ETF，写入 `etf_share_daily`。沪深300固定跟踪按 2026-10-16 规模排名选定的华泰柏瑞、易方达、华夏、嘉实、天弘；升级后正常运行脚本即可为无记录的新增基金补采最近 300 个交易日，网页后端需重启以加载新清单。

页面展示最新份额、日变动和历史曲线，可切换份额变化率、聚焦单只基金。运行前的数据接口检查、连接配置、缺失处理、只读账号权限和 Linux 升级步骤见 [ETF 份额仪表盘说明](docs/etf-shares.md)。

## 高股息选股

`collectors/高股息选股_数据库版.py` 支持两组各最多 25 只：纯高股息沿用三年累计分红、PEG 和盈利增长规则，再按三年分红比例降序；高股息低估值要求近一年股息率 > 4%、0 < PE < 8，按一年股息率降序。已加入批量采集，显示采集进度。

每次重算最近完整交易日，只保留两组最新结果，事务失败保留旧名单；完全相同的结果不重复写入。网页新增双策略切换、排名列表、搜索和共同入选筛选。口径、写入规则及部署见 [高股息选股说明](docs/dividend-selection.md)。

## 微盘新高新低

`collectors/微盘新高新低数量统计_数据库版.py` 每天按原程序规则选择微盘 400 只股票，统计 5/10/20/30 日新高新低数量，写入 `microcap_breadth`。首次默认近 180 个自然日，之后从最新入库日之后增量采集，只新增、保留历史；批量采集已加入该策略。完整单文件，无外部策略或配置文件依赖。

网页新增“微盘新高新低”模块，展示四周期数量/比例、趋势、加权强度、新高占优持续性与最近记录。口径、参数、表结构及更新部署步骤见 [微盘新高新低说明](docs/microcap-breadth.md)。

## 净利润断层筛选与展示

`collectors/净利润断层_数据库版.py` 在聚宽研究环境扫描统一最新已结束季度的全部 A 股披露数据，筛选归母净利润同比达到 20%、公告对应交易日高开超过 3% 且收盘不低于开盘的股票。不下单，不限制入选数量。每次完整重查目标季度，一次事务替换该季度结果，保留其他季度。

页面提供代码/名称搜索、细分行业/季度/公告类型筛选、排序和分页，展示市值、PE、PB、利润增幅与信号日期。点击股票可核查快报、预告、正式财报原始数据及信号附近 K 线。详细口径、数据单位、首次运行和 Linux 更新步骤见 [净利润断层说明](docs/earnings-gap.md)。

## 页面与数据口径

- 总览：七个研究模块并列展示，支持关键词搜索、分类筛选和键盘跳转；首页不读取行情接口，不受数据库连接状态影响。图形为明确标注的视觉示意，不展示虚构实时行情。
- 全站采用米白、朱红、暖金与青绿的亮色风格，导航、热力图、折线图和手机端统一配色，并尊重系统减少动态效果设置。
- 微盘温度计：综合温度与四类分位、微盘 1000 等权指数、PE/PB 和归母净利润增速；采样频率为 5 个交易日，页面区间切换不改变历史分位。
- 抱团拥挤度：最新成交额占比、区间统计、原始曲线和可选 EMA 10 平滑线；支持近一年等区间与全部历史，所有统计使用原始值。
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
| `GET /api/v1/health` | 服务存活状态，不代表数据库连通或缓存已就绪 |
| `GET /api/v1/cache/status` | 本地快照的同步状态、更新时间与每日计划 |
| `POST /api/v1/cache/refresh` | 手动同步一个或全部模块，要求 Bearer 同步密钥 |
| `GET /api/v1/modules` | 模块目录 |
| `GET /api/v1/earnings-gap/stocks` | 净利润断层名单，支持季度/行业/公告类型/搜索/排序/分页 |
| `GET /api/v1/earnings-gap/stocks/{code}?period=2026-09-30` | 股票财务、公告及价格详情 |
| `GET /api/v1/etf-shares/history?group=hs300&days=365` | ETF 最新份额与区间曲线；支持 hs300 / zz500，days=0 查看全部 |
| `GET /api/v1/microcap-temperature/history?days=365` | 微盘温度计最新指标、历史曲线及区间统计；days=0 查看全部 |
| `GET /api/v1/market-crowding/history?days=365` | 抱团拥挤度最新值、历史及区间统计；days=0 查看全部 |
| `GET /api/v1/industry-momentum/latest` | 最新完整读取的日期快照及前一个有记录日期的差值 |
| `GET /api/v1/industry-momentum/industries/{code}/history?days=365&end_date=2026-10-16` | 单行业历史及区间统计 |

行业动量接口支持 `industry_type=sw_l1/sw_l2/sw_l3`，网页当前固定使用 `sw_l2`。数据库连接失败返回 503，未知行业返回 404，非法参数返回 422；响应不泄露连接信息。

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
