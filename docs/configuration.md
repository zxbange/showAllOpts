# 配置管理

## 配置位置和读取规则

Linux 生产配置放在**项目根目录下的 `config/app.config`**。例如代码部署在 `/home/ubuntu/showAllOpts`，配置就是 `/home/ubuntu/showAllOpts/config/app.config`。文件使用 UTF-8 的 dotenv 格式（`KEY=VALUE`），不是 INI 或 JSON。

```text
showAllOpts/
├── config/
│   ├── app.config.example   # 配置模板，提交 Git
│   └── app.config           # 真实生产配置，不提交 Git
└── backend/
    ├── .env.example         # 开发配置模板
    └── .env                 # 本地开发配置，不提交 Git
```

systemd 模板通过 `APP_ENV_FILE=config/app.config` 选择生产配置；Docker Compose 默认读取宿主机的 `config/app.config`。Linux `bash start.sh` 也会自动选择该文件；直接运行 Python 或 Windows 启动器且未设置变量时仍使用开发配置 `backend/.env`。

进程环境变量优先于选中的配置文件，未提供的字段使用程序默认值。选中生产配置时不与开发配置合并。相对路径以项目根目录为基准，不受终端工作目录或 systemd 工作目录影响。显式指定的文件不存在、不可读或路径为空时启动失败。

配置项见 [config/app.config.example](../config/app.config.example)。`APP_ENV_FILE` 设置在启动进程的环境中，不要写进 `app.config` 内部。配置修改后需重启。

## Linux 生产配置和现有配置迁移

