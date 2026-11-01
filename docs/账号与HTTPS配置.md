# 全站账号与 IP HTTPS

全站需要登录。管理员可管理用户、同步数据、配置监控通知、扫码登录和运行胜率验证；普通用户只查看数据并修改自己的密码。不开放注册，没有默认账号或默认密码。博主监控与胜率验证不再使用独立管理密钥或一次性配对码。

用户密码保存为 Argon2id 哈希：每个密码使用独立随机盐，64 MiB 内存、3 次迭代、1 路并行。不是可解密密码，也不能保证弱密码永远无法破解；建议使用密码管理器生成独立的长密码。数据库连接密码和第三方 Cookie 需要供后台使用，继续采用原来的加密保存方式，不转成用户密码哈希。

## 迁移顺序

这是首次启用登录的升级。代码部署后会先显示管理员初始化或 HTTPS 提示；不会自动创建账号，也不会将公网 HTTP 默认为安全连接。GitHub 发布不会代替下面的服务器一次性操作。

1. 开放云安全组及主机防火墙的 TCP 80、443，保留 SSH 管理入口；后端 8000 仅监听 `127.0.0.1`。
2. 申请 IP 证书，启用 HTTPS、自动续期。
3. 使用 API 服务相同账号、相同配置创建首个管理员，然后登录网站创建普通用户。

下文针对当前原生 Ubuntu 部署：项目 `/home/ubuntu/showAllOpts`、API 服务 `showallopts-api`、Nginx 静态目录 `/var/www/showallopts`、IP `119.28.106.127`。实际路径或服务账号不同，应相应替换。不要把账号密码粘贴到聊天、命令行参数或提交到 Git。

## IP 证书与 HTTPS

Let’s Encrypt 支持公网 IP 证书，IP 证书有效期 **6 天**；Certbot 的 IP webroot 模式需要 **5.4 或更高版本**。不需要域名，客户端无需导入证书。[官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)

