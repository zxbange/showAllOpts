# 架构与扩展约定

## 请求流向

```text
浏览器 React 页面
  → frontend/src/api/client.ts
  → Vite / Nginx /api 代理
  → FastAPI 路由：校验查询参数
  → Service：确定快照日期、计算区间指标、标记行业缺失
  → Repository：参数化 SELECT
  → 本地 SQLite 模块快照（每日 18:00 / 手动从 MySQL 同步）

聚宽采集 → MySQL 模块数据表 → 后台只读同步 → 本地 SQLite 快照
```

前后端独立进程、独立依赖、独立构建。后端不返回网页，前端不直接访问数据库。数据库凭据只由后端从环境变量或选中的配置文件读取：Windows 启动器及直接运行 Python 未指定配置时使用 `backend/.env`；Linux `start.sh`、systemd 和 Compose 默认选择项目根目录下的 `config/app.config`。配置优先级与迁移方式见 [配置管理](configuration.md)。所有查询通过 SQLAlchemy 表达式绑定参数，不拼接用户输入的 SQL。

## Linux 部署约定

当前部署目录为 `/home/ubuntu/showAllOpts`，systemd 服务名为 `showallopts-api`，运行账号和组为 `ubuntu`。基础 service 配合本机 `99-ubuntu-home.conf` 覆盖配置运行，最终读取 `config/app.config`，写入项目 `log/`。`ProtectHome=read-only` 允许读取家目录中的代码，`ReadWritePaths` 允许写入日志目录。

