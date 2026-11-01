# 微博、雪球服务器扫码登录

浏览器运行在部署 ShowAllOpts 的服务器上；电脑只访问网页中的登录画面，手机扫描官方二维码。Ubuntu 无需安装桌面，也不需要开放远程桌面、VNC 或浏览器调试端口。

## 已有 Ubuntu 部署升级

在项目根目录执行（虚拟环境若不在 `backend/.venv`，请替换路径）：

```bash
backend/.venv/bin/python -m pip install -r backend/requirements.lock.txt
sudo backend/.venv/bin/python -m playwright install-deps chromium
backend/.venv/bin/python scripts/install_social_browser.py
```

使用全站管理员账号登录，不再生成或填写浏览器管理密钥；先按 [账号与HTTPS配置](账号与HTTPS配置.md) 完成账号和 HTTPS 接入。

更新、构建发布前端并重启 `showallopts-api`。现有 `deploy.sh` 不自动安装 Chromium 及其系统库，上述步骤需要额外执行一次。Docker 镜像已加入 Chromium 和系统库安装，重新构建镜像即可；持久化原有 `log` 挂载。

## 网页操作

### 本机 Windows 的浏览器显示模式

如果无界面扫码窗口被平台拒绝，但普通显示窗口测试正常，可在本机私有配置中设置：

```dotenv
SOCIAL_BROWSER_CHANNEL=chrome
SOCIAL_BROWSER_HEADLESS=false
```

重启本机项目后，点击「打开扫码登录」会弹出独立 Chrome 窗口，也会继续向网页提供画面。可以直接在 Chrome 的官方登录页操作，切换「扫码登录」并用雪球 App 扫描、手机确认；网页由全站管理员会话保护。使用独立资料目录，不控制日常 Chrome 的资料。最终必须等到项目显示“登录正常”才算成功。

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

这会给 API 进程和它启动的浏览器提供虚拟显示，不开放新网络端口。仍从电脑访问项目页面、以管理员登录、打开扫码画面、用手机确认。不需要远程桌面；不关闭 Chromium 沙箱或 AppArmor。直接在普通 SSH 终端运行诊断时，可用 `xvfb-run -a env APP_ENV_FILE=config/app.config backend/.venv/bin/python scripts/check_social_browser.py`；这与 systemd 环境不同，最终仍需在网页完成一次扫码验收。

Windows 可见 Chrome 已完成实际扫码、身份校验和加密保存；用户也已在 Ubuntu/Xvfb 环境完成网页扫码与帖子入库验证。该 Ubuntu 环境需要在 systemd 中保留 `Environment=PLAYWRIGHT_LEGACY_SCREENSHOT=1` 截图兼容开关。显示模式不是永久放行保证，仍可能收到平台验证或访问拒绝。恢复无界面模式时设置 `SOCIAL_BROWSER_HEADLESS=true`，同时移除本段新增的 `zz-browser-display.conf` 并重载、重启服务。

1. 进入「博主动态监控 → 通知设置」，直接查看微博、雪球的登录状态，不再有显示/隐藏窗口开关。
2. 登录正常时只显示「登录正常」和「退出登录」。未登录、失效或平台受限时显示「扫码登录」；点击后显示一个对应平台的官方画面，按提示扫码并在手机确认。重复点击不会再启动新任务。
3. 扫码身份验证成功后自动收起画面，显示「登录正常」；每个平台的凭据供该平台所有博主及组合共用。取消扫码会结束对应后台任务，在途页面操作返回后关闭浏览器。
4. 每次监控抓取使用当前已保存凭据成功后更新正常状态；明确的登录失效响应更新为失效，网络超时或限流不当作退出。无凭据的游客抓取不算登录。手动更新但未验证的凭据显示「等待下一次监控验证」。抓取正常表示当前凭据可用于监控，不等同于每次都重新进行账号身份校验。
5. 「退出登录」清除本程序保存的对应平台 Cookie、浏览器资料和截图，并暂停该平台监控；不退出你手机或其他浏览器的账号。重新扫码成功会恢复因退出或登录过期暂停的该平台；其他风控暂停仍需人工处理，不自动打开总监控开关。

二维码窗口最多六分钟。切换标签页不会新建扫码任务，回到设置后查看现有任务。浏览器每次使用新的临时资料目录，正常关闭后删除，不再恢复旧标签页；已验证 Cookie 单独加密保留。进程异常退出时临时目录可能残留，需在浏览器停止后由管理员检查清理。

网页中的画面支持点击，不支持键盘输入、滚动和滑块拖动。验证码、短信、滑块等要求人工验证时会明确提示；若当前画面无法完成，请取消，在平台提供的正式渠道处理后回到扫码区域重新登录。本机独立 Chrome 窗口支持直接人工操作；页面不再提供手动 Cookie 配置。

## 存储与访问保护

- `${LOG_DIR}/social/browser-profiles/weibo/`、`xueqiu/`：保存加密的 `cookies.enc`。旧版本资料可能仍在这两个目录中，但不会加载旧标签页或站点存储；退出对应平台时清理。
- 同目录下每次创建的临时浏览器资料只在扫码任务期间使用，正常关闭后删除。浏览器运行中的临时文件不使用 Fernet 加密，仅限服务账号访问。
- `${LOG_DIR}/social/browser-login/`：加密窗口状态、临时截图、操作指令及进程锁；结束时删除截图，API 不提供资料目录下载。Linux 目录权限为 `0700`，应用新建状态文件为 `0600`。
- `${LOG_DIR}/social/monitor.sqlite3`：验证成功后导出的抓取 Cookie，沿用现有 Fernet 加密；密钥仍为同目录 `secrets.key`。Cookie 不返回前端。
- 全站会话保护所有接口，扫码窗口、操作和截图只向管理员开放；写操作校验同源和CSRF。独立 `X-Browser-Login-Key` 已移除，旧密钥不能绕过账号登录。

登录期间暂停对应平台的抓取，避免与扫码操作并发；其他平台按原计划工作。

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

服务器出口 IP、地区、浏览器运行模式或平台策略可能让官方页面拒绝访问。页面会显示拒绝原因并终止窗口，不重试突破风控；更换网页访问电脑不会改变服务器出口。本机显示窗口模式与用户的 Ubuntu/Xvfb 部署均已完成扫码及抓帖验证；新环境部署后仍需分别进行一次人工扫码验收。
