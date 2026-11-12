# main 自动部署：一次配置，之后 push 自动发布

## CI 浏览器环境

2026-11-11 发布检查先后遇到 Azure Ubuntu 镜像及官方 archive/security 源 DNS 解析失败，浏览器依赖无法安装，端到端未启动，部署安全跳过。单纯换源并不足以解决这次网络故障。

前端 CI 使用微软官方预装浏览器镜像 `mcr.microsoft.com/playwright:v1.64.1-noble`，固定为已从官方注册表校验的多架构清单摘要 `sha256:2f05f8a734509a7b3f26a912639488ee08f58a7b292d14d62271bcff9038768f`。镜像含 Chromium 及其系统依赖，CI 不再现场执行 APT 或下载另一个浏览器。仍使用 Node 22 和 `npm ci` 安装项目锁定依赖。

运行完整 E2E 前，`frontend/scripts/verify-ci-browser.mjs` 核对锁文件、实际安装的三个 Playwright 包与镜像标记版本一致，确认使用镜像浏览器目录且没有替代可执行文件，并实际启动、关闭 Chromium。版本不符、浏览器缺失或无法运行都会失败。升级 Playwright 锁文件时须同步审核官方镜像版本及摘要。

单测、构建、桌面和移动端全部 Playwright 测试、失败证据及后端/前端共同通过才能部署的门槛均保留。镜像拉取或测试失败仍阻止部署；不改 DNS、签名校验、生产服务器设置，也不放宽权限或启用特权容器。原直接换源脚本已移除，避免保留无效配置路径。

