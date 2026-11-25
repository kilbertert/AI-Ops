"""跨 Gateway 与 Dify 独立旁路服务共享的平台哨兵常量。

仅保留无外部依赖的纯字符串常量，供 Gateway（``dify_workspaces``）、控制台发布桥
（``dify_console_bridge``）与登录代理（``dify_login_proxy``）共同引用，彻底消除
原 ``dify_console_publish`` 因受限打包被迫复制常量以及 ``PLATFORM_TENANT_ID``
跨领域重名歧义（与快捷动作 ``SHORTCUT_PLATFORM_SCOPE = "__platform__"`` 严格区分）。
"""

from __future__ import annotations

#: 平台账号在 Dify workspace 目录里的哨兵租户键（对应 ``dify_workspaces.tenant_id='platform'``）。
DIFY_PLATFORM_WORKSPACE_ID = "platform"

#: 公司用户体系中平台管理员的 ``sys_user.type`` 字符串值。
PLATFORM_USER_TYPE = "-1"
