# 微博、雪球服务器扫码登录

浏览器运行在部署 ShowAllOpts 的服务器上；电脑只访问网页中的登录画面，手机扫描官方二维码。Ubuntu 无需安装桌面，也不需要开放远程桌面、VNC 或浏览器调试端口。

## 已有 Ubuntu 部署升级

在项目根目录执行（虚拟环境若不在 `backend/.venv`，请替换路径）：

```bash
backend/.venv/bin/python -m pip install -r backend/requirements.lock.txt
sudo backend/.venv/bin/python -m playwright install-deps chromium
backend/.venv/bin/python scripts/install_social_browser.py
backend/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

最后一条命令生成个人管理密钥。将其写入**服务实际读取的私有配置**（生产通常为 `config/app.config`，本地默认为 `backend/.env`）：

```dotenv
SOCIAL_BROWSER_LOGIN_KEY=此处填写刚生成的随机密钥
SOCIAL_BROWSER_CHANNEL=
SOCIAL_BROWSER_HEADLESS=true
```

安装脚本默认把浏览器放在项目 `.runtime/playwright/`，避免 systemd 的 `ProtectHome` 阻止读取用户缓存。由部署账号安装，确保服务账号有读取和执行权限。若自定义 `PLAYWRIGHT_BROWSERS_PATH`，安装和运行服务时必须使用同一个目录。升级 Playwright 版本后需重新运行安装脚本。

Linux 浏览器启用 Chromium 沙箱，要求操作系统允许服务账号使用用户命名空间；Ubuntu 的 AppArmor、systemd 限制或 Docker 的 seccomp 策略可能阻止启动，需要管理员为该服务配置相应权限。程序不会自动关闭沙箱。镜像安装通过不代表目标主机的沙箱策略已验收。

更新、构建发布前端并重启 `showallopts-api`。现有 `deploy.sh` 不自动安装 Chromium 及其系统库，上述步骤需要额外执行一次。Docker 镜像已加入 Chromium 和系统库安装，重新构建镜像即可；私有管理密钥仍需通过容器环境注入，持久化原有 `log` 挂载。

## 网页操作

### 本机 Windows 的浏览器显示模式

如果无界面扫码窗口被平台拒绝，但普通显示窗口测试正常，可在本机私有配置中设置：

```dotenv
SOCIAL_BROWSER_CHANNEL=chrome
SOCIAL_BROWSER_HEADLESS=false
```

重启本机项目后，点击「打开扫码登录」会弹出独立 Chrome 窗口，也会继续向网页提供画面。可以直接在 Chrome 的官方登录页操作，切换「扫码登录」并用雪球 App 扫描、手机确认；网页仍使用管理密钥保护。使用独立资料目录，不控制日常 Chrome 的资料。最终必须等到项目显示“登录已验证，凭据已加密保存”才算成功。

Linux 纯命令行服务不能直接使用这个设置。没有图形显示时保持 `SOCIAL_BROWSER_HEADLESS=true`；如需显示窗口模式，管理员可以使用 Xvfb 提供虚拟显示环境，项目网页仍通过截图展示登录画面，无需远程桌面。平台是否放行仍需在实际服务器验证。

### Ubuntu 命令行服务器通过 Xvfb 运行显示模式

以下沿用项目目录 `/home/ubuntu/showAllOpts`、服务名 `showallopts-api`；其他部署必须替换 `ExecStart` 中的路径和启动参数。先更新代码，确认 Chromium 与系统库已按前文安装，AppArmor 用户命名空间规则也已生效。

```bash
sudo apt update
sudo apt install -y xvfb xauth
```

在服务实际读取的私有配置（通常 `config/app.config`）中设置：

```dotenv
SOCIAL_BROWSER_HEADLESS=false
SOCIAL_BROWSER_CHANNEL=
```

Linux 使用已下载的 Chromium，`SOCIAL_BROWSER_CHANNEL` 保持空；不要直接复制 Windows 上的 `chrome` 设置。增加一个排在现有 Ubuntu 覆盖配置之后的 systemd 配置：

```bash
sudo install -d /etc/systemd/system/showallopts-api.service.d
sudo tee /etc/systemd/system/showallopts-api.service.d/zz-browser-display.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/xvfb-run -a -s "-screen 0 1280x900x24" /home/ubuntu/showAllOpts/backend/.venv/bin/python /home/ubuntu/showAllOpts/backend/run.py --host 127.0.0.1 --port 8000 --workers 2
EOF
sudo systemctl daemon-reload
sudo systemctl restart showallopts-api
sudo systemctl status showallopts-api --no-pager
```

这会给 API 进程和它启动的浏览器提供虚拟显示，不开放新网络端口。仍从电脑访问项目页面、输入管理密钥、打开扫码画面、用手机确认。不需要远程桌面；不关闭 Chromium 沙箱或 AppArmor。直接在普通 SSH 终端运行诊断时，可用 `xvfb-run -a env APP_ENV_FILE=config/app.config backend/.venv/bin/python scripts/check_social_browser.py`；这与 systemd 环境不同，最终仍需在网页完成一次扫码验收。

本次已在 Windows 可见 Chrome 中完成实际扫码、当前账号身份校验和加密保存；Linux/Xvfb 尚未在目标服务器验收。显示模式不是永久放行保证，仍可能收到平台验证或访问拒绝。恢复无界面模式时设置 `SOCIAL_BROWSER_HEADLESS=true`，同时移除本段新增的 `zz-browser-display.conf` 并重载、重启服务。

1. 打开「博主动态监控 → 通知设置 → 扫码登录」，输入管理密钥，点击「查看登录状态」。
2. 对微博或雪球点击「打开扫码登录」。画面来自服务器上的官方网页，可点击画面里的扫码登录入口；按官方页面提示用微博、雪球或微信应用扫码并在手机确认。
3. 页面显示「登录已验证，凭据已加密保存」才表示成功。登录检测最多每十秒一次，也可点「检查登录结果」。每个平台的凭据供该平台所有博主共用。
4. 已有会话会在下一次打开窗口时优先复用；过期后重新扫码。若平台原先因风控而暂停，确认问题解决后再点击原页面的「恢复监控」。登录不自动打开总监控开关。

二维码窗口最多六分钟。刷新网页后重新输入密钥即可查看尚未结束的窗口；多个 API worker 共享窗口状态，同一平台只允许一个浏览器实例。隐藏窗口或切换标签页不会取消服务器登录，取消需点击「取消登录」。关闭窗口后浏览器进程退出，用户资料目录保留。

画面支持点击，不支持键盘输入、滚动和滑块拖动。验证码、短信、滑块等要求人工验证时会明确提示；若当前画面无法完成，请取消，并在平台提供的正式渠道处理或继续使用原有手动 Cookie 配置。不会尝试绕过验证。

## 存储与访问保护

- `${LOG_DIR}/social/browser-profiles/weibo/`、`xueqiu/`：Chromium 独立资料目录，保存 Cookie、站点存储等。**浏览器资料目录本身不使用应用的 Fernet 加密**，仅限服务账号访问；应视为账号凭据，备份也需保护。
- 每份浏览器资料中的 `cookies.enc`：额外加密保存会话 Cookie，使浏览器正常退出后也能复用未过期的会话；保留原来的过期属性，不修改平台有效期。站点仍可随时让会话失效。
- `${LOG_DIR}/social/browser-login/`：加密窗口状态、临时截图、操作指令及进程锁；结束时删除截图，API 不提供资料目录下载。Linux 目录权限为 `0700`，应用新建状态文件为 `0600`。
- `${LOG_DIR}/social/monitor.sqlite3`：验证成功后导出的抓取 Cookie，沿用现有 Fernet 加密；密钥仍为同目录 `secrets.key`。Cookie 不返回前端。
- 管理密钥保存在私有配置，前端仅在内存保存，通过 `X-Browser-Login-Key` 标头发送，不能放在 URL、公开配置或 Git。使用 HTTPS 或 SSH 隧道，避免经明文公网传输登录画面和密钥。

现有监控管理页面和原接口仍按原设置开放；此次密钥只保护新增的服务器浏览器入口。登录期间暂停对应平台的抓取，避免与扫码操作并发；其他平台按原计划工作。

## 验证与限制

浏览器启动失败时，可在项目根目录以 API 服务实际运行账号执行诊断（以下假设为 `ubuntu`）：

```bash
sudo -u ubuntu env APP_ENV_FILE=config/app.config backend/.venv/bin/python scripts/check_social_browser.py
```

脚本使用临时资料目录，仅检查浏览器启动、写入和截图，不读取已有登录资料、不请求微博或雪球，也不输出 Cookie 或管理密钥。它会输出安全的分类错误，例如 `browser_missing`（浏览器未安装或路径不匹配）、`system_dependencies_missing`（缺少系统库）、`permission_denied`（权限）、`sandbox_denied`（沙箱受限）。网页错误会显示失败阶段和分类代码，服务日志仅记录阶段与代码，不输出原始异常中的敏感 URL。

若仍只有通用的 `browser_failed`，使用增强诊断及 Playwright 的浏览器启动日志：

```bash
cd /home/ubuntu/showAllOpts
xvfb-run -a -e /dev/stderr env APP_ENV_FILE=config/app.config DEBUG=pw:browser \
  backend/.venv/bin/python scripts/check_social_browser.py 2>&1 | tail -n 120
