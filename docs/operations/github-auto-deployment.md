# main 自动部署：一次配置，之后 push 自动发布

## PR 前置检查与生产边界

面向 main 的 pull request 与 main 的 push 都运行完整 CI。PR 使用 GitHub 的合并预检提交，独立于生产发布队列，不读取生产环境、SSH Secret 或执行服务器命令。普通功能分支 push 不部署；手动 workflow_dispatch 只有 main 上原有的 check / deploy 入口可访问受限服务器。

固定状态检查名称为 **CI gate**，汇总三个后端分组、两个前端分组（含前端工程质量）、后端工程质量检查和独立的 MySQL 集成矩阵；任一必需 job 失败、取消或跳过，汇总门禁均失败。生产部署只有在该门禁成功且满足原 main 事件/开关条件时才执行。

GitHub branch protection / ruleset 是仓库设置，不由本文件自动修改。管理员可在启用前置检查后，将 **CI gate** 配置为 main 的必需状态检查、要求走 PR；开启前须确认检查名称已出现并通过，避免无法合并。未调整仓库设置时，新增 PR CI 只有检查能力，不能声称已经强制禁止直接推送 main。

## 并行检查与耗时

后端完整测试由 3 个独立 GitHub runner 分组执行，按实际收集的测试数量分配完整测试文件，保留文件内顺序和共享 fixture；本地不传分组选项仍运行全套。前端使用 Playwright 原生 `--shard` 分成 2 组，桌面和移动端均保留。五组完整离线测试、后端工程质量和独立 MySQL 集成（8.4 / 5.7.44）均成功后才允许部署；任一必需检查失败都会阻止发布，失败截图按组分别保存。

后端每组输出最慢 20 项测试及实际分配数量。分组回归测试验证完整覆盖、无重复、空分组报错和默认全量行为。新增并行环境仅存在于 GitHub 临时 runner，不在生产服务器创建额外虚拟环境。服务器继续沿用现有发布及清理流程。

MySQL CI 使用每个任务独立的临时 Docker 服务，固定虚构账号与数据库名、随机宿主机端口、loopback 连接，不读取应用环境或任何生产数据库凭据。8.4 与 5.7.44 版本矩阵显式运行真实数据库测试；普通离线 pytest 未开启集成测试时可跳过这些用例，MySQL job 不允许跳过或零执行。镜像 8.4 跟踪该发布系列，5.7 固定最后一个补丁版本；它们用于兼容验证而非生产镜像部署。

2026-11-13 优化前的参考耗时：后端测试约 10～14 分钟、前端 E2E 约 7 分钟，两者并行；服务器部署约 1～2 分钟。实际收益取决于 runner 排队和各组测试耗时，以 Actions 日志为准。

## CI 浏览器环境

2026-11-11 发布检查先后遇到 Azure Ubuntu 镜像及官方 archive/security 源 DNS 解析失败，浏览器依赖无法安装，端到端未启动，部署安全跳过。单纯换源并不足以解决这次网络故障。

前端 CI 使用微软官方预装浏览器镜像 `mcr.microsoft.com/playwright:v1.64.1-noble`，固定为已从官方注册表校验的多架构清单摘要 `sha256:2f05f8a734509a7b3f26a912639488ee08f58a7b292d14d62271bcff9038768f`。镜像含 Chromium 及其系统依赖，CI 不再现场执行 APT 或下载另一个浏览器。仍使用 Node 22 和 `npm ci` 安装项目锁定依赖。

运行完整 E2E 前，`frontend/scripts/verify-ci-browser.mjs` 核对锁文件、实际安装的三个 Playwright 包与镜像标记版本一致，确认使用镜像浏览器目录且没有替代可执行文件，并实际启动、关闭 Chromium。版本不符、浏览器缺失或无法运行都会失败。升级 Playwright 锁文件时须同步审核官方镜像版本及摘要。

单测、构建、桌面和移动端全部 Playwright 测试、失败证据及后端/前端共同通过才能部署的门槛均保留。镜像拉取或测试失败仍阻止部署；不改 DNS、签名校验、生产服务器设置，也不放宽权限或启用特权容器。原直接换源脚本已移除，避免保留无效配置路径。

