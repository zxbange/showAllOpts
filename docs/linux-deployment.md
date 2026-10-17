# Linux 运行与部署

开发时使用 `start.sh` 同时启动前后端；长期运行使用 systemd 管理 Python API、Nginx 提供前端静态页面。数据库继续使用现有 MySQL，应用只读取行业动量、抱团拥挤度及微盘温度计数据。

本文默认沿用现有 `/home/ubuntu/showAllOpts` 项目目录和 `ubuntu` 账号，与 [README systemctl 管理指引](../README.md#linux-后台服务systemctl-管理) 一致。RHEL 或其他账号部署时，请统一替换目录、用户和组，并核对目标系统权限与 SELinux 设置。

## 1. 系统环境

建议使用 Ubuntu 24.04 LTS 或 RHEL 9.2+。需要 Bash、Python 3.11+、Node.js 22.12+ 和 npm。以下软件包命令按这两个系统编写；其他版本需要选择对应的软件包。

Ubuntu 24.04：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx rsync curl
```

RHEL 9.2+（需要已启用系统软件源）：

```bash
sudo dnf install -y python3.11 python3.11-pip nginx rsync curl
```

Node.js 需要 22.12 或更高版本。可以使用 [nvm 官方安装方式](https://github.com/nvm-sh/nvm#installing-and-updating)；在 Bash 中以运行项目的同一个部署用户执行以下命令，不要加 `sudo`：

```bash
export NVM_DIR="$HOME/.nvm"
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.8/install.sh | bash
source "$NVM_DIR/nvm.sh"
nvm install 22
nvm use 22
nvm alias default 22
```

已有 nvm 环境时，只需加载其 `nvm.sh` 并执行 `nvm use 22`。不要假设系统默认 nodejs 软件包满足要求；确认当前用户可以调用 `node` 和 `npm`。`setup.sh` 提示 `Install Node.js 22.12+ and npm first.` 时，请按 [README 的 Node.js 排查步骤](../README.md#setupsh-提示缺少-nodejs--npm) 处理。

```bash
node --version
npm --version
```

Ubuntu 24.04 的 `python3-venv` 对应 Python 3.12；RHEL 9.2 起提供 Python 3.11。参考 [Ubuntu 软件包](https://packages.ubuntu.com/noble/python3-venv) 和 [Red Hat Python 文档](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/installing_and_using_dynamic_programming_languages/index)。Ubuntu 22.04 默认 Python 3.10 不满足要求，需要另行准备 Python 3.11+ 或采用容器部署。不要替换 RHEL 系统 Python。

## 2. 复制代码并安装依赖

复制项目时排除 `backend/.venv`、`frontend/node_modules`、`frontend/dist`、`.runtime`。Windows 虚拟环境和原生 Node 依赖不能在 Linux 上直接复用。虚拟环境应在最终目录创建，安装后不要再移动。

以 `ubuntu` 用户进入 `/home/ubuntu/showAllOpts`，按实际系统选择一条安装命令：

```bash
cd /home/ubuntu/showAllOpts
# Ubuntu 24.04
bash scripts/setup.sh --python python3

# RHEL 9.2+
bash scripts/setup.sh --python python3.11
```

脚本检查版本，创建 `backend/.venv`、安装锁定依赖，执行 `npm ci` 并构建前端。指定 `APP_ENV_FILE` 时只校验该文件；否则保留已有 `config/app.config`。若生产配置缺失，优先从已有 `backend/.env` 迁移，其次提取同级旧采集程序的连接常量，否则复制 `config/app.config.example` 模板。新建配置权限为 `600`，已有配置不会被覆盖。

编辑 `config/app.config`，填写 Linux 服务器可访问的 MySQL 地址、库名和账号。若设置了 `APP_ENV_FILE`，编辑其指定文件。数据库在另一台机器时，`DB_HOST` 应为实际数据库地址。完成后只读检查连通性：

```bash
APP_ENV_FILE=config/app.config backend/.venv/bin/python scripts/check_database.py
```

脚本使用 `bash 文件名` 执行，无需依赖压缩包是否保留可执行权限。`.gitattributes` 已固定 shell 脚本为 LF 换行。

## 3. 开发启动和停止

```bash
bash start.sh
```

`start.sh` 默认选择 `config/app.config`，也支持显式 `APP_ENV_FILE` 覆盖；启动日志会显示所选配置路径。默认监听 `0.0.0.0:5173`，其他机器直接访问 `http://服务器IP:5173`，本机仍可使用 `http://127.0.0.1:5173`。如果需要限制为仅本机访问：

```bash
bash start.sh --host 127.0.0.1
```

访问 `http://服务器IP:5173`，必要时在防火墙和云安全组放行 5173。API 绑定本机 8000，通过前端 `/api` 代理访问，无需对外开放 8000。

`Ctrl+C` 停止两项服务；启动器接收 `SIGTERM` 时也会清理子进程。开发模式依赖终端；长期运行使用下一节。

## 4. systemd + Nginx 后台服务

部署约定：代码 `/home/ubuntu/showAllOpts`，运行账号和组 `ubuntu`，网页发布到 `/var/www/showallopts`。保留 `showAllOpts` 的大小写。使用现有用户与项目，无需迁移代码或重建未移动的虚拟环境。

仓库基础 service 保留独立账号及 `/opt` 部署参数；本节必须同时安装 `99-ubuntu-home.conf` 覆盖配置，使最终服务按上述目录和账号运行。变更部署目录时，同步修改覆盖配置中的 `WorkingDirectory`、`ExecStart`、`ReadWritePaths`；更换用户时同步修改 `User`、`Group` 和目录权限。

### 部署目录和权限

先以 `ubuntu` 在现有项目目录按第 2 节安装依赖。生产配置为 `/home/ubuntu/showAllOpts/config/app.config`，按 [生产配置初始化或迁移](configuration.md#linux-生产配置和现有配置迁移) 保留或创建配置，设置 `ubuntu` 所有权、配置目录 `700` 和真实文件 `600` 权限，并以该账号检查数据库连通性。

部署前在原终端按 `Ctrl+C` 停止 `bash start.sh`；已有后台服务时执行 `sudo systemctl stop showallopts-api`。避免开发启动器与后台服务同时占用 8000 端口。

### 日志目录与服务升级

应用按天轮转 `log/app.log` 和 `log/error.log`，默认各保留 30 份历史日文件。格式和参数见 [README 运行日志](../README.md#运行日志按天轮转)。systemd 使用文件系统保护，需要在启动前准备可写目录。以下命令必须在相关进程停止后执行，旧日志和锁文件也统一归 `ubuntu`：

```bash
sudo install -d -o ubuntu -g ubuntu -m 0750 /home/ubuntu/showAllOpts/log
sudo chown -R ubuntu:ubuntu /home/ubuntu/showAllOpts/log
```

修改 `LOG_DIR` 时，同步修改覆盖配置中的 `ReadWritePaths` 并准备新目录权限。升级已有服务时，先安装所需依赖，再按下一节重新安装基础模板与覆盖配置、执行 `daemon-reload` 和重启。不要删除现有 `config/app.config` 或日志文件。

### API 服务

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

### Nginx 网页和反向代理

```bash
cd /home/ubuntu/showAllOpts
sudo install -d -m 0755 /var/www/showallopts
sudo rsync -rlt --chmod=D755,F644 frontend/dist/ /var/www/showallopts/
sudo install -m 0644 deploy/nginx/showallopts.conf /etc/nginx/conf.d/showallopts.conf
```

编辑 `/etc/nginx/conf.d/showallopts.conf`，把 `server_name showallopts.example.com` 改为实际域名或服务器 IP。域名需要解析至服务器；默认端口 80。同机已有网站时保留其配置，确保域名不重复。只发布 `frontend/dist`，不要将项目源码目录作为静态目录。

RHEL 开启 SELinux 时：

```bash
sudo setsebool -P httpd_can_network_connect 1
sudo restorecon -Rv /var/www/showallopts
```

这符合 [Red Hat Nginx 反向代理配置](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/deploying_web_servers_and_reverse_proxies/index)，无需关闭 SELinux。

```bash
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

按实际防火墙放行 HTTP，以下选用一种：

```bash
# Ubuntu 使用 UFW 时
sudo ufw allow 80/tcp

# RHEL 使用 firewalld 时
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --reload
```

访问 `http://实际域名或服务器IP`。健康检查只判断 API 存活；打开行业动量页面或访问 `/api/v1/industry-momentum/latest` 进一步验证数据库。项目没有登录功能；如公开到互联网，在 Nginx 层配置 HTTPS 和访问认证。

### 日志、启停和更新

```bash
sudo systemctl start showallopts-api
sudo systemctl status showallopts-api --no-pager
sudo journalctl -u showallopts-api -n 100 --no-pager
sudo journalctl -u showallopts-api -f
sudo systemctl restart showallopts-api
sudo systemctl stop showallopts-api
# 停止并取消开机启动
sudo systemctl disable --now showallopts-api
```

停止 API 不影响同机其他 Nginx 网站；本网站数据请求会返回代理错误。如需停止网页访问，移除该站点配置，再执行 `nginx -t` 和 `systemctl reload nginx`。

已部署服务器的完整更新命令、纯前端发布命令和排错方法见 [README：修改配置或更新代码](../README.md#修改配置或更新代码)。

更新代码时保留 `config/app.config`，不要用模板覆盖真实配置，也不要随发布清理 `config/`。更新代码后，以 `ubuntu` 在 `/home/ubuntu/showAllOpts` 重新运行 setup（Python 参数按系统选择），发布新的 `frontend/dist/`，重启 API。保留本机 `99-ubuntu-home.conf`；更新基础 service 或覆盖配置后需要 `daemon-reload`，仅修改数据库配置时重启即可。只有前端修改时，可运行 `bash scripts/build.sh` 再发布。rsync 命令保留旧哈希资源，供已打开的页面继续读取；可在维护窗口按发布版本清理旧资源。

## 5. Docker Compose

已安装 Docker Engine 和 Compose 插件时，宿主机无需另装 Python、Node.js 或 Nginx。默认配置好 `config/app.config` 后执行下列命令；也可通过 `APP_ENV_FILE` 指定其他文件，权限与配置更新方法见 [配置管理的 Docker 说明](configuration.md#docker-compose)：

```bash
docker compose up --build -d
docker compose logs -f
docker compose down
```

后端应用日志通过 `./log:/app/log` 映射到宿主机项目目录。首次启动会创建目录；容器入口准备目录权限后切换到 UID 10001 的 appuser 运行 API，容器重建会保留日志。Linux 宿主机查看日志可能需要 `sudo tail -f log/app.log`。原生部署和 Docker 应使用各自的部署目录，以免不同账号的日志文件权限冲突。Nginx 的日志通过 `docker compose logs frontend` 查看。

默认监听所有 IPv4 网卡，其他机器访问 `http://服务器IP:8080`；本机也可使用 `http://127.0.0.1:8080`。如需修改地址或端口，在项目根目录新建 `.env`（与 `backend/.env` 分开；仅本机访问时将 `WEB_BIND_ADDRESS` 改为 `127.0.0.1`）：

```dotenv
WEB_BIND_ADDRESS=0.0.0.0
WEB_PORT=8080
```

再次执行 `docker compose up --build -d`，访问 `http://服务器IP:8080`。容器内 `DB_HOST=127.0.0.1` 指容器自身；数据库在宿主机时应使用容器可达的宿主机地址。容器不会新建 MySQL 或迁移现有表。

## 6. 验证和排错

```bash
cd backend
.venv/bin/python -m pytest -q
cd ../frontend
npm test
npm run build
```

- 服务仍显示旧 `/opt` 路径或 `showallopts` 账号：确认安装了 `99-ubuntu-home.conf` 并执行 `daemon-reload`、重启，使用 `systemctl show` 检查最终配置。
- 服务无法读取 `/home`：检查最终 `ProtectHome` 为 `read-only`，并核对覆盖配置已生效。
- `Permission denied`：检查目录遍历、虚拟环境执行、配置读取权限；RHEL 同时检查 SELinux 审计日志。
- 虚拟环境失败：可能复制了 Windows 环境或移动了 Linux 环境；将旧环境移出项目后，在最终目录重新安装。
- `$'\r': command not found`：脚本被改成 CRLF，恢复 UTF-8 / LF。
- Nginx 欢迎页：访问地址与 `server_name` 不匹配，或新配置没有加载。
- Nginx 502：检查 API 服务和 SELinux 反向代理设置。
- API 503：检查数据库地址、数据库账号允许连接的来源主机、网络和表权限。
- 刷新行业详情 404：确认保留 SPA 的 `try_files` 配置。

本次开发环境为 Windows；Ubuntu/RHEL 上的 systemd、Nginx、SELinux 和容器运行仍需在目标服务器验证。

## 抱团拥挤度模块升级

新模块需先在聚宽运行独立采集程序，创建并写入 `market_crowding` 表；网页账号需有该表 SELECT 权限。已有 systemd + Nginx 部署只需更新代码、构建并发布前端、重启 API，保留当前配置及访问端口。操作命令见 [抱团拥挤度部署说明](market-crowding.md#linux-已部署项目升级)。

## 微盘温度计模块升级

先在聚宽运行独立采集脚本，创建并写入 `microcap_temperature` 表，并给网页账号增加 SELECT 权限。然后更新代码、构建发布前端并重启 API；原配置、代理与端口不变。详见 [微盘温度计部署说明](microcap-temperature.md#已有-linux-部署升级)。

## ETF 份额模块升级

已有部署接入 ETF 仪表盘时，先在聚宽创建并填充 `etf_share_daily`，给网页账号授予该表 SELECT 权限，再更新代码、构建并发布 `frontend/dist/`、重启 `showallopts-api`。无需更换 Nginx 端口、systemd 服务或真实数据库配置。完整命令与字段口径见 [ETF 份额仪表盘](etf-shares.md#已有部署升级)。
