"""订单标识的单一权威（#606）。

「什么样的一串字符是一个订单号」曾经由七个位置各自回答：三个 Pydantic
字段、网关常量、两个 store、传输层白名单——答案在「首字符可否为符号」
与「长度上限」上互相矛盾，同一个 ``.x1`` 在请求体被接受、进 store 又被
拒。现在只有本模块定义语法：

* ``ORDER_ID_ATOM`` 是唯一一份字母表与长度（合同承诺的形状：
  ``docs/standard-api-contract.md`` 允许字母、数字、下划线、点、冒号、
  连字符，上限 128）。
* 三个谓词共享它：``is_order_identifier`` / ``normalize_order_identifier``
  / 自由文本抽取。

**不在这里的**：``sources.SAFE_VALUE`` / ``SAFE_HTTP_PARAM`` 与
``tdengine_proxy.SAFE_VALUE`` 是传输层注入防护（SQL 字面量与 URL 插值），
不是标识符语法——它们的输入是已过本校验的值，不并入本权威。
``gateway_store.SAFE_SCOPE`` / ``conversation_store._SAFE_SCOPE`` 校验的是
scope 指纹等别的标识符，首字符必须为字母数字的那条规定属于它们自己。
"""

from __future__ import annotations

import re

#: 订单号的唯一字母表与长度（不含锚点）。Pydantic 字段、正则与可选包装
#: 都从这同一段字符串组装，保证任何地方看到的形状逐字相同。
ORDER_ID_ATOM = r"[A-Za-z0-9_.:-]{1,128}"

ORDER_ID_PATTERN = re.compile(f"^{ORDER_ID_ATOM}$")


def is_order_identifier(value: object) -> bool:
    """``value`` 是否是一个合法的订单号。非字符串一律不是。"""
    return isinstance(value, str) and ORDER_ID_PATTERN.fullmatch(value) is not None


def normalize_order_identifier(value: object) -> str | None:
    """剥掉首尾空白后按同一语法校验；不合法返回 ``None``。

    ``None``、空串、非字符串一律 ``None``——与 ``_scope`` 曾经给 ``order_no``
    的语义逐字相同（strip 后校验），调用方据 ``None`` 抛自己的错误。
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if ORDER_ID_PATTERN.fullmatch(candidate) else None


#: 自由文本里「像个订单号」的候选 token：6–64 位、首尾必须是字母数字
#: （边界字符不进标识符）。这条比 ``ORDER_ID_PATTERN`` 窄是故意的——它是
#: 扫描启发式，不是合法性判定；合法性仍由 ``ORDER_ID_PATTERN`` 回答。
_ORDER_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9_.:-]{4,62}[A-Za-z0-9](?![A-Za-z0-9])")


def order_identifier_candidates(text: str) -> list[str]:
    """自由文本里全部可信订单号，按出现顺序、去重。

    A candidate MUST carry at least one digit. Without that rule the scan
    returns the first ordinary WORD of a Latin-script question — `Please
    check charging anomalies for order (2098…)` yielded ``Please``, so the
    order was never found and the request silently degraded to a zero-order
    answer (41 live, 2026-09-20). Chinese hid the defect: CJK characters are
    outside the token class, so the scan skipped them and landed on the bare
    order number. Requiring a digit is what tells an identifier apart from a
    word; a real order number that carries none does not exist in this
    system.
    """
    if not text:
        return []
    found: list[str] = []
    for match in _ORDER_TOKEN.finditer(text):
        candidate = match.group(0)
        if not any(char.isdigit() for char in candidate):
            continue
        # Skip pure-digit tokens that are too short to be order ids and could
        # be phone numbers (<6 digits already excluded by length, but a long
        # pure-digit run like a phone would be 11 digits — still ambiguous;
        # order numbers in this system are >=15 chars). To be conservative,
        # only treat pure-digit runs >=15 as order candidates.
        if candidate.isdigit() and len(candidate) < 15:
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def extract_order_identifier(text: str) -> str | None:
    """自由文本里第一个可信订单号，没有则 ``None``。"""
    candidates = order_identifier_candidates(text)
    return candidates[0] if candidates else None
