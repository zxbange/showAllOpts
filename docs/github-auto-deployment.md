# main 自动部署（默认关闭）

目标固定为 `ubuntu@119.28.106.127:22`、`/home/ubuntu/showAllOpts`。
这是**待审核的安装材料，不代表已经登录服务器、设置凭据或部署成功**。
本文命令仅由用户在现有可信密码登录/云控制台中执行；助手不读取或上传用户本机文件。
安装入口、授予 sudo 权限、添加 SSH 公钥、保存 Secret、开启发布均是分别需要确认的持久变更。

## 运行规则

- 任何人推送 `main` 都触发测试；只有该次提交的完整后端 pytest、前端 Vitest、构建和 Playwright 全通过，才可能连接服务器。
- `DEPLOY_ENABLED` 未设置或不等于字符串 `true`：**禁止实际部署**，包括手动运行。首次安装不要设置它。
- `DEPLOY_PREFLIGHT_ENABLED=true` 仅允许手动选择 `check`；它不会开启 push 发布。两开关相互独立。
- 推送和手动发布都使用该次运行的 `github.sha`，不发布“拉取时最新”的未知提交。手动运行必须选择 `main`。
- 同一生产队列不取消正在进行的构建/发布，服务器另有部署锁。`queue: max` 最多等待 100 次运行；超出容量会取消新增等待，不能承诺无限排队。运行入队顺序不保证与 push 顺序相同，旧 SHA 在较新 SHA 已上线后会被快进校验拒绝，不能倒退覆盖。
- SSH 阶段是没有项目 checkout、依赖安装和缓存的独立 runner；只使用 OpenSSH。官方 Actions 按完整 SHA 固定，Token 默认仅 `contents: read`，SSH job 无 Token 权限。

**现有阻塞不能略过：**历史完整测试记录为 1,242 通过、2 跳过、7 失败（5 项 runtime、2 项雪球；此材料没有其完整日志）。没有把它们标成通过、排除或 `continue-on-error`。本地部署单元测试通过不等于应用全套/线上验收通过。必须诊断并修复完整 CI 的实际失败后才能自动发布。

## 一次性准备：先确认，不改安全配置

1. 提供**真实网站 origin**，例如 `https://你的域名` 或 `http://119.28.106.127:实际端口`。不要使用猜测值；它是必填的 `DEPLOY_WEB_URL`，用于验证网站代理、首页和静态资源。
2. 在可信服务器会话确认：项目是独立、干净的 `main` checkout；`origin` 是本仓库；现有应用服务为 `showallopts-api` 且 `User=ubuntu`；API 为 `127.0.0.1:8000`，Nginx 静态根目录为 `/var/www/showallopts`。**必须先审查实际 unit、所有 drop-in、Nginx 配置/includes/load_module 指向的模块及其父目录：均须 root 管理且 ubuntu 不可写，不能通过符号链接指向源码树。**服务所有 `ExecStart/ExecStartPre/ExecStartPost/ExecStop/ExecStopPost/ExecReload` 不得使用 `+`/`!` 提权前缀或以 root 执行用户可写脚本；仅 `User=ubuntu` 不足以证明这一点。未核实则停止安装/启用。配置保持已有 `APP_ENV_FILE` 方式，不迁移或显示生产密码。
3. 确认 `/usr/bin/python3` 是 3.11+（建议 Ubuntu 24.04 的 3.12）、Node 是 22.12+，以及 git、npm、rsync、sudo、systemctl、nginx 已安装。`dirname "$(command -v node)"` 的结果填写为 `NODE_BIN`；允许已安装的 nvm 版本目录，但入口不会 source `.bashrc` 或 nvm 脚本。
4. 用户在服务器自行查看 `sudo -l`。**本方案不新增 `NOPASSWD: ALL`，也不会移除现有权限。**如果 ubuntu 已有广泛免密 sudo，受限 key 不是完整隔离；任何可写 main 的人最终都能让应用/构建代码以 ubuntu 执行，并可能继承其已有 root 能力。收紧旧权限或改专用账号需要另行设计、批准，不能悄悄操作。
5. 通过可信云控制台/已经验证的登录取得主机公钥及指纹，用户自行核对：

   ```bash
   sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   sudo cat /etc/ssh/ssh_host_ed25519_key.pub
   ```

   GitHub 变量 `DEPLOY_KNOWN_HOSTS` 最终应为 `119.28.106.127 ssh-ed25519 公钥内容`。这是**服务器主机公钥**，不是下面的部署登录公钥。不要用未核验的 `ssh-keyscan` 结果替代身份验证；主机密钥变更时发布应失败，核验原因后再更新。

