"""Dify 登录代理（#772 B / #778）：用**公司账号**换一个 Dify 会话。

## 它替代了什么

运营今天要拿一个**与公司身份无关**的 Dify 邮箱密码登控制台：公司里谁该改智能体、
谁该发布到生产，Dify 都不知道；人员离职也不会反映到 Dify 账号上。这一层的职责是
把「谁是这个人」这件事**重新交给公司**：

    公司账号+密码+真实验证码
      → 代理亲自向公司 `/upms/token/login` 完成登录交换   （身份结论只来自这次交换）
      → 用这枚令牌读 `/upms/user/info` 的权限码
      → 没有编辑权限码 ⇒ 拒绝
      → 有 ⇒ 为该用户签一枚 **Dify 控制台会话**
      → 把会话交给浏览器，跳转控制台

## 会话是**自签**的，这是本决定的核心（见 ADR-0023）

Dify 1.17.1 的控制台会话是 `SECRET_KEY` 签的 HS256 JWT，逐请求只验签 + 取 `user_id`
查库，**不校验密码、不查会话存储**。所以代理只要持有 `SECRET_KEY` 就能为该映射账号
签发被 Dify 接受的会话 —— 而这正是被选中的方案：

* **为什么不用 Dify 自己的 `/console/api/login`**：它要密码，而 Dify 1.17.1 **没有
  任何接口能给一个账号设密码**（邀请创建时 `password=None`，激活只设资料，重置要邮件
  令牌）—— 代理无法为映射账号"准备"一个密码。自签绕开了整条密码链路。
* **代价（写进 ADR）**：代理持有 `SECRET_KEY` 等于能签**任何** Dify 账号（含 owner）
  的会话，且那把 key 同时保护 Dify 库里存着的工具密钥。代理被攻破 ⇒ 全部映射账号 +
  全部 Dify 侧凭据失守。缓解：只监听回环、经控制台入口同源反代、不对外开端口、
  密钥轮换步骤见 ADR。

## 信任模型：不接受浏览器带来的公司令牌

公司令牌在现状下**可被伪造**（自制 HS256、默认密钥、网关验签关闭）。因此代理
**不接受任何来自浏览器的公司令牌**；它只信任自己在本次登录中从 `/upms/token/login`
拿到的那一枚，且只用于当次读权限码。这条是整个身份闭环的前提，不是卫生习惯。

## 不落盘、不记日志的东西

公司密码、公司令牌、`SECRET_KEY` 都**不写盘、不进日志**。日志只记公司用户 id 与
结果码。映射表里也**没有密码** —— 自签模型下不存在"要保管的密码"。
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger("aiops.dify_login_proxy")

# ── 公司侧端点（相对公司根，由部署配置决定打到谁）───────────────────────────
COMPANY_LOGIN_PATH = "/upms/token/login"
COMPANY_CAPTCHA_PATH = "/code"
COMPANY_USER_INFO_PATH = "/upms/user/info"
#: 公司失败**不是** HTTP 状态码：登录失败也是 200 + `code:-1000` + 中文 msg。
_COMPANY_SUCCESS_CODE = 0

# ── 我们自己的路由 ──────────────────────────────────────────────────────────
CAPTCHA_ROUTE = "/__aiops/login/captcha"
LOGIN_ROUTE = "/__aiops/login"
LOGOUT_ROUTE = "/__aiops/login/logout"
#: 发布权查询（#780）。发布桥拿运营的 **Dify 会话**调它，问"这个账号能发布吗"。
#: 代理验自己签出的会话 → 从映射表取公司用户与该次登录的权限码 → 答是/否。
#: 放在代理上而不是让桥直接读映射表：**身份边界**（代理持 SECRET_KEY 与映射数据，
#: 桥不该持证据的汇总）。查询只读、只回一个布尔与审计用的发起人，不回权限码全集。
PUBLISH_CHECK_ROUTE = "/__aiops/publish/check"

#: 账号邮箱从公司用户 id **确定性**生成，且用不可投递的保留域名（RFC 2606 ``.invalid``）。
#: 确定性是为了"同一公司用户永远映射到同一个 Dify 账号"（用户故事 14）。
ACCOUNT_EMAIL_DOMAIN = "aiops-login.invalid"
#: Dify 账号姓名的长度上限（`ActivatePayload.name` max_length=30）。
_MAX_NAME_CHARS = 30
_SAFE_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DifyLoginError(RuntimeError):
    """代理自己的一步失败了。``code`` 是稳定的、非本地化的结果码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CompanyLoginRejected(DifyLoginError):
    """公司侧明确拒绝了这次登录（密码错、验证码错、账号停用/不存在）。"""