依据：[Playwright 官方 CI 容器方案](https://playwright.dev/docs/ci#via-containers)、[官方镜像和版本匹配要求](https://playwright.dev/docs/docker)、[微软镜像清单](https://mcr.microsoft.com/v2/playwright/manifests/v1.64.1-noble)。

固定目标：`ubuntu@119.28.106.127:22`，项目 `/home/ubuntu/showAllOpts`，网站 `http://119.28.106.127:8081`。以下为首次安装或重新授权时的操作；已有有效配置不需要重复生成密钥。仅替换为已审阅并发布的提交号。

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

- backend 全量 pytest、Ruff/Mypy 质量门禁、两版本 MySQL 隔离测试，以及 frontend 质量门禁、Vitest、build、Playwright 全部通过才能访问服务器。没有削减 CI，也没有第三方 SSH Action。
- 固定小入口安装到 root 管理的 `/usr/local/libexec/showallopts-ssh`；配置为 `/etc/showallopts-deploy.json`。每次 `check/deploy <完整SHA>` 都核验 origin、干净 main、提交确实属于远端 main 且可从当前 HEAD 快进，再在私有临时目录暂存该 SHA 的 `scripts/deploy.py`。不提前切换生产源码；因此 runner 修复也随通过测试的 main 提交自动生效，旧源码仍是恢复基点。
- bootstrap 在授权公钥前验证环境和入口。文件以原子 rename 发布；已有内容、属主及权限完全匹配时可重试，不同安装拒绝覆盖。公钥原子追加，完全相同条目不重复添加，同 key 不同权限条目拒绝。旧 authorized_keys 内容保留。固定入口/配置自身若需改变，应单独审核升级，不自动覆盖。
- 使用现有免密 sudo，不安装 admin helper/sudoers，不改变静态树属主、账号、网络、unit 或 drop-in；保留 xvfb 与截图支持。服务器须已有非交互读取仓库的能力；现有 unit/drop-in/Nginx 由 root 管理。保留用户明确选择的 `NOPASSWD ALL`，main 写入者可以取得 ubuntu/root；入口不声称隔离 main 写入者。
- SSH key 使用 restrict + forced command。已可信核验的主机公钥固定在 workflow，指纹 `SHA256:BP5xdzMVEFxMOGoF+Sl2HYh0vS9xQqkvgjwbZqeZInw`，开启 StrictHostKeyChecking。主机换钥须可信核验后修改代码。
- API 就绪要求当前 unit cgroup 内 MainPID 或其合法子进程拥有监听 socket，要求监听进程与整条祖先链的实际 UID 和网络 namespace 均匹配当前探测进程，跨 HTTP 和 socket 检查复核 InvocationID、祖先链及正数 PID start-time，兼容 xvfb 和多 worker，不读取 `/proc/*/environ`。
- Actions 串行队列不取消正在发布的任务，服务器也有部署锁；队列最多等待 100 项。旧 SHA 不能覆盖新版本。
- 保留准备、切换、恢复及备份流程。失败尝试恢复源码、虚拟环境和静态文件；恢复后可能 detached HEAD，需人工协调。数据库/业务数据不回滚；断电/SIGKILL 不保证自动恢复。

停止后续发布：把仓库 `DEPLOY_ENABLED` 改为 `false`。不要中断正在激活/恢复的任务。真实凭据、网络可达性和生产发布仍须验收；本地测试不代表已部署。

## 仅文档、脚本或测试变更时不实际发布

正常 main 推送仍跑完整 CI，统一 CI gate 的全部必需检查成功且 `DEPLOY_ENABLED=true` 后调用受限服务器入口。是否实际发布，由服务器在部署锁内比较当前健康发布版本与本次已测试 SHA 的累计差异，不再比较单次推送的 before/head。Actions 仍可能显示 `deploy` 作业成功，因为它运行了受限 SSH 入口及线上基线校验。实际跳过以日志中的 `Deployment cumulative impact ... required=False` 和随后的 `Deployment not required: no cumulative runtime changes; live release unchanged.` 为准，不以作业颜色判断。

基线来自 `backend/.venv` 实际指向的 `.runtime/releases/<release>/status.json`：必须为 `complete`，目标 SHA 与干净生产 checkout 一致，服务、分支及祖先关系均须匹配，并通过现有进程身份和健康检查。仅 CI 成功、check 成功或上一轮跳过发布都不会前移基线。

累计差异仅涉及根路径 `README.md`、`scripts/`、`collectors/`、`docs/`、`backend/tests/`、`frontend/tests/` 时，不创建 release、不构建、不重启、不移动源码或基线。`scripts/deploy.py` 是部署运行器，保持原有运行时代码例外；应用、依赖、工作流及其他目录的变更仍实际发布。相似前缀（如 `scripts-other/`）不在排除范围。空差异同样为 no-op。根 README 仅按精确区分大小写的 `README.md` 路径排除；其他目录中的同名文件、近似文件名和未发布的运行差异仍按原规则处理。更改发布判定本身（`scripts/deploy.py`）是运行相关变更；先完成该发布器修订的独立 CI 与部署验收，再单独推送 README／文档。

例如功能提交 A 的 CI 失败，随后只改测试的 B 通过，服务器会从仍在运行的旧版比较到 B，发现 A 的未发布功能，正常部署 B。若线上已包含全部功能而只修改采集脚本，则只检查而不发布网站。

缺失、损坏、非祖先或不健康基线保守拒绝，不猜测已部署版本。已有失败恢复流程不变，只有完成激活和健康检查后才写 `complete`；回滚保留原基线。首次安装或故障恢复仍使用人工部署流程，确认环境后再恢复自动化。

原已安装的受限 SSH 入口会读取本次已测试 SHA 的独立 `scripts/deploy.py`，并传入 `--non-interactive --commit`，因此此次修复不需要更换 root 所有入口、不增权限或凭据。手动 `operation=deploy` 也执行累计检查；`operation=check` 仍只做认证、预检和健康检查。