以下命令使用现有 Ubuntu 环境：代码在 `/home/ubuntu/showAllOpts`，运行账号和组为 `ubuntu`，且依赖已安装。无需另建服务账号。先准备生产配置，再按 [Linux 部署指南](linux-deployment.md#api-服务) 安装基础 service 及 `99-ubuntu-home.conf` 覆盖配置。修改配置权限前先停止开发启动器；已有后台服务时执行 `sudo systemctl stop showallopts-api`。

```bash
cd /home/ubuntu/showAllOpts
sudo install -d -o ubuntu -g ubuntu -m 0700 /home/ubuntu/showAllOpts/config

# 仅在目标不存在时初始化；已有生产配置不覆盖。
if ! sudo test -e config/app.config; then
    if [ -f backend/.env ]; then
        sudo install -o ubuntu -g ubuntu -m 0600 backend/.env config/app.config
    else
        sudo install -o ubuntu -g ubuntu -m 0600 config/app.config.example config/app.config
    fi
fi
sudo chown ubuntu:ubuntu config/app.config
sudo chmod 0600 config/app.config
sudoedit /home/ubuntu/showAllOpts/config/app.config

# 使用服务账号验证文件权限、配置解析及数据库连接。
sudo -u ubuntu env APP_ENV_FILE=config/app.config \
    backend/.venv/bin/python scripts/check_database.py
```

配置目录由 `ubuntu` 管理，权限为 `700`；真实配置权限为 `600`，仅属主可读写（系统管理员仍可通过 sudo 管理）。后台服务也使用 `ubuntu`，能够读取该文件，不需要开放给其他用户。依赖安装以 `ubuntu` 执行，不要用 sudo 安装项目依赖。

如果此前已经使用其他位置的生产配置，请将其内容迁入 `config/app.config`，确认新配置可用后再重启；不要用开发配置覆盖已有生产参数。应用不会自动删除旧文件。

基础 service 和 Ubuntu 本机覆盖配置均包含：

```ini
Environment=APP_ENV_FILE=config/app.config
```

应用将其解析为 `/home/ubuntu/showAllOpts/config/app.config`，由 Python 读取 dotenv 内容，systemd 不使用 `EnvironmentFile`。部署到 `/home` 时必须安装 `99-ubuntu-home.conf`，覆盖运行账号、工作目录、启动命令、`ProtectHome=read-only` 和日志写入目录；只安装基础模板仍会使用旧的 `/opt` 路径。首次迁移或修改 service/覆盖配置后执行 `daemon-reload` 并重启；仅修改 `app.config` 内容时只需重启服务。终端中临时 export 的变量不会改变 systemd 服务环境。

发布和更新时保留 `config/app.config`，不要用模板覆盖它，也不要随代码目录清理而删除它。Git 已忽略 `config/` 下除模板外的文件；Git 忽略规则不会保护文件免受人工删除或部署脚本覆盖。

## 手动启动与安装

Windows 本地开发继续使用 `backend/.env`。Linux 在项目根目录直接执行 `bash start.sh` 就会读取 `config/app.config`；也可显式指定：

```bash
export APP_ENV_FILE=config/app.config
bash start.sh
```

Windows 也支持同一配置路径：

```powershell
$env:APP_ENV_FILE = 'config/app.config'
.\start.cmd
```

设置 `APP_ENV_FILE` 后运行 setup，只校验选中的配置，不覆盖或生成配置副本；指定文件必须已准备好且执行账号可读。未设置时，Linux setup 保留已有 `config/app.config`；缺失时优先从已有 `backend/.env` 迁移，其次提取旧采集配置，否则复制生产模板。Windows setup 继续保留或初始化 `backend/.env`。正式 systemd 服务始终按 service 内的变量选择 `config/app.config`。

Windows 恢复开发配置可执行 `Remove-Item Env:APP_ENV_FILE`；Linux 若需使用开发配置，执行 `APP_ENV_FILE=backend/.env bash start.sh`。

## Docker Compose

准备好 `config/app.config` 后，在项目根目录执行：

```bash
docker compose up --build -d
```

Compose 在宿主机读取文件，将键值注入后端容器环境。`config/` 位于前后端 Docker 构建上下文之外，不打包进镜像；容器不需要该文件的宿主机路径。不要在 `app.config` 中定义 `APP_ENV_FILE`。Compose 设置的 `LOG_DIR=/app/log` 优先于文件值，配合现有日志挂载使用。

执行 Compose 的账号须有配置文件读取权限和 Docker 使用权限。按本页权限设置，`ubuntu` 可读取配置；若 Docker 操作需要 sudo，可使用 `sudo docker compose up --build -d`。

自定义配置路径仍可通过 `APP_ENV_FILE` 指定；旧 Docker 部署若暂时继续使用开发配置，可执行 `APP_ENV_FILE=./backend/.env docker compose up --build -d`。

配置修改后执行 `docker compose up -d --force-recreate backend`，保持同一配置路径与执行账号；仅执行 `restart` 不会重新注入环境变量。不要分享展开后的 Compose 配置或容器环境，其中可能包含凭据。


## 本地缓存配置

默认使用磁盘快照，`CACHE_ENABLED=true`、`CACHE_AUTO_REFRESH=true`、`CACHE_REFRESH_TIME=07:00`（北京时间）。快照目录固定为当前 `LOG_DIR` 下的 `cache/`，可复用现有目录权限和持久化挂载。网页可直接选择全部模块或指定模块同步，无需密钥；旧的 `CACHE_REFRESH_TOKEN` 配置已不再使用。CLI 仍须使用与服务相同的配置和运行账号。后台同步读取超时 `CACHE_DB_READ_TIMEOUT=600`（秒），网络失败总尝试次数 `CACHE_SYNC_ATTEMPTS=3`；这两个默认值无需在旧配置中补填即可生效。同步时间可在页面直接保存，无需重启；页面保存优先，旧默认20:00自动迁移为07:00，其他自定义配置保留，详见[本地缓存时间设置](local-cache.md)。其他配置修改后重启后端。

两融担保比例使用独立的中证官方采集任务，`MARGIN_WORKER_ENABLED=true` 为默认值，旧配置无需补填。每天北京时间20:00检查（约30秒调度间隔），不受 `CACHE_REFRESH_TIME` 影响；设为 `false` 只关闭该模块定时任务，仍可手动更新。本地数据与续传清单位于 `LOG_DIR/market-data/margin-guarantee/`，需持久化保留。启动不立即补跑，首次可在“两融担保比例”页面点击“更新数据”。

修改这些配置后重启后端。缓存开关和命令用法见[本地缓存说明](local-cache.md)，真实配置仍按本文规则排除 GitHub。
