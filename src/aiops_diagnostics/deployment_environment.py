"""部署身份里的**环境标识**：判据只在这里实现一次（ADR-0059 / ADR-0061）。

## 它是什么

一个部署（41、95…）的环境标识由**它自己的部署身份**声明 —— 公司的 JWT 里没有这个字段
（ADR-0059 已全量核实），所以它只能来自配置：

* 网关：``AIOPS_GATEWAY_ENVIRONMENT``（必填，无默认值）；
* 登录代理：``AIOPS_LOGIN_PROXY_ENVIRONMENT``（必填，无默认值）。

两者**必须按同一条判据**校验。各写一份的后果不是"风格不统一"，而是**漂移**：一处拒绝的
值另一处照收，于是同一个部署的两个组件对"我在哪个环境"给出不同答案，而
``dify_workspaces`` 恰好按这个值分区。那种漂移的表现是"某个租户的工作区有时找得到、
有时找不到"。

## 为什么不归一化

第一版对环境做了与其它标识相同的"把不安全字符换成 ``-``"。那会造出一个新的撞号方式：
``prod.41`` 与 ``prod/41`` 归一化后都是 ``prod-41`` —— 两个部署可以配置出两个不同的
字符串，却仍然落到同一批账号与同一批工作区。所以这里**只校验，不修改**：不合规就报错，
让部署者改配置，而不是替他猜。

错误类型由调用方决定（网关是启动错误 ``ValueError``，登录代理包成自己的
``DifyProvisionError``）；**判据本身只有这一份**。
"""

from __future__ import annotations

import re

#: 环境标识的规范形式：小写字母/数字/``-``/``_``，以字母或数字开头结尾，≤32 字符。
ENVIRONMENT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?$")

#: 校验失败时给人看的那句话。两个入口共用，读者不必猜"这个报错是另一处判据吗"。
ENVIRONMENT_FORMAT_MESSAGE = "环境标识必须是规范形式（小写字母/数字/`-`/`_`，以字母或数字开头结尾，≤32）"


def canonical_environment(value: str) -> str:
    """返回**已经是**规范形式的环境标识；不是就抛 ``ValueError``。

    **不做任何字符替换** —— 理由见模块 docstring。两侧空白是配置文件的噪声，剥掉它
    不改变含义（``" 41"`` 与 ``"41"`` 是同一次部署），这不构成归一化。
    """
    normalized = (value or "").strip()
    if not ENVIRONMENT_PATTERN.match(normalized):
        raise ValueError(ENVIRONMENT_FORMAT_MESSAGE)
    return normalized


def require_deployment_identity(environment: str) -> str:
    """起监听之前必须回答"我在哪个环境"；没有答案就**不起**。

    必备性单独一个函数，是因为**"起监听"与"构造一个 settings"是两件事**：测试会构造
    几十个 settings（它们不是部署），而每个真正的启动入口 —— `aiops-gateway serve`、
    M2 隔离栈的两个 launcher —— 都必须过这一道。把必备性写进 `validate()` 会让前者
    全部失败，把必备性只写在 `serve` 里会让后者绕过它（评审指出：两个 launcher 正是
    这样绕过去的）。
    """
    if not environment:
        raise ValueError("AIOPS_GATEWAY_ENVIRONMENT is required")
    return canonical_environment(environment)
