# 文档目录

从[项目 README](../README.md)开始。按职责查阅，数据口径与部署步骤分别维护。

## 开发与工程

- [架构与扩展约定](development/architecture.md)
- [工程与页面规范](development/engineering.md)

## 运行与维护

- [缓存同步策略与历史修复](operations/cache-sync-strategies.md)
- [CCI 采集核验（2026-11-09）](operations/cci-collection-investigation.md)
- [CCI 数据更新与断档恢复](operations/cci-recovery.md)
- [配置管理](operations/configuration.md)
- [main 自动部署：一次配置，之后 push 自动发布](operations/github-auto-deployment.md)
- [Linux 运行与部署](operations/linux-deployment.md)
- [本地缓存与数据同步](operations/local-cache.md)
- [模块诊断日志](operations/logging-diagnostics.md)
- [微博、雪球服务器扫码登录](operations/social-browser-login.md)
- [System configuration](operations/system-configuration.md)
- [API worker 退出诊断](operations/worker-health-diagnostics.md)
- [全站账号与 IP HTTPS](operations/账号与HTTPS配置.md)
- [飞书通知配置](operations/飞书通知配置.md)

## 数据模块与采集

- [市场热度与抱团拥挤度：AShareHub 原始日行情](data/asharehub-market-data.md)
- [奶牛股](data/cash-cow.md)
- [聚宽批量采集](data/collector-batch.md)
- [行业动量：采集窗口与历史保留](data/collector-retention.md)
- [大宗商品：长江现货 A00铝](data/commodities.md)
- [高股息选股](data/dividend-selection.md)
- [净利润断层观察名单](data/earnings-gap.md)
- [国家队动态：页面配置与采集](data/etf-collection.md)
- [ETF 交易所采集器](data/etf-exchange-collector.md)
- [ETF 历史：聚宽平台手动补采](data/etf-joinquant-history.md)
- [ETF 动量：历史恢复的边界与独立档案](data/etf-momentum-history-recovery.md)
- [ETF/LOF 动量热度](data/etf-momentum.md)
- [ETF 分类去重研究脚本](data/etf-selection.md)
- [ETF 份额与合计流通市值](data/etf-shares.md)
- [创红比与科创红利比](data/growth-dividend.md)
- [高增长](data/high-growth.md)
- [行业动量与微盘新高新低：聚宽完整历史恢复](data/industry-breadth-history-recovery.md)
- [个股机构持仓与社保新进](data/institution-ownership.md)
- [聚宽历史采集与恢复范围](data/joinquant-history-coverage.md)
- [聚宽市场情绪与估值标尺历史补采](data/joinquant-market-valuation-history.md)
- [两融平均维持担保比例](data/margin-guarantee.md)
- [抱团拥挤度：采集与展示](data/market-crowding.md)
- [市场指标每日采集实现与验收边界](data/market-daily-collection.md)
- [市场热度与抱团拥挤度：公开数据 API 核查](data/market-heat-public-api.md)
- [市场热度](data/market-sentiment.md)
- [微盘新高新低](data/microcap-breadth.md)
- [微盘温度计历史恢复](data/microcap-history-recovery.md)
- [微盘温度计：采集与展示](data/microcap-temperature.md)
- [机构持仓全历史补洞](data/ownership-history-recovery.md)
- [个人持仓（管理员）](data/personal-holdings.md)
- [计算入库与历史兼容](data/raw-pipeline.md)
- [高股息选股与高增长：当前名单和历史档案恢复](data/selection-history-recovery.md)
- [博主动态监控](data/social-monitor.md)
- [估值标尺](data/valuation-ruler.md)

## 工具入口

- [采集脚本清单](../collectors/README.md)
- [运行与维护命令](../scripts/README.md)