依据：[Playwright 官方 CI 容器方案](https://playwright.dev/docs/ci#via-containers)、[官方镜像和版本匹配要求](https://playwright.dev/docs/docker)、[微软镜像清单](https://mcr.microsoft.com/v2/playwright/manifests/v1.64.1-noble)。

固定目标：`ubuntu@119.28.106.127:22`，项目 `/home/ubuntu/showAllOpts`，网站 `http://119.28.106.127:8081`。以下材料尚未执行；代码审核发布后填写真实提交号。

## 用户只做三步

1. **在服务器现有 ubuntu 会话运行一次**，只替换审核后的提交号。会生成专用 key 文件但不打印私钥，安装固定入口和公钥；不更新运行中源码，不重启服务。
   ```bash
   (
     set -euo pipefail
     umask 077
     REVIEWED_SHA='替换为审核并发布后的40位提交号'
     [[ "$REVIEWED_SHA" =~ ^[0-9a-f]{40}$ ]]
     cd /home/ubuntu/showAllOpts
     git fetch origin main
     git merge-base --is-ancestor "$REVIEWED_SHA" FETCH_HEAD
     KEY_DIR="$(mktemp -d /home/ubuntu/.showallopts-key.XXXXXX)"
     ssh-keygen -q -t ed25519 -N '' -f "$KEY_DIR/actions" -C showallopts-actions
     BUNDLE="$(mktemp -d)"
     trap 'rm -rf -- "$BUNDLE"' EXIT
     git archive "$REVIEWED_SHA" scripts/deploy.py deploy/automation | tar -x -C "$BUNDLE"
     printf '专用私钥文件（仅直接填入 GitHub）：%s/actions\n' "$KEY_DIR"
     /usr/bin/python3 -I "$BUNDLE/deploy/automation/bootstrap.py" \
       --commit "$REVIEWED_SHA" --public-key "$(cat "$KEY_DIR/actions.pub")"
   )
   ```
   私钥仅保存在权限 700 的专用目录中；使用可信服务器会话中的编辑器打开该文件并直接复制到下一步 GitHub，不发聊天、不提交、不打印到 CI 日志。不要重跑整个生成块创建多把 key：安装中断时，用原公钥和相同 SHA 重试 bootstrap 即可。助手本次没有生成或读取凭据。

2. **GitHub 保存私钥**：Settings → Environments → `production`，允许部署分支设为 `main`，添加 Secret **`DEPLOY_SSH_KEY`**，填专用完整私钥。需要完全自动发布则不设 required reviewers。确认保存后可由用户删除上一步专用私钥文件，保留公钥记录；不影响其他登录 key。

3. **验收并启用**：Actions 手动选择 `main` / `check`；成功后在 Settings → Secrets and variables → Actions → Variables 添加**仓库变量** `DEPLOY_ENABLED=true`，再手动选择 `main` / `deploy` 完成首次发布。此后任何人 push main 都在完整测试通过后部署。启用变量不能只放 production 环境，因为 job `if` 求值时还不可用。无需手填主机公钥或预检开关。

## 实现与维护边界

- backend 全量 pytest，frontend Vitest、build、Playwright 全部通过才能访问服务器。没有削减 CI，也没有第三方 SSH Action。
- 固定小入口安装到 root 管理的 `/usr/local/libexec/showallopts-ssh`；配置为 `/etc/showallopts-deploy.json`。每次 `check/deploy <完整SHA>` 都核验 origin、干净 main、提交确实属于远端 main 且可从当前 HEAD 快进，再在私有临时目录暂存该 SHA 的 `scripts/deploy.py`。不提前切换生产源码；因此 runner 修复也随通过测试的 main 提交自动生效，旧源码仍是恢复基点。
- bootstrap 在授权公钥前验证环境和入口。文件以原子 rename 发布；已有内容、属主及权限完全匹配时可重试，不同安装拒绝覆盖。公钥原子追加，完全相同条目不重复添加，同 key 不同权限条目拒绝。旧 authorized_keys 内容保留。固定入口/配置自身若需改变，应单独审核升级，不自动覆盖。
- 使用现有免密 sudo，不安装 admin helper/sudoers，不改变静态树属主、账号、网络、unit 或 drop-in；保留 xvfb 与截图支持。服务器须已有非交互读取仓库的能力；现有 unit/drop-in/Nginx 由 root 管理。保留用户明确选择的 `NOPASSWD ALL`，main 写入者可以取得 ubuntu/root；入口不声称隔离 main 写入者。
- SSH key 使用 restrict + forced command。已可信核验的主机公钥固定在 workflow，指纹 `SHA256:BP5xdzMVEFxMOGoF+Sl2HYh0vS9xQqkvgjwbZqeZInw`，开启 StrictHostKeyChecking。主机换钥须可信核验后修改代码。
- API 就绪要求当前 unit cgroup 内 MainPID 或其合法子进程拥有监听 socket，要求监听进程与整条祖先链的实际 UID 和网络 namespace 均匹配当前探测进程，跨 HTTP 和 socket 检查复核 InvocationID、祖先链及正数 PID start-time，兼容 xvfb 和多 worker，不读取 `/proc/*/environ`。
- Actions 串行队列不取消正在发布的任务，服务器也有部署锁；队列最多等待 100 项。旧 SHA 不能覆盖新版本。
- 保留准备、切换、恢复及备份流程。失败尝试恢复源码、虚拟环境和静态文件；恢复后可能 detached HEAD，需人工协调。数据库/业务数据不回滚；断电/SIGKILL 不保证自动恢复。

停止后续发布：把仓库 `DEPLOY_ENABLED` 改为 `false`。不要中断正在激活/恢复的任务。真实凭据、网络可达性和生产发布仍须验收；本地测试不代表已部署。

## 脚本目录变更不自动部署

所有main推送仍跑完整CI。`scripts/deployment_impact.py` 比较推送前后完整差异：仅 `scripts/`、`collectors/`、`docs/`、`backend/tests/`、`frontend/tests/` 内文件（含子目录）变化时，跳过deploy。因此聚宽采集脚本及其测试、文档一起修改也不会发布网站。`scripts-other/`、`collectors-other/`、`backend/tests-other/` 等相似前缀不包含在内。

目录外应用、共享运行时代码、工作流、依赖或混合变更仍部署。目录内的 `scripts/deploy.py`、同步工具、分类程序等也按用户要求跳过自动部署；这些工具更新若需立即激活，须在CI通过后手动运行 `operation=deploy`，否则会随以后一次正常部署一起进入服务器。删除必需脚本可能使CI失败，此规则不绕过测试。

比较失败、SHA缺失/非祖先或空差异时阻止自动发布；手动deploy/check不执行推送分类，原有安全验证、权限、凭据及部署开关均不变。