Nginx 从 `/var/www/showallopts` 提供前端静态资源，将 `/api` 代理到 `127.0.0.1:8000`。前端资源发布与后端进程启停分别管理。安装、权限、服务配置和升级步骤见 [Linux 部署指南](linux-deployment.md#4-systemd--nginx-后台服务)。

## 数据一致性

快照先确定分类内最大日期，再读取该日期全部行业，不把不同行业各自的最新日期拼成一张图。历史请求带快照日期，防止浏览过程中采集程序更新导致上下文变化。每次请求使用独立 Session，关闭时结束只读事务。

接口排序使用动量和行业代码；显示排名根据排序重新编号。原始采集排名仍用于历史点信息。零分行业始终保留。

行业动量数据库没有独立的交易日历或采集完成标志。历史缺失检查仅基于该分类有数据的日期，不能判断全市场共同缺失日，也不能保证某个最新快照已经完整采集。应在采集程序解决数据质量问题；网页不推断或伪造分数。

## 扩展新模块

1. 在后端增加对应 model / repository / service / schema / router；业务不堆叠到 `main.py`。
2. 在 `/api/v1/modules` 注册模块元信息。
3. 在前端增加页面与 API 类型，在 `App.tsx` 注册路由，并在 `src/lib/modules.ts` 登记名称、路径、分类、说明、标签、图标与视觉类型。首页卡片、搜索、分类计数、导航和面包屑共用此注册表，不必逐个修改入口。后端 `/modules` 元信息仍需同步维护。
4. 增加必要的查询边界测试及浏览器用户流程测试。

当前模块包括申万二级行业动量、抱团拥挤度、微盘温度计、ETF 份额仪表盘和净利润断层。抱团拥挤度对应 `market_crowding` 表，复合主键为 `(top_pct, trade_date)`，每次请求一次 SELECT 获取数据，在服务层计算统计和 EMA 10；前端只将比例转换为百分比显示。模型共用 `models/base.py` 中的 Base，网页不创建表。

各类采集的保留规则独立：行业动量仅保留 180 个交易日；拥挤度默认保留 2020 年以来的完整历史；微盘温度计保留 2021 年以来的五交易日采样历史；ETF 份额首次采集最近 300 个交易日，后续增量积累历史，不自动清理。细节见 [抱团拥挤度说明](market-crowding.md) 与 [微盘温度计说明](microcap-temperature.md)。

微盘温度计使用独立的 `microcap_temperature` 表。采集器保存原始指标、当期历史分位、综合温度、累计指数和成分股；刷新最后一期时从该期之前的基准续算，避免重复计数。网页模型将成分股字段设为延迟加载，页面接口只读指标，不传输名单。网页不会在切换范围时重算分位或重新设置指数基期。

ETF 份额按基金独立续传，源数据缺口不会补零。复合主键 `(fund_code, trade_date)` 支持最新日期和区间查询；非空页面请求执行两条 SELECT，历史查询只传输绘图字段。各卡片明确标记自己的数据日期，日增减严格关联 `previous_trade_date`；相对变化曲线使用共同基准日。完整口径、首次采集和部署说明见 [ETF 份额仪表盘](etf-shares.md)。

净利润断层使用 `earnings_gap_run` 与 `earnings_gap_stock` 保存每季度最新扫描元数据和全部入选股票。股票表以 `(report_period, code)` 为主键，另有季度+行业、季度+信号日期联合索引。独立脚本默认在采集完成后以一次事务替换目标季度；批量入口使用只新增模式，已有股票保留首次快照，只插入该季度未保存的股票，运行元数据按变化更新，命中数为累计保留数量。两种模式均保存零命中运行元数据。列表查询分页且不加载公告和日线 JSON；详情一次主键查询取得全部证据。网页默认请求当前目标季度，无数据时不回退旧季度。已移除自动原因分析；旧表兼容列保留，API 不加载或返回它们，新采集结果写入空值占位。详见 [净利润断层说明](earnings-gap.md)。

## 错误、并发与浏览器行为

- 后端数据库异常统一返回 503，不向浏览器返回 SQL、驱动异常全文或连接串。
- 前端有加载、空数据、搜索无结果、断网/查询失败、未知页面状态。
- 切换行业/时间范围时取消旧请求，防止较慢请求覆盖新结果。
- ECharts 实例在组件卸载时销毁，容器变化通过 ResizeObserver 调整。
- 热力图单元格使用原生链接，可键盘操作和新标签打开；图表尊重减少动态效果偏好。
- 浏览器只使用 JSON API；所有文字通过 React 渲染，图表 tooltip 使用 canvas richText，避免拼接 HTML。

## Windows 开发启动器的进程管理

`start.cmd` 与 `start.ps1` 共用 `scripts/dev.py`。旧版本只在 Python 的 `finally` 中终止直接子进程，而 Windows 关闭终端可能直接终止启动器，无法保证执行这段清理。前后端以 `CREATE_NO_WINDOW` 启动，也不会依靠终端关闭自动退出。

`scripts/windows_job.py` 在创建任何服务进程之前，将启动器加入一个启用 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Windows Job Object；新建的后代进程自动加入同一个 Job。唯一的 Job 句柄不可继承，始终由启动器持有。启动器正常退出或被强制结束时，操作系统关闭句柄并回收全部后代进程，涵盖虚拟环境 Python 转发进程、Vite 和构建工具子进程。机制依据 [Microsoft Job Objects 文档](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

启动器自己也是 Job 成员，因此不在 `finally` 或析构函数中主动关闭这个句柄；否则会过早终止自身、打断日志刷新。正常 `Ctrl+C` 仍先执行原有退出清理与日志关闭，再由操作系统兜底。保护初始化失败时停止启动，不创建未受保护的服务；其他进程不属于该 Job，不会被清理。Linux 的原有信号处理与 systemd 部署方式不变。

Windows 集成测试创建随机本地端口的子进程和孙进程，分别验证正常退出、强制结束启动器后的进程退出及立即重新绑定端口，同时确认独立服务仍然存活。测试不操作真实 8000 / 5173 服务或数据库。非 Windows 环境跳过此系统测试，启动器行为仍有独立回归测试。

新机制只保护更新后由启动器创建的进程，旧版残留需要按 [README 排查步骤](../README.md#windows-提示端口已被占用) 一次性清理。

## 本地开发依赖

`frontend/package-lock.json` 锁定 Node 依赖；后端 `requirements.txt` 定义运行依赖范围，`requirements-dev.txt` 增加测试依赖，`requirements.lock.txt` 记录本次验证环境的精确依赖版本（含开发依赖）。

## 总览与亮色主题

总览使用前端模块注册表展示全部已接入页面，进入具体模块后才请求对应业务数据。首页不调用行业快照或其他数据库接口，也不把采样频率不同的模块共用一个“最新日期”。分类和搜索在本地完成；无匹配时可重置。新增模块会自动进入目录和导航，移动导航可横向滚动。

主背景、正文、边框和强调色集中在 `frontend/src/styles/global.css` 的 CSS 变量中；总览专属布局在 `styles/overview.css`；ECharts 的亮色画布配色统一在 `lib/chartTheme.ts`。行业热力图保留原有对数映射及排序，色阶由浅米金到朱红，文字按实际背景亮度选用黑色或白色。总览插画标注为视觉示意，不用于表达市场涨跌。

本节的总览与主题调整只涉及网页展示，不更改采集、数据库结构和指标口径。桌面与手机浏览器测试覆盖搜索与分类组合、空结果重置、键盘跳转、所有已注册模块导航、数据库故障状态和原有图表交互。

微盘新高新低使用 `microcap_breadth`，按 `(series_id, trade_date)` 唯一保存每日四周期数量。只新增续传，历史不清理；后端一次限量 SELECT，跳过成分股文本，按需计算比例和滚动指标。详见 [统计与部署说明](microcap-breadth.md)。

高股息选股使用 `dividend_selection_snapshot` 和 `dividend_selection_stock`，由聚宽单文件每次重算并事务替换最新结果，不保留历史；这是其他采集模块增量规则的独立例外。网页通过一次只读 JOIN 查询展示两组 Top25，详情见 [高股息选股](dividend-selection.md)。


## 本地快照查询层

`get_session` 默认按 API 模块选择 `SnapshotCache` 的只读 SQLite 会话，现有 repository/service 查询和响应格式保持一致。只有同步任务连接源 MySQL，网页查询没有自动回源分支；`CACHE_ENABLED=false` 恢复原会话工厂。七个模块、九张表按模块分事务复制，详情 JSON 保留，不复制网页不用的成分股和已删除分析字段。MySQL 使用 REPEATABLE READ，确保同一模块的快照头和明细来自同一事务。

每次生成一个新的 SQLite 文件，完成写入与完整性检查后原子替换元信息；已有请求继续读取旧版本，新请求读取新版本。源数据库保持只读，建表和索引操作仅发生在本地 SQLite。独立文件锁协调 Uvicorn 多进程、每日调度和 CLI/网页手动同步。未完成模块记录在元信息中，进程中断后可继续；数据库/账号/映射结构变化会切换缓存命名空间，避免复用错误数据。详情见[本地缓存说明](local-cache.md)。
