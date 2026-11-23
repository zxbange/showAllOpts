# 高股息规则版本字段扩容

公开采集规则 `dividend_dps_proposal_age3_annual_history_v10` 长 45 字符，旧表的
`rule_version VARCHAR(32)` 不足。严格 SQL 模式可能拒绝写入；非严格模式可能
截断并导致本地完整快照校验失败。旧的 `sync_local ValueError` 和 `cache ... rows=71` 是历史日志示例，不是当前线上状态。单凭前者不能证明故障就是截断；先核对实际列宽或查看 `metadata_fields` 诊断。已是 `VARCHAR(64)` 或更宽时无需再扩容。

## 发布与恢复

- 新表使用 `VARCHAR(64)`；公开采集及独立采集脚本对旧 MySQL 字段只扩容，不缩短。
- 扩容位于快照替换事务之前。DDL 会隐式提交，不属于随后快照事务的回滚范围。
- 扩容失败时不继续替换快照。公开采集保留 durable pending；问题修复后重试先发布
  已保存数据，不需要清空 SQLite 或重新下载。休市日手动失败例外：结果移入隔离表，
  后续自动任务不重放；没有更新成功快照时可由手动重试恢复，隔离数据不会直接丢弃。
- 撤回应用版本不应把列缩回 32，否则已写入的长版本可能丢失；旧代码可继续读取更宽列。
- `cache ... rows=71` 是快照表及股票表行数之和，与 1 + 50 + 20 相容，但不能证明元数据正确或本地核验通过。

## 权限及运维边界

生产 MySQL 版本、字符集、字段自定义属性、账号权限和长事务未通过本地测试确认。
自动扩容需要目标表的 ALTER 权限（MySQL 文档还要求 CREATE、INSERT）；没有权限时
应由管理员按变更流程预扩容，不为此给应用账号授予全局权限。

管理员应先检查 `SHOW CREATE TABLE dividend_selection_snapshot`、账号权限与活跃事务，
保留任何自定义列属性，按实际数据库版本评估并执行扩容。项目默认字段的目标定义为：

```sql
ALTER TABLE dividend_selection_snapshot
  MODIFY COLUMN rule_version VARCHAR(64) NOT NULL;
```

即使表只有一条快照，ALTER 仍可能等待 metadata lock。utf8mb4 下 32→64 字符可能
跨越 VARCHAR 255 字节长度编码边界，不保证 INPLACE、无锁或即时完成。应在维护窗口
处理长事务并监控锁等待。不要仅因客户端超时就推断 DDL 没执行；重试会重新检查列宽。

测试使用 SQLite 和合成 MySQL inspector/权限异常，覆盖保留旧结果、pending 重试及
业务事务回滚；不是对生产 MySQL 权限或真实 DDL 时长的验证。

参考：
- https://dev.mysql.com/doc/refman/8.4/en/privileges-provided.html
- https://dev.mysql.com/doc/refman/8.4/en/implicit-commit.html
- https://dev.mysql.com/doc/refman/8.4/en/metadata-locking.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-online-ddl-operations.html