## A. 安装受限入口（单独批准后，由用户执行）

先审核本文件及四份 `deploy/automation/` 材料。采用审核后真实发布的完整提交号，不使用浮动分支。以下是在服务器的**现有密码登录会话**里准备暂存材料，不改变运行中源码：

```bash
REVIEWED_SHA='填写审核通过的真实40位提交号'
BUNDLE="$(mktemp -d)" &&
cd /home/ubuntu/showAllOpts &&
[[ "$REVIEWED_SHA" =~ ^[0-9a-f]{40}$ ]] &&
git fetch origin main &&
git merge-base --is-ancestor "$REVIEWED_SHA" FETCH_HEAD &&
(set -o pipefail; git archive "$REVIEWED_SHA" scripts/deploy.py deploy/automation | tar -x -C "$BUNDLE")
```

阅读暂存内容，核对目标目录为 root 管理且没有符号链接。以下是首次安装命令；**如目标文件已经存在，停止并审核/备份旧版本，不直接覆盖**：

```bash
(
  set -euo pipefail
  for target in /usr/local/libexec/showallopts-ssh /usr/local/libexec/showallopts-admin \
                /usr/local/libexec/showallopts-deploy.py /etc/showallopts-deploy.json; do
    if sudo test -e "$target" || sudo test -L "$target"; then
      echo '目标已存在；停止，先审核并备份旧安装。' >&2; exit 1
    fi
  done
  sudo install -d -o root -g root -m 0755 /usr/local/libexec
  sudo install -o root -g root -m 0755 "$BUNDLE/deploy/automation/showallopts-ssh" /usr/local/libexec/showallopts-ssh
  sudo install -o root -g root -m 0755 "$BUNDLE/deploy/automation/showallopts-admin" /usr/local/libexec/showallopts-admin
  sudo install -o root -g root -m 0644 "$BUNDLE/scripts/deploy.py" /usr/local/libexec/showallopts-deploy.py
  sudo install -o root -g root -m 0644 "$BUNDLE/deploy/automation/config.example.json" /etc/showallopts-deploy.json
  sudoedit /etc/showallopts-deploy.json
)
```

配置仅填 `DEPLOY_WEB_URL` 和 `NODE_BIN`，不要放密码、私钥或数据库配置。入口、配置、冻结部署 runner 及所有父目录必须 root 所有且不可被 group/other 写。自动更新代码不会自动替换这些受信任入口；安全修订需要重新审核安装。

### 静态目录权限（单独批准）

本模式不授予 root 复制/删除任意文件的能力。用户先核对 `/var/www/showallopts` 是此站点的**专用真实目录**、无符号链接、没有其他站点文件/挂载；然后只把这一棵目录交给 ubuntu，不修改 `/var/www` 或整个项目/系统目录的权限：

```bash
sudo chown -R --no-dereference ubuntu:ubuntu /var/www/showallopts
```

目录及文件必须对 Nginx 可读/可遍历；部署产物使用目录 755、文件 644。入口会拒绝仍有其他所有者、不可写或符号链接的静态树。这个受限模式针对没有启用 SELinux 的 Ubuntu；不兼容时失败而不是自动放宽权限。

### 最小 sudo 权限（单独批准）

只允许 helper 的 `check`、`nginx-test`、`api-stop`、`api-restart` 四个字面命令；不能传服务名、路径、额外参数或任意 shell。不要对 `deploy.sh`/仓库脚本授予 root 执行权限。

```bash
(
  set -euo pipefail
  sudo test ! -e /etc/sudoers.d/showallopts-deploy
  sudo test ! -L /etc/sudoers.d/showallopts-deploy
  sudo visudo -cf "$BUNDLE/deploy/automation/sudoers.showallopts"
  sudo install -o root -g root -m 0440 "$BUNDLE/deploy/automation/sudoers.showallopts" /etc/sudoers.d/showallopts-deploy
  sudo visudo -c
  sudo -n /usr/local/libexec/showallopts-admin check
)
```

仅在前一步校验成功后继续。保留原密码会话和登录途径，不改 sshd、防火墙、密码认证或其他 authorized_keys 项。

## B. 专用 SSH key 和 GitHub Secret（分别批准、用户处理）

