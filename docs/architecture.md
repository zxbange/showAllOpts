# 架构与扩展约定

## 请求流向

```text
浏览器 React 页面
  → frontend/src/api/client.ts
  → Vite / Nginx /api 代理
  → FastAPI 路由：校验查询参数
  → Service：确定快照日期、计算区间指标、标记行业缺失
  → Repository：参数化 SELECT
  → MySQL industry_momentum / market_crowding / microcap_temperature 表
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
3. 在前端增加页面与 API 类型，注册路由和导航，首页增加模块入口。
4. 增加必要的查询边界测试及浏览器用户流程测试。

当前模块包括申万二级行业动量、抱团拥挤度和微盘温度计。抱团拥挤度对应 `market_crowding` 表，复合主键为 `(top_pct, trade_date)`，每次请求一次 SELECT 获取数据，在服务层计算统计和 EMA 10；前端只将比例转换为百分比显示。模型共用 `models/base.py` 中的 Base，网页不创建表。

各类采集的保留规则独立：行业动量仅保留 180 个交易日；拥挤度默认保留 2020 年以来的完整历史；微盘温度计保留 2021 年以来的五交易日采样历史。细节见 [抱团拥挤度说明](market-crowding.md) 与 [微盘温度计说明](microcap-temperature.md)。

微盘温度计使用独立的 `microcap_temperature` 表。采集器保存原始指标、当期历史分位、综合温度、累计指数和成分股；刷新最后一期时从该期之前的基准续算，避免重复计数。网页模型将成分股字段设为延迟加载，页面接口只读指标，不传输名单。网页不会在切换范围时重算分位或重新设置指数基期。

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