class CompanyUnreachable(DifyLoginError):
    """公司侧不可达或返回了无法解释的东西 —— 与"凭据不对"是两件事。"""


class PermissionDenied(DifyLoginError):
    """公司登录成功，但这个账号没有编辑智能体的权限码。"""


# ── 公司令牌的**结构**校验 ──────────────────────────────────────────────────
#
# 明写「只解析不验签」：公司令牌可伪造是已知前提，我们**从不**拿它当身份结论 ——
# 身份结论只来自"这次登录交换是否成功"。校验形状是为了拿到 user_id / username 去
# 读权限码，不是为了认证。把它当成认证才是错误用法。

_JWT_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


def decode_company_token(token: str) -> dict[str, Any]:
    """Decode the company login token's payload **without verifying** its signature.

    The docstring is the contract: this is a *shape* read. The token is trusted
    only because we obtained it in this very login exchange, never because the
    signature checked out — on this deployment a forged signature would pass any
    check we could perform anyway (the key is a source default and the gateway
    does not verify). Callers must not use this to authenticate anything.
    """
    parts = token.split(".")
    if len(parts) != 3 or not all(_JWT_SEGMENT.match(part) for part in parts):
        raise CompanyUnreachable("COMPANY_TOKEN_MALFORMED", "公司令牌不是三段式 JWT")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CompanyUnreachable("COMPANY_TOKEN_MALFORMED", "公司令牌载荷无法解析") from exc
    if not isinstance(payload, dict):
        raise CompanyUnreachable("COMPANY_TOKEN_MALFORMED", "公司令牌载荷不是对象")
    return payload


# ── 密码的 AES 编码（公司网关要求）──────────────────────────────────────────
#
# 公司网关的 `PasswordDecoderFilter` 会在**转发给 UPMS 之前**对 `/token/login` 的
# `password` 参数做 AES/CBC/NoPadding 解密，key 与 IV 都是 Nacos 里的
# `security.encode.key`。所以代理必须**客户端加密**，明文会被解成乱码（表现为登录失败）。
# 这一段是那个约定的忠实实现，不是"我们发明的编码"。


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def encrypt_company_password(plaintext: str, key: str) -> str:
    """AES/CBC + **PKCS7** padding, key == IV == ``security.encode.key``, base64 out.

    **Padding 上的一致性是这里唯一会静默出错的地方**，所以写清楚：公司前端用的是
    CryptoJS 默认（PKCS7）；服务端 `PasswordDecoderFilter` 的 `decryptAES` 用
    `AES/CBC/NoPadding` + `Base64.decode`。两侧能对上，是因为 PKCS7 的**完整填充块**
    在去掉 padding 后与 NoPadding 的"块内截断 + `trim()`"结果相同 —— 密码短于一个块、
    UTF-8 尾部恰好落在块内时，服务端 `trim()` 去掉的是 PKCS7 填进来的字节。
    ⇒ 用 PKCS7 而不是 CryptoJS 的 `ZeroPadding` 模式，是这里唯一能被两侧同时接受的选择。

    key 的长度由公司 Nacos 决定（16 字节）。长度不符直接拒：拿错 key 去解会得到一个
    看似成功的乱码，而那会表现为"密码错误"，把运营引向一个不存在的问题。
    """
    raw = key.encode("utf-8")
    if len(raw) not in (16, 24, 32):
        raise ValueError("公司 AES key 必须是 16/24/32 字节")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - 依赖缺失是部署错误
        raise RuntimeError("cryptography 未安装，无法加密公司密码") from exc
    encryptor = Cipher(algorithms.AES(raw), modes.CBC(raw)).encryptor()
    blob = encryptor.update(_pkcs7_pad(plaintext.encode("utf-8"))) + encryptor.finalize()
    return base64.b64encode(blob).decode("ascii")