本项目的维护服务保留 `NoNewPrivileges` 隔离设置，使用 root 所有的独立 Python 环境运行 Certbot，避免 snap 启动器与服务隔离的兼容问题。不要将应用的虚拟环境供 root 使用。[Certbot 的虚拟环境安装说明](https://eff-certbot.readthedocs.io/en/stable/install.html#alternative-2-pip)

```bash
sudo apt-get install python3-venv
sudo python3 -m venv /opt/certbot
sudo /opt/certbot/bin/python -m pip install --upgrade 'certbot>=5.4'
sudo /opt/certbot/bin/certbot --version
sudo install -d -m 755 /var/www/letsencrypt/.well-known/acme-challenge
```

已有其他 Certbot 安装时，先确认现有证书和定时任务；不要删除证书目录。以下命令均使用明确的 `/opt/certbot/bin/certbot` 路径。后续定期通过同一 pip 命令更新工具，证书本身由定时任务自动续期。

先为 **80 端口**提供 HTTP-01 验证路径。模板为 `deploy/nginx/showallopts-ip-acme.conf`：如果已有本站 80 端口的 server 块，只把模板中的 `/.well-known/acme-challenge/` location 加进去；如果没有，可以安装该临时站点。不要重复定义同 IP、同端口的 server 块。

```bash
# 仅适用于当前没有本站 80 端口 server 块的情况：
sudo install -m 644 deploy/nginx/showallopts-ip-acme.conf /etc/nginx/sites-available/showallopts-acme
sudo ln -s /etc/nginx/sites-available/showallopts-acme /etc/nginx/sites-enabled/showallopts-acme
sudo nginx -t
sudo systemctl reload nginx
```

确认公网能访问该路径后申请正式证书。以下命令首次会交互要求邮箱和同意服务条款，邮箱由站主填写：

```bash
sudo /opt/certbot/bin/certbot certonly --webroot \
  --webroot-path /var/www/letsencrypt \
  --ip-address 119.28.106.127 \
  --preferred-profile shortlived \
  --cert-name showallopts-ip
```

证书成功生成后，备份本站原 Nginx 配置，将本站配置替换为 `deploy/nginx/showallopts-ip-https.conf`。停用上一步的临时 `showallopts-acme` 站点链接，避免与最终模板的 80 端口块重复；不要删除其他站点配置。最终模板在 443 提供 HTTPS，80 和原 8081 跳转至 HTTPS，保留 80 的证书验证路径。IP 客户端可能不发送 SNI，因此模板将本站设为 443 的默认站点；如已有其他默认 HTTPS 站点，需要先协调配置，不能定义两个 `default_server`。确认 `nginx -t` 通过后才重载。

```bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail https://119.28.106.127/api/v1/health
```

在服务实际使用的私有配置 `config/app.config` 添加：

```dotenv
AUTH_PUBLIC_ORIGIN=https://119.28.106.127
AUTH_ALLOW_HTTP=false
```

Nginx 必须保留 `Host`（含端口）并覆盖 `X-Forwarded-Proto`、`X-Forwarded-For`。Uvicorn 仅信任本机反向代理；不得把 `FORWARDED_ALLOW_IPS` 设置为 `*` 并暴露后端到公网。模板已包含这些代理头。此配置是浏览器到网站的 HTTPS，不改变既有研究数据库连接方式。

## GitHub 自动部署的检查地址

HTTPS 验证通过后，还需把服务器 `/etc/showallopts-deploy.json` 中的 `DEPLOY_WEB_URL` 改为 `https://119.28.106.127`，保留 `NODE_BIN` 原值、文件的 root 所有权和 0644 权限。使用 `sudoedit /etc/showallopts-deploy.json` 修改即可，不要重新运行首次 SSH 安装脚本。

自动部署会拒绝健康检查重定向，因此不能继续使用会跳转的 `http://119.28.106.127:8081`。手动部署也应使用 `bash deploy.sh --web-url https://119.28.106.127`。只在 HTTPS 已经可访问、证书验证通过后切换检查地址。

## 自动续期与自动加载修复

无需等到过期：每小时检查一次，由 Certbot 判断是否进入续期窗口。已过期时也会尝试续期。没有使用 `--force-renewal`，不会每小时重复申请证书。网络临时失败后，下次检查自动重试。

维护程序对比磁盘证书与本机 Nginx 实际提供的证书；若没有加载最新证书，先检查 Nginx 配置，再重载并验证证书链、IP 身份及新证书。已经一致时不重载。不会关闭证书验证、重启整台服务器或自动修改防火墙。

```bash
sudo install -d -m 755 /usr/local/libexec
sudo install -o root -g root -m 755 deploy/certificate_maintenance.py /usr/local/libexec/showallopts-certificate-maintenance
sudo install -m 644 deploy/systemd/showallopts-certificate.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/showallopts-certificate.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now showallopts-certificate.timer
sudo systemctl start showallopts-certificate.service
sudo systemctl list-timers showallopts-certificate.timer
sudo journalctl -u showallopts-certificate.service -n 30 --no-pager
sudo /opt/certbot/bin/certbot renew --cert-name showallopts-ip --dry-run
```

维护脚本安装为 root 所有的独立副本，systemd 不会以 root 执行可被部署账号改写的项目文件。后续若修改维护脚本，需重新执行 `install` 更新该副本。Certbot 自带定时续期可以保留；本项目任务还负责检查 Nginx 是否加载新证书。

如果证书已经过期，可手动立即触发同一自动修复流程：

```bash
sudo systemctl start showallopts-certificate.service
sudo journalctl -u showallopts-certificate.service -n 50 --no-pager
```

客户端无需操作。若仍失败，检查 `/var/log/letsencrypt/letsencrypt.log`、80 端口和验证路径、磁盘空间与系统时间。配置损坏或安全组关闭等问题仍需人工处理；不要让用户忽略浏览器证书警告。

## 创建管理员、用户管理和恢复

在项目根目录，以 **API 服务实际运行账号**执行（当前部署通常为 `ubuntu`，不要无故用 root 创建应用数据库）：

```bash
APP_ENV_FILE=config/app.config backend/.venv/bin/python backend/manage_users.py init --username admin
sudo systemctl restart showallopts-api
```

密码在终端交互输入两次，不回显。已有账号后不能再次初始化。进入 `https://119.28.106.127`，登录后在右上角“用户管理”创建、停用用户、修改角色或重置密码。至少保留一个启用的管理员。停用、角色变化及重置密码会使该用户已有登录失效；用户修改自己的密码后需要重新登录。

管理员忘记密码时，在服务器终端执行：

```bash
APP_ENV_FILE=config/app.config backend/.venv/bin/python backend/manage_users.py reset-password --username admin
```

Docker 初始化对应命令：`docker compose exec --user appuser backend python manage_users.py init --username admin`。上述 Nginx 模板是原生部署模板，Docker 的 TLS 终止和代理可信地址需按容器网络另行配置，不能直接套用回环地址。

## 持久化和本地开发

- 用户、会话摘要、限流和审计记录存于 `LOG_DIR/accounts/accounts.sqlite3`，不会写入策略源 MySQL。Linux 目录 0700、数据库 0600；备份使用 SQLite 在线备份，或停服务后复制整个目录。不要作为日志清理，不要提交到 Git。
- 会话最多 8 小时，连续 2 小时无请求失效；Cookie 为 HttpOnly、SameSite=Strict，HTTPS 下为 Secure。写请求校验同源和 CSRF。用户登录连续失败受用户名和 IP 双重限速，重启不能绕过。浏览器本地存储不保存密码或会话令牌。
- 原 `VALIDATION_ADMIN_KEY`、`VALIDATION_ALLOW_HTTP`、`SOCIAL_BROWSER_LOGIN_KEY` 已不用于鉴权，仅兼容旧配置脱敏；旧配对会话也不能登录。原 `VALIDATION_ENCRYPTION_KEY`、`VALIDATION_ALLOWED_TARGETS` 仍按原含义生效，不要误删数据库配置加密密钥。旧 `short-validation-auth` 目录不再读取，先备份后可人工清理。
- 本地开发通过回环地址访问时，可在本地私有配置显式设置 `AUTH_ALLOW_HTTP=true`、`AUTH_PUBLIC_ORIGIN=http://127.0.0.1:5173`，并同样初始化管理员。**不用于公网 HTTP**；哈希存储不能防止明文网络窃听密码或会话。
