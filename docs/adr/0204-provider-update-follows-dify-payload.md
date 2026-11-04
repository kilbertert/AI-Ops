# 204. 工具迁移遵循Dify更新载荷

状态：已采纳。日期：2026-11-04。

Dify 1.17.1的ApiToolProviderUpdatePayload要求credentials、icon和original_provider；
现有迁移仅传schema，真实更新返回422。更新时保留提供方图标、标签和说明，
cpms-node继续使用auth_type=none，业务凭据仍由单次任务授权提供。只有success回执才记迁移完成。
依据为固定版本官方控制器与管理服务，未修改Dify原接口校验。

## 被否掉的替代形态

不删掉再建提供方，避免既有应用引用失效；不手改Dify数据库；不将真实业务凭据写入提供方。
