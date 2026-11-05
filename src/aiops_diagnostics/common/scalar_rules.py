"""外部标量读取与存储时间的单一权威（#573）。

「一段外部数据读进来是什么意思」和「同一个时刻落库是什么文本」，曾经
由每个模块各自回答：时间解析有七份（`rules._as_datetime`、已删除的
`engine._datetime`、`health_report._datetime`、`gateway_runtime._as_datetime`、
`gateway_store._parse_time`、`conversation_store._parse_iso`、
`knowledge_retrieval._utc`），数值读取有三份，存储写法有五种。答案在
「naive 算不算 UTC」「`1.0` 算不算整数 1」「`None` 是抛还是空」上互相
矛盾——同一个 `created_time` 缺失，在六个实现里是「没有值」，在
`gateway_runtime` 里是把整个作业归成 ``SOURCE_UNAVAILABLE`` 且可重试的
``ValueError``。

现在只有这一处：

* ``as_datetime`` / ``as_integer`` —— 外部数据的**读取**规则。不可解析
  一律 ``None``，绝不抛异常。调用方据 ``None`` 决定自己语境里该怎么说
  「没有」。
* ``declared_timezone`` —— 「这段数据自己声明了哪个时区」，与
  ``as_datetime`` 的「没声明当 UTC」是两个问题；列级调和（同一行里
  另一列带偏移、naive 列认对方的区）用的是它。
* ``format_stored_time`` / ``parse_stored_time`` —— 同一个时刻落库只有
  一种文本：**UTC、带偏移**。写者与读者在同一处定义，不可能各自漂移。
  ``parse_stored_time`` 会抛——它读的是本模块写下的文本，读不出来意味
  着数据损坏，不是「没有值」。
* ``now_utc`` —— 「现在」也只有一处回答。

**不在这里的**（台账见 ``tests/common/test_scalar_rules.py``）：
``ops_analysis._iso_z`` 是 API 响应合同（``Z`` 后缀由合同定义）；
``health_report`` 报告体与 ``consumer_tool_grants``/``operator_cpms`` 的
wire 字段是对外形状；``chat_execution`` 与 ``operator_cpms`` 对
``observed_at`` 的校验要求文本**显式带偏移**——naive 补 UTC 会放宽它；
``consumer_time`` 是展示层容错；``station_pricing``/``station_tariff`` 的
``date.fromisoformat`` 解析的是日期类型与上游字段；``sources``/
``tdengine_proxy`` 的时间函数是 SQL 字面量规则；``node_capabilities``
的 ``observed_at`` 是 epoch 秒列，不是 ISO 文本。它们回答的是别的问题。
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo


def now_utc() -> datetime:
    """「现在」的唯一答案：UTC、带时区。"""
    return datetime.now(UTC)


def as_datetime(value: object) -> datetime | None:
    """把外部数据读成 ``datetime``；读不出一律 ``None``，绝不抛异常。

    * ``None`` 与非 ``str``/``datetime`` 类型 → ``None``；
    * ``datetime`` 原样返回，naive 视为 UTC；
    * ``str`` 去首尾空白、``Z`` 归一化为 ``+00:00`` 后按 ISO-8601 解析，
      naive 视为 UTC；解析失败 → ``None``。

    「不抛」是合同：历史上唯一会抛的那一份把缺失时间戳冒名成
    ``SOURCE_UNAVAILABLE`` 并把作业标成可重试——重试一次也不会成功。
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def as_integer(value: object) -> int | None:
    """把外部数据读成 ``int``；读不出一律 ``None``，绝不抛异常。

    接受 ``int``、整值 ``float``（``1.0`` → 1）、以及命名整数的十进制
    字符串（``"3"`` → 3、``"3.0"`` → 3）。``bool`` 不是整数——把
    ``True`` 读成状态码 1 没有任何依据。``"3.5"``、``"abc"``、``None``
    等一律 ``None``：非整值浮点串截断成整数是把坏数据说成好数据。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def declared_timezone(value: object) -> tzinfo | None:
    """这段外部数据**自己声明**的时区；没声明或读不出 → ``None``。

    与 ``as_datetime`` 的分工：``as_datetime`` 给「没声明就当 UTC」的兜底
    读数，本函数回答「它到底有没有声明、声明的是哪个」。当同一行里另一列
    显式带偏移、需要按对方的区解释 naive 墙钟时（``order_window`` 的混区
    列调和），需要的是后一个答案——naive 文本在 ``as_datetime`` 里已被
    盖上 UTC，到那时再问就来不及了。
    """
    if isinstance(value, datetime):
        return value.tzinfo
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).tzinfo
    except ValueError:
        return None


def format_stored_time(value: datetime) -> str:
    """同一个时刻落库的唯一文本：UTC、带偏移（``+00:00``）。

    naive 输入视为 UTC——与读取规则同一个答案，写下的文本永远带偏移，
    因此读回时「naive 视为 UTC」的分支对正常数据不可达。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def parse_stored_time(value: object) -> datetime:
    """把 ``format_stored_time`` 写下的文本读回 ``datetime``。

    **会抛 ``ValueError``**——与 ``as_datetime`` 的分工是：这里读的是
    本模块写下的文本，读不出来意味着数据损坏或合同被破坏，不是「外部
    数据没有值」。naive 文本视为 UTC（既有库里的旧写法可能无偏移，
    按同一规则读回）。
    """
    if not isinstance(value, str):
        raise ValueError(f"stored time must be ISO-8601 text, got {type(value).__name__}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
