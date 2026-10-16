# 架构与扩展约定

## 请求流向

```text
浏览器 React 页面
  → frontend/src/api/client.ts
  → Vite / Nginx /api 代理
  → FastAPI 路由：校验查询参数
  → Service：确定快照日期、计算区间指标、标记行业缺失
  → Repository：参数化 SELECT
  → 现有 MySQL industry_momentum 表
```

前后端独立进程、独立依赖、独立构建。后端不返回网页，前端不直接访问数据库。数据库凭据只由后端从环境变量或选中的配置文件读取：Windows 启动器及直接运行 Python 未指定配置时使用 `backend/.env`；Linux `start.sh`、systemd 和 Compose 默认选择项目根目录下的 `config/app.config`。配置优先级与迁移方式见 [配置管理](configuration.md)。所有查询通过 SQLAlchemy 表达式绑定参数，不拼接用户输入的 SQL。

## Linux 部署约定

当前部署目录为 `/home/ubuntu/showAllOpts`，systemd 服务名为 `showallopts-api`，运行账号和组为 `ubuntu`。基础 service 配合本机 `99-ubuntu-home.conf` 覆盖配置运行，最终读取 `config/app.config`，写入项目 `log/`。`ProtectHome=read-only` 允许读取家目录中的代码，`ReadWritePaths` 允许写入日志目录。

Nginx 从 `/var/www/showallopts` 提供前端静态资源，将 `/api` 代理到 `127.0.0.1:8000`。前端资源发布与后端进程启停分别管理。安装、权限、服务配置和升级步骤见 [Linux 部署指南](linux-deployment.md#4-systemd--nginx-后台服务)。

## 数据一致性

快照先确定分类内最大日期，再读取该日期全部行业，不把不同行业各自的最新日期拼成一张图。历史请求带快照日期，防止浏览过程中采集程序更新导致上下文变化。每次请求使用独立 Session，关闭时结束只读事务。

接口排序使用动量和行业代码；显示排名根据排序重新编号。原始采集排名仍用于历史点信息。零分行业始终保留。

数据库没有独立的交易日历或采集完成标志。历史缺失检查仅基于该分类有数据的日期，不能判断全市场共同缺失日，也不能保证某个最新快照已经完整采集。应在采集程序解决数据质量问题；网页不推断或伪造分数。

## 扩展新模块

1. 在后端增加对应 model / repository / service / schema / router；业务不堆叠到 `main.py`。
2. 在 `/api/v1/modules` 注册模块元信息。
3. 在前端增加页面与 API 类型，注册路由和导航，首页增加模块入口。
4. 增加必要的查询边界测试及浏览器用户流程测试。

当前模块只有申万二级行业动量，其他模块占位不提供虚假的可用入口。

## 错误、并发与浏览器行为

- 后端数据库异常统一返回 503，不向浏览器返回 SQL、驱动异常全文或连接串。
- 前端有加载、空数据、搜索无结果、断网/查询失败、未知页面状态。
- 切换行业/时间范围时取消旧请求，防止较慢请求覆盖新结果。
- ECharts 实例在组件卸载时销毁，容器变化通过 ResizeObserver 调整。
- 热力图单元格使用原生链接，可键盘操作和新标签打开；图表尊重减少动态效果偏好。
- 浏览器只使用 JSON API；所有文字通过 React 渲染，图表 tooltip 使用 canvas richText，避免拼接 HTML。

## 本地开发依赖

`frontend/package-lock.json` 锁定 Node 依赖；后端 `requirements.txt` 定义运行依赖范围，`requirements-dev.txt` 增加测试依赖，`requirements.lock.txt` 记录本次验证环境的精确依赖版本（含开发依赖）。
