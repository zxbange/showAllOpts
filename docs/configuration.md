# 配置管理

## 配置位置和读取规则

Linux 生产配置放在**项目根目录下的 `config/app.config`**。例如代码部署在 `/opt/showallopts`，配置就是 `/opt/showallopts/config/app.config`。文件使用 UTF-8 的 dotenv 格式（`KEY=VALUE`），不是 INI 或 JSON。

```text
showAllOpts/
├── config/
│   ├── app.config.example   # 配置模板，提交 Git
│   └── app.config           # 真实生产配置，不提交 Git
└── backend/
    ├── .env.example         # 开发配置模板
    └── .env                 # 本地开发配置，不提交 Git
```

systemd 模板通过 `APP_ENV_FILE=config/app.config` 选择生产配置；Docker Compose 默认读取宿主机的 `config/app.config`。手动启动时需设置同一个变量；未设置变量的 Python 启动仍使用开发配置 `backend/.env`。

进程环境变量优先于选中的配置文件，未提供的字段使用程序默认值。选中生产配置时不与开发配置合并。相对路径以项目根目录为基准，不受终端工作目录或 systemd 工作目录影响。显式指定的文件不存在、不可读或路径为空时启动失败。

配置项见 [config/app.config.example](../config/app.config.example)。`APP_ENV_FILE` 设置在启动进程的环境中，不要写进 `app.config` 内部。配置修改后需重启。

## Linux 生产配置和现有配置迁移

以下命令假设代码在 `/opt/showallopts`，服务账号和组均为 `showallopts`，且依赖已安装。先按 [Linux 部署指南](linux-deployment.md) 创建服务账号、安装依赖，再准备生产配置，最后安装或重启新版 systemd 服务。

```bash
cd /opt/showallopts
sudo install -d -o root -g showallopts -m 0750 /opt/showallopts/config

# 仅在目标不存在时初始化；已有生产配置不覆盖。
if ! sudo test -e config/app.config; then
    if [ -f backend/.env ]; then
        sudo install -o root -g showallopts -m 0640 backend/.env config/app.config
    else
        sudo install -o root -g showallopts -m 0640 config/app.config.example config/app.config
    fi
fi
sudo chown root:showallopts config/app.config
sudo chmod 0640 config/app.config
sudoedit /opt/showallopts/config/app.config

# 使用服务账号验证文件权限、配置解析及数据库连接。
sudo -u showallopts env APP_ENV_FILE=config/app.config \
    backend/.venv/bin/python scripts/check_database.py
```

文件由 root 维护，服务组只读。普通部署用户不属于该组时，使用上面的服务账号命令验证配置；安装依赖仍可用普通部署账号，不需要放宽生产配置权限。

如果此前已经使用其他位置的生产配置，请将其内容迁入 `config/app.config`，确认新配置可用后再重启；不要用开发配置覆盖已有生产参数。应用不会自动删除旧文件。

systemd 模板包含：

```ini
Environment=APP_ENV_FILE=config/app.config
```

应用将其解析为项目根目录下的文件，由 Python 读取 dotenv 内容。systemd 不使用 `EnvironmentFile`，避免 JSON 和引号解析差异。首次迁移需重新安装 service、执行 `daemon-reload` 并重启；仅修改 `app.config` 内容时只需重启服务。

发布和更新时保留 `config/app.config`，不要用模板覆盖它，也不要随代码目录清理而删除它。Git 已忽略 `config/` 下除模板外的文件；Git 忽略规则不会保护文件免受人工删除或部署脚本覆盖。

## 手动启动与安装

本地开发继续使用 `backend/.env`。在项目根目录，使用生产配置手动启动：

```bash
export APP_ENV_FILE=config/app.config
bash start.sh
```

Windows 也支持同一配置路径：

```powershell
$env:APP_ENV_FILE = 'config/app.config'
.\start.cmd
```

设置 `APP_ENV_FILE` 后运行 setup，只校验选中的配置，不覆盖或生成配置副本；指定文件必须已准备好且执行账号可读。未设置时，setup 保留或初始化 `backend/.env`，不改动生产配置。正式 systemd 服务始终按 service 内的变量选择 `config/app.config`。

恢复默认开发配置：PowerShell 执行 `Remove-Item Env:APP_ENV_FILE`；Bash 执行 `unset APP_ENV_FILE`，然后重新启动。

## Docker Compose

准备好 `config/app.config` 后，在项目根目录执行：

```bash
docker compose up --build -d
```

Compose 在宿主机读取文件，将键值注入后端容器环境。`config/` 位于前后端 Docker 构建上下文之外，不打包进镜像；容器不需要该文件的宿主机路径。不要在 `app.config` 中定义 `APP_ENV_FILE`。Compose 设置的 `LOG_DIR=/app/log` 优先于文件值，配合现有日志挂载使用。

执行 Compose 的账号须有文件读取权限。如果文件按上面的 root 权限管理，可由有权限的部署账号执行，或使用 `sudo docker compose up --build -d`。

自定义配置路径仍可通过 `APP_ENV_FILE` 指定；旧 Docker 部署若暂时继续使用开发配置，可执行 `APP_ENV_FILE=./backend/.env docker compose up --build -d`。

配置修改后执行 `docker compose up -d --force-recreate backend`，保持同一配置路径与执行账号；仅执行 `restart` 不会重新注入环境变量。不要分享展开后的 Compose 配置或容器环境，其中可能包含凭据。
