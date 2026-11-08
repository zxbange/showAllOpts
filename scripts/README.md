# 项目运行与维护工具

本目录保留环境初始化、启动、部署和故障排查工具。采集入口在 [collectors](../collectors/README.md)，采集公共代码同步工具在 `collectors/tools/`。

## 启动与部署

| 文件 | 用途及入口 |
| --- | --- |
| `setup.ps1` | Windows 初始化依赖，调用 `configure_database.py` |
| `setup.sh` | Linux 初始化依赖、生产配置并构建前端 |
| `configure_database.py` | 初始化或校验配置，保留已有配置；由两个 setup 脚本调用 |
| `dev.py` | 启动前后端，由根目录 `start.cmd`、`start.ps1`、`start.sh` 调用 |
| `windows_job.py` | 被 `dev.py` 调用，Windows 启动器退出时回收子进程 |
| `build.sh` | 单独安装前端依赖并构建，供手动更新使用 |
| `deploy.py` | 发布、健康检查和失败恢复，由 `deploy.sh` 及自动部署入口调用 |
| `deployment_impact.py` | GitHub Actions 判断本次变更是否需要部署 |

## 数据库与缓存维护

| 文件 | 用途 |
| --- | --- |
| `check_database.py` | 只读检查数据库连接和行业动量表；发布准备阶段也会调用 |
| `refresh_cache.py` | 不启动网页，手动查看缓存状态或同步，可指定模块及全量重建 |
| `repair_ownership_classification.py` | 检查并修复历史机构持仓分类；默认预览，`--apply` 才写入，支持限定股票 |

分类修复工具读取 `collectors/个股机构持仓_数据库版.py` 的定义并复用分类逻辑，不请求聚宽。它是项目维护工具；聚宽采集脚本不会反向调用它。

## 博主监控浏览器维护

| 文件 | 用途 |
| --- | --- |
| `install_social_browser.py` | 将 Chromium 安装到服务使用的目录 |
| `check_social_browser.py` | 使用临时配置和内联页面检查浏览器启动、显示环境；不访问平台或已有登录配置 |
| `check_xueqiu_response.py` | 使用已保存凭据发起一次雪球接口只读请求，分析响应类型 |
| `check_xueqiu_browser.py` | 使用临时浏览器上下文和已保存凭据进行访问对照，复用上一个脚本的响应判断逻辑 |

三个排查脚本用途不同，按故障情况手动运行，不属于日常采集任务。运行账号与配置应和服务一致，具体命令见[浏览器登录排查](../docs/social-browser-login.md)和[博主监控说明](../docs/social-monitor.md)。

`__init__.py` 用于包导入；`__pycache__/` 是 Python 自动生成的缓存，不是需要维护的脚本，不提交 Git。