```

脚本会显示实际运行模式、失败阶段、DISPLAY、XAUTHORITY 是否可读、Xvfb/xauth 是否安装及当前进程的 NoNewPrivs/Seccomp。XAUTHORITY 内容不读取、不输出。DEBUG 仅用于这个临时空资料目录和内联测试页面的启动检查，不应直接用于含真实登录资料的扫码流程。日志中的 Chromium stderr 才能进一步判断进程为何退出；不能仅凭 NoNewPrivs 值或单个 DBus 告警确定故障。

如果失败阶段为 `capture_screenshot`，说明启动和设置测试页面已经返回，但截图时仍可能发生浏览器退出或渲染错误。此时查看新增的 `Error details (redacted)`（保留协议错误和调用日志）以及关闭浏览器前采集的 `Page state at failure`。诊断视口与实际扫码统一为 1100×800，截图超时为 15 秒。`xkbcomp` 自报非致命的提示不能单独作为失败原因；不要在没有截图错误内容时关闭 GPU、沙箱或 AppArmor。

手动诊断不处于 systemd 的沙箱环境：若脚本通过而网页仍失败，应进一步检查服务实际配置路径、环境变量和 systemd 的目录/权限限制。不要通过关闭 Chromium 沙箱或将服务改为 root 来排除问题。

微博通过移动端官方登录状态接口确认已登录；雪球通过登录页所在浏览器请求官网当前使用的 `/user/show.json`，要求返回的账号 ID、昵称与 Cookie（包括刷新凭据）匹配。访客 Cookie 不算登录成功，不再依赖旧版 `SNB.data.user` 页面变量。身份校验返回访问拒绝或限流时停止窗口，不反复请求。不能仅凭二维码消失判断成功。

会话持久化不保证 Cookie 永久有效。日常抓取仍沿用现有 HTTP 请求方式，浏览器仅在打开登录窗口时运行；不通过后台循环登录延长平台有效期。平台要求重新认证时仍需人工续登。

服务器出口 IP、地区、浏览器运行模式或平台策略可能让官方页面拒绝访问。页面会显示拒绝原因并终止窗口，不重试突破风控；更换网页访问电脑不会改变服务器出口。本机显示窗口模式已经完成真实扫码、身份校验、加密保存及新 Cookie 抓帖验证；Ubuntu/Xvfb 尚未在目标服务器验收，部署后必须分别进行一次人工扫码验收。
