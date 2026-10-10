"""`deploy/d4-cutover.py` 的 `/v1/dify/` 那一跳（#614）。

这一段配置决定**Dify 能不能带着它自己的 API Key 打到适配路由**：

* 原来的 `location ^~ /v1/` 用 AI-Ops 自己的服务令牌**覆盖** `Authorization`
  （`proxy_set_header Authorization $aiops_auth;`），而 Dify 的 External Knowledge API
  **只支持 Bearer 自带 key** —— 它的 key 被覆盖掉，回 403 `DIFY_CREDENTIAL_REJECTED`
  （2026-10-09 公网实测）。
* 修法是给 `/v1/dify/` 一条**更长的前缀** location：nginx 按"最长前缀优先"选它，
  于是 `Authorization` 保留客户端带来的那个；同时**不** include 服务令牌 conf、
  **不**注入入口/来源密钥/会话 —— Dify 不是我们的用户。

这个文件证三件事，各自对应一种会静默出错的形态：

1. **生成的块是对的**（保留 Authorization、不带服务令牌、上游固定）。
2. **位置不依赖书写顺序**——它靠前缀长度取胜，所以断言"更靠前"是**为了可读**，
   断言"前缀更长"才是判据。两条都写，因为读的人很容易以为顺序在起作用。
3. **幂等**：`apply` 跑第二次不能把块叠起来，也不能留下旧的免责声明注释。

判据在真文件上驱动 —— 测试里带一份从 41 `nginx -T` 取回的 vhost 片段（`_VHOST_SAMPLE`），
形状与线上一致（真实的 `location` 行是顶格的）。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _load():
    """按路径导入 —— `deploy/` 不是包（先例：`tests/test_derive_zh_hant_tool.py`）。"""
    spec = importlib.util.spec_from_file_location("d4_cutover", ROOT / "deploy" / "d4-cutover.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


#: 41 上 `nginx -T` 取回的 rewrite 片段（2026-10-09），去掉无关的 vhost。
#: `location` 顶格、前缀带 `^~` —— 形状与线上一致，替换逻辑就是按这个形状写的。
_VHOST_SAMPLE = """\
# AI-Ops consumer API compatibility route: H5 uses api.mall.qushiyun.com.
# 2026-09-29：按入口分流（#448 / ADR-0009）。客户端仍拿服务令牌；管家端透传用户 JWT + 注入来源密钥。
# AI-Ops 入口：按内容域分流（#448 / ADR-0009；D-4 于 2026-09-30 改向）。
#   consumer  → 直连 AI-Ops（172.18.0.1:8788）：服务令牌 + 会话，行为逐字不变
#   operator  → 先经公司网关（127.0.0.1:30899 → cloud-gateway），由它注入来源密钥
# 变量式 proxy_pass 不会自动拼 URI，`rewrite … break` 是必需的。
location ^~ /v1/ {
    include /etc/aiops-41/nginx-aiops-service-token.conf;   # 只 set 变量，不直接设头
    proxy_pass $aiops_upstream;
    rewrite ^/v1/(.*)$ /v1/$1 break;
    proxy_set_header Authorization      $aiops_auth;
}