用户在自己的可信终端生成**仅供此仓库部署的** Ed25519 key，私钥无口令以供无人值守使用，并按敏感凭据保护。不要复用个人主密钥。**此材料不会生成 key；不要把密码/私钥发到聊天、提交 Git、截图或打印到 Actions 日志。**

用户通过现有可信密码会话，把新的**公钥**作为独立一行加入 ubuntu 的 `~/.ssh/authorized_keys`，保留所有旧条目；该行必须有前缀：

```text
restrict,command="/usr/local/libexec/showallopts-ssh" ssh-ed25519 替换为部署公钥 showallopts-actions
```

`.ssh` 为 ubuntu 所有、700；`authorized_keys` 为 ubuntu 所有、600。使用 `restrict` 禁止 PTY、端口/代理/X11 转发和用户 rc；入口只接受 `check` 或 `deploy <40位小写SHA>`，不使用 eval。不会授予普通 SSH shell、scp 或 sftp。不过它允许发布 main 代码，这本身不是对 main 写入者的沙盒。

GitHub 仓库 **Settings → Environments → production**：创建环境，并将可部署分支限制为 `main`。需要审批人时配置 required reviewers（会使每次自动触发等待人工审批）。然后用户直接在 GitHub 保存：

| 位置 | 名称 | 内容 |
| --- | --- | --- |
| production 环境 Secret | `DEPLOY_SSH_KEY` | 专用完整私钥；直接从用户可信位置填写，不经聊天 |
| Repository Actions Variable | `DEPLOY_KNOWN_HOSTS` | 已可信核验的服务器 known_hosts 单行 |
| Repository Actions Variable | `DEPLOY_PREFLIGHT_ENABLED` | 预检授权后才设 `true` |
| Repository Actions Variable | `DEPLOY_ENABLED` | 继续保留缺失/`false`，直到首次生产发布和自动触发得到明确批准 |

Actions → 服务器的 key 与服务器 → GitHub 拉仓库的凭据是两回事。私有仓库若尚不能非交互拉取，需用户另行批准并配置**本仓库只读 deploy key**及 GitHub 主机身份验证；不要借用个人广权限 key 或把 Actions 私钥转存服务器。已有可用只读凭据则无需新增。

## C. 验收后才开自动发布

1. 工作流保持 `DEPLOY_ENABLED=false`，先让完整测试跑通。CI 失败必须修复，不得改成仅跑这份部署测试绕过基线。
2. 获准后设置 `DEPLOY_PREFLIGHT_ENABLED=true`，在 Actions 手动运行本工作流，branch 选 `main`、operation 选 `check`。它检查认证、配置、干净工作区、版本、静态权限、服务和 Nginx；不 fetch/build/restart。Nginx `-t` 可能打开现有日志，不是完全无文件访问。
3. 检查生产已有可恢复代码/依赖/静态备份，安排首次发布窗口；获得实际发布与自动触发授权后，设置 `DEPLOY_ENABLED=true`，手动选择 `deploy`。**开关生效后任何新的 main push 也能发布**，首次验收窗口应暂停其他推送。此次失败会阻止成功结论；恢复后可能 detached HEAD，需按原部署文档人工协调才能再次发布。
4. 验证此次**准确 SHA**、API 新 systemd 进程、网站代理、首页及资源检查均成功后，才报告自动部署已可用。此脚本不回滚数据库/应用数据，不保证断电/SIGKILL 的恢复；不能代替业务验证。

紧急停止新发布：把 `DEPLOY_ENABLED` 改为 `false`（预检也停止则将另一个开关改为 `false`）。不要取消正在激活/回滚的运行，先确认服务器状态。撤销此专用访问应由用户删除对应公钥行并删除 GitHub 专用 Secret；不要删除其他 key 或关闭原密码登录。

## 官方依据与固定版本（2026-10-28 核对）

- [GitHub 工作流安全与最小权限](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub 并发与队列上限](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- [GitHub 环境部署保护](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments)
- [OpenSSH authorized_keys 的 command/restrict](https://man.openbsd.org/sshd.8)、[主机密钥检查](https://man.openbsd.org/ssh_config.5)
- [checkout v7.0.1 完整 SHA](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1)
- [setup-python v7.1.0 完整 SHA](https://github.com/actions/setup-python/commit/6d4fd22b20465ccc0f7354476588e8df7c9ca0e0)
- [setup-node v7.1.0 完整 SHA](https://github.com/actions/setup-node/commit/949feb2413d6458794dcd2491c4babbbce0c15c1)