# ── Dify 会话自签 ───────────────────────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_console_jwt(payload: Mapping[str, Any], secret_key: str) -> str:
    """HS256 JWT —— 与 Dify 的 `PassportService.issue` 逐字同形。

    手写而不是引一个 JWT 库：这段是**唯一**服务间的签名约定，把它摊开写出来，
    下一读者能直接对照 Dify 的 `libs/passport.py` 核对，而不是去翻某个库的默认值。
    """
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    body = _b64url(json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header}.{body}".encode("ascii")
    import hashlib
    import hmac as _hmac

    signature = _hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(signature)}"


def verify_console_jwt(token: str, secret_key: str) -> dict[str, Any] | None:
    """Verify one of **our own** console tokens (#780). ``None`` = not ours / invalid.

    This is the mirror of :func:`sign_console_jwt`, and it is only ever used on
    tokens this service itself minted. It is what lets the bridge ask "which Dify
    account is this session?" without holding the mapping database: the proxy
    verifies the session it issued and answers on the database's behalf.
    """
    import hashlib
    import hmac as _hmac

    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, body_b64, signature_b64 = parts
    expected = _hmac.new(
        secret_key.encode("utf-8"), f"{header_b64}.{body_b64}".encode("ascii"), hashlib.sha256
    ).digest()
    try:
        given = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    except (binascii.Error, ValueError):
        return None
    if not hmac.compare_digest(expected, given):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    return payload


@dataclass(frozen=True, slots=True)
class ConsoleSession:
    """一次成功的登录要交给浏览器的两枚令牌。

    ``access_token`` 发给浏览器当 cookie；``csrf_token`` 同时发 cookie 与
    `X-CSRF-Token` 头（Dify 的双提交）。两枚都是我们自签的，``repr=False`` 让它们
    不出现在任何 repr / 审计输出里。
    """

    access_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    cookie_prefix: str = ""
    access_max_age: int = 3600
    csrf_max_age: int = 3600

    def cookies(self, *, secure: bool) -> list[str]:
        """三枚 Set-Cookie：access（HttpOnly）、refresh（HttpOnly）、csrf（可读）。

        refresh 与 access 同值：Dify 的刷新路径只要求它是一枚合法的会话令牌，而自签
        模型下"刷新"就是再签一枚 —— 于是把同一枚当 refresh 下发，前端行为不变。
        """
        prefix = self.cookie_prefix
        attrs = "; Path=/; SameSite=Lax"
        secure_attr = "; Secure" if secure else ""
        tail = f"HttpOnly{secure_attr}{attrs}"
        return [
            f"{prefix}access_token={self.access_token}; {tail}; Max-Age={self.access_max_age}",
            f"{prefix}refresh_token={self.access_token}; {tail}; Max-Age={self.access_max_age}",
            f"{prefix}csrf_token={self.csrf_token}{secure_attr}{attrs}; Max-Age={self.csrf_max_age}",
        ]


def mint_console_session(
    account_id: str,
    *,
    secret_key: str,
    deployment_edition: str,
    access_expire_minutes: int,
    cookie_prefix: str = "",
) -> ConsoleSession:
    """为 ``account_id`` 签一枚 Dify 会接受的控制台会话（#772 B / ADR-0023）。

    载荷形状照抄 `AccountService.get_account_jwt_token_for_account_id`：
    ``{user_id, exp, iss, sub}``。``iss`` 必须是部署版本（社区版即 ``COMMUNITY``）——
    写错不会立刻报错，但下一个读 `iss` 的地方会拒。
    """
    now = int(time.time())
    exp = now + access_expire_minutes * 60
    access = sign_console_jwt(
        {"user_id": account_id, "exp": exp, "iss": deployment_edition, "sub": "Console API Passport"},
        secret_key,
    )
    csrf = sign_console_jwt({"exp": exp, "sub": account_id}, secret_key)
    return ConsoleSession(
        access_token=access,
        csrf_token=csrf,
        cookie_prefix=cookie_prefix,
        access_max_age=access_expire_minutes * 60,
        csrf_max_age=access_expire_minutes * 60,
    )