location ~* ^/(erp|qm|das)  {
	proxy_pass http://back_server;
}
"""


def _blocks(src: str) -> dict[str, str]:
    """每个 `location` 块的正文，按 location 行取。"""
    found: dict[str, str] = {}
    for match in re.finditer(r"^location ([^{]+)\{", src, re.MULTILINE):
        start = match.end()
        end = src.index("\n}", start)
        found[match.group(1).strip()] = src[start:end]
    return found


def test_the_dify_block_keeps_the_clients_authorization() -> None:
    """判据一：保留 Dify 自带的那把 key。这正是打不通的原因。"""
    module = _load()
    out = module.replace_location(_VHOST_SAMPLE)
    dify = _blocks(out)["^~ /v1/dify/"]
    assert "proxy_set_header Authorization      $http_authorization;" in dify, (
        "Dify 那一跳必须原样带上客户端（Dify）的 Authorization"
    )
    assert "$aiops_auth" not in dify, "不得用我们的服务令牌覆盖 Dify 的 key"


def test_the_dify_block_is_narrower_than_the_general_one() -> None:
    """判据二：**前缀更长**才是取胜的原因 —— 位置只是可读性。

    nginx 的 location 选择是"最长前缀优先"，与书写顺序无关。这里两条都断言，
    是因为读这段配置的人很容易以为"写在前面"起了作用，然后在某次重排里把它挪走
    （挪走其实无害），或反过来把它挪到 `/v1/` 里（有害）。
    """
    module = _load()
    out = module.replace_location(_VHOST_SAMPLE)
    assert "location ^~ /v1/dify/ {" in out and "location ^~ /v1/ {" in out
    assert out.index("location ^~ /v1/dify/ {") < out.index("location ^~ /v1/ {"), (
        "Dify 块应当写在 /v1/ 之前（可读性；真正的判据是前缀更长）"
    )
    assert len("/v1/dify/") > len("/v1/"), "前缀必须更长，否则 nginx 不会选它"


def test_the_dify_block_carries_none_of_the_caller_chain_injection() -> None:
    """Dify 不是我们的用户：不该拿到入口、来源密钥、会话那些注入。

    把调用者链的头注入进去会让下游以为这是某个真实用户发来的请求 ——
    而这条路由的整个设计前提就是"它不是调用者"。
    """
    module = _load()
    dify = _blocks(module.replace_location(_VHOST_SAMPLE))["^~ /v1/dify/"]
    for header in ("X-AIOps-Source-Key", "X-Business-Entry", "X-Third-Session"):
        assert header not in dify, f"Dify 那一跳不得注入 {header}"
    assert "nginx-aiops-service-token.conf" not in dify, "不得 include 服务令牌 conf —— 那是调用者链的凭据"
    assert "proxy_pass http://172.18.0.1:8788;" in dify, "上游固定直连 AI-Ops，与入口无关"


def test_reapplying_does_not_stack_blocks_or_leave_the_old_disclaimer() -> None:
    """判据三：幂等。

    `apply` 是可以重跑的（回滚、重放、hand-off 之后再来一次）。重跑若把块叠起来，
    nginx -t 未必报错（重复的 location 是合法的），但文件会越长越看不懂；
    旧的两行免责声明若留在原地，还会说"按入口分流"——而 Dify 这一跳根本不分流。
    """
    module = _load()
    onced = module.replace_location(_VHOST_SAMPLE)
    twice = module.replace_location(onced)
    thrice = module.replace_location(twice)
    assert onced == twice == thrice, "重跑改动了输出 —— 不是幂等"
    assert onced.count("location ^~ /v1/dify/ {") == 1
    assert onced.count("location ^~ /v1/ {") == 1
    assert onced.count("# AI-Ops 入口：按内容域分流") == 1
    # 文件开头的总说明在 umbrella 之上，不在被替换的范围里 —— 不该被动到。
    assert "H5 uses api.mall.qushiyun.com" in onced
    # 紧挨着 `location ^~ /v1/` 上方的那段说明必须是本文件写的 umbrella，
    # 不是原来那两行（它们已被 umbrella 取代；留着会说"按入口分流"，
    # 而 `location ^~ /v1/` 的上游是分流过的，Dify 那一跳不是）。
    before_v1 = onced[: onced.index("location ^~ /v1/ {")].rstrip().splitlines()
    assert before_v1[-1] == "# 变量式 proxy_pass 不会自动拼 URI，`rewrite … break` 是必需的。"
    assert "# AI-Ops 入口：按内容域分流（#448 / ADR-0009；D-4 于 2026-09-30 改向）。" in before_v1


def test_the_other_locations_are_untouched() -> None:
    """替换只碰 `/v1/` 那一处：其余 location 逐字不变。

    `replace_location` 用 `src.index("\\nlocation ")` 找一个块的结尾，
    这个写法对"块尾紧跟下一个 location"的形状成立。换一个形状（块之间夹注释）
    会让它多吃掉一段 —— 所以这里用带后续 location 的样本钉住。
    """
    module = _load()
    out = module.replace_location(_VHOST_SAMPLE)
    before = _blocks(_VHOST_SAMPLE)
    after = _blocks(out)
    for name, body in before.items():
        if name == "^~ /v1/":
            continue
        assert after[name] == body, f"{name} 被改动了"
    assert set(after) - set(before) == {"^~ /v1/dify/"}, "只应新增 Dify 这一条"


# ── #649：会话头的两种拼写都要认 ─────────────────────────────────────────────


def test_the_session_header_map_normalises_both_spellings() -> None:
    """#649：`$http_third_session` 只匹配 `third-session`，对 `X-Third-Session` 取空值。

    那个空值让头被丢掉、网关没有会话、回 401 `INVALID_ACCESS_TOKEN` —— 与「会话过期」
    **同形**。而两种拼写**都已经被写进仓库**（客户端文档写前者、本文件与网关参数名写
    后者），所以入口要两种都认，而不是只在文档里写对一种。

    这里断言的是**映射的形状**：一个变量、两种输入，且两个都空时仍是空（不伪造会话）。
    nginx 的 map 求值本身由 41 上的真机验收覆盖（本仓无法在 CI 里跑 nginx）。
    """
    module = _load()
    text = module.MAP_TEXT
    assert 'map "$http_third_session:$http_x_third_session" $aiops_third_session {' in text, (
        "两种拼写必须归一到一个变量"
    )
    assert text.count("map ") == 5, "本文件现在应有 5 个 map（入口/上游/鉴权/来源/会话）"
    body = text[text.index("$aiops_third_session") :]
    assert "default    $http_third_session;" in body, "默认取小写拼写"
    assert '"~^:(.+)$" $1;' in body, "前者为空、后者有值 ⇒ 取后者"


def test_the_general_location_does_not_read_the_raw_header_again() -> None:
    """反向：`proxy_set_header` 必须用归一后的变量。

    留着 `$http_third_session` 会让 map 形同虚设 —— 而配置里**看不出**这件事，
    因为两种写法都对 nginx 合法、只有一种会丢头。这就是这个用例存在的理由。
    """
    module = _load()
    blocks = _blocks(module.LOCATION_TEXT)
    general = blocks["^~ /v1/"]
    assert "proxy_set_header X-Third-Session    $aiops_third_session;" in general
    assert "$http_third_session" not in general, "不得再直接读裸头 —— 那会绕过归一"
    dify = _blocks(module.DIFY_LOCATION_TEXT)["^~ /v1/dify/"]
    assert "X-Third-Session" not in dify, "Dify 那一跳不注入会话（它不是我们的用户）"


def test_the_umbrella_comment_has_one_definition() -> None:
    """umbrella 的文本只写一次：`replace_location` 认它，生成的正文里也有它。

    两处各写一遍时，改一处漏一处会让 `replace_location` 找不到锚点而**退到
    `location ^~ /v1/ {` 兜底** —— 那时它会漏掉 umbrella 这一段，把注释留在原地，
    而 nginx 仍然能起来（只是说明与实现不一致）。
    """
    module = _load()
    assert module.LOCATION_TEXT.startswith(module.V1_UMBRELLA), "正文由同一个常量拼出来"
    out = module.replace_location(_VHOST_SAMPLE)
    assert out.count(module.V1_UMBRELLA.splitlines()[0]) == 1, (
        "替换后 umbrella 应恰好出现一次 —— 出现两次说明锚点没命中、旧注释留在了原地"
    )


def test_an_already_applied_vhost_is_reconciled_not_swallowed() -> None:
    """已经 apply 过的 vhost：不能被吃掉一块、也不能把块挪来挪去。

    这是 #649 那次改动**在真机上试出来**的一个缺陷：`replace_location` 原来用
    "到下一个 `\nlocation `" 找块尾，而它在 41 的真实 vhost 上会遇到**已经存在**的
    Dify 块 —— 于是那个块被吞掉再原样吐出、夹在它和 `/v1/` 之间的东西一起被吃掉。

    判据是**幂等 + 只多一条 + 邻居字节不变**三条一起：只测幂等会漏掉「吞掉再吐出」
    （那也幂等），只测数量会漏掉位置漂移。
    """
    module = _load()
    # 一个"已经 apply 过一次"的文件：Dify 块在上、umbrella 在下、后面还有别的 location。
    applied = module.replace_location(_VHOST_SAMPLE)
    assert applied.count("location ^~ /v1/dify/ {") == 1

    again = module.replace_location(applied)
    assert again == applied, "二次 apply 必须逐字不变"

    # 中间的邻居（那个 `location ~* ^/(erp|qm|das)`）在任何一次替换后都不得消失。
    assert again.count("location ~* ^/(erp|qm|das)") == 1
    assert _blocks(again)["~* ^/(erp|qm|das)"] == _blocks(_VHOST_SAMPLE)["~* ^/(erp|qm|das)"]
