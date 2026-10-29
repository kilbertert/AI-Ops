# 删除无生产调用的诊断配置别名

Status: Superseded by 0158

本记录为上一轮窄范围决定，已被 [0158](0158-retire-python-runtime.md) 的完整退役授权替代。

当时用户确认只删除已无依赖的旧代码，仍在运行的 Thin Harness 列出迁移清单。
仓内调用核查表明，`DiagApiSettings` 无代码引用，`Settings.diag_api` 与
`AgentSettings.provider_names()` 仅被测试调用；现役代码直接使用 `HttpSettings`、
`Settings.http` 和 `providers`。删除这三处并将测试移至真实配置，不留空壳兼容。

## 被否掉的替代形态

- 整体删除 Thin Harness：能显著减小代码，但 C端检测、CLI、Dify 试用及多个共享消费者仍可达，
  超出本次授权，且不能用 Java 两工具证明全部旧能力已被替代。
- 保留所有旧别名到整个引擎退役：可保留未知外部 Python 导入，但仓内只会继续维护两套名称；
  本次明确清理无生产消费者的内部包装，保留有配置或输出合同意义的兼容面。

## 后果

HTTP 数据源、环境变量回退、脱敏输出及 provider 顺序不变。若仓外代码直接导入旧别名，
需要改用现役名称；本次仓内扫描不能证明不存在这种外部使用。
不改变公开 HTTP 路由、生产配置或历史证据。目录目标与剩余消费者见
[迁移清单](../architecture/repository-layout.md)，其中目标目录仍为建议。
