"""登录页与 nginx 接线的静态判据（#783 的部署层）。

登录页是**静态资产**，本仓没有前端构建，所以它不会被任何单测跑到 —— 而它接的是
公司口令，是这一整条链上最不能出错的一页。这个文件用「读文件 + 断言内容」把它钉住
（先例：`tests/test_dify_entry_cutover.py`）。

这里判的是**形状**，不是渲染结果：真正的验收是浏览器里走一遍（见 docs/validation.md）。
静态判据拦的是"下一版把凭据写进 localStorage""加了一条第三方 script"这类回归 ——
它们在浏览器里看起来完全正常。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PAGE = ROOT / "ops" / "dify-login-proxy" / "dify-login-page.html"
PROXY_CONF = ROOT / "ops" / "dify-login-proxy" / "dify-login-proxy.conf"
CONSOLE_CONF = ROOT / "ops" / "dify-console-bridge" / "dify-console-10008.conf"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_login_page_is_a_single_self_contained_file(page: str) -> None:
    """单文件、无外链：它要在 `location = /signin` 上被一个 alias 直接吐出去。"""
    assert page.lstrip().startswith("<!DOCTYPE html>")
    assert "<style>" in page and "<script>" in page
    # 外部资源一个都不要 —— 每一条都是可以被动手脚的信任边。
    assert 'src="http' not in page and "src='http" not in page
    assert 'href="http' not in page and "href='http" not in page


def _executable(page: str) -> str:
    """去掉 HTML 注释与 `<style>`，只留会被浏览器执行的标记。

    注释里会**提到**这些 API 名字（说明为什么禁止用它们），那不构成违规 ——
    判据要落在代码上，否则这条测试会逼着文档不许提事。
    """
    import re

    without_comments = re.sub(r"<!--.*?-->", "", page, flags=re.DOTALL)
    return re.sub(r"<style>.*?</style>", "", without_comments, flags=re.DOTALL)


def test_the_login_page_never_stores_credentials(page: str) -> None:
    """不落 storage、不写 document.cookie —— 这一页接的是公司口令。"""
    code = _executable(page)
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert forbidden not in code, f"登录页出现 {forbidden}：凭据可能被留在浏览器里"


def test_the_login_page_calls_only_the_agent_login_routes(page: str) -> None:
    """它只跟同源的那两条路由说话，且不发第三方请求。"""
    assert 'fetch("/__aiops/login"' in page
    assert "/__aiops/login/captcha?randomStr=" in page
    assert "eval(" not in page


def test_the_login_page_posts_the_validated_redirect_through(page: str) -> None:
    """`redirect_url` 从查询串取、随请求体交给服务端**校验**，页面自己不做第二遍判断。

    判据是"页面把它发出去了"而不是"页面自己判断" —— 白名单必须在服务端
    （`dify_login_server.safe_redirect_path`），页面能被绕过。
    """
    assert "redirect_url" in page
    assert "redirect: redirect" in page
    assert "window.location.assign(result.data.redirect" in page


def test_the_captcha_can_be_refreshed_by_clicking(page: str) -> None:
    """点图刷新验证码（提交失败后也必须换一张：同一个 randomStr 用第二次会失败）。"""
    assert 'captcha.addEventListener("click", refreshCaptcha)' in page
    # 失败分支里也要刷新 —— 少了它，重试会一直说"验证码错"。
    assert page.count("refreshCaptcha();") >= 3


# --------------------------------------------------------------------------- #
# nginx 接线                                                                   #
# --------------------------------------------------------------------------- #


def test_the_login_proxy_routes_are_wired_into_the_console_entry() -> None:
    """登录代理的三条路由 + 发布检查，挂进 **10008** 那一层。

    为什么必须是 10008 而不是 TLS 层：10008 被 `dify.ranlei.work:443` 与明文
    `:10008` **两个入口共用**。挂在 TLS 层等于给明文入口留一条绕过口 —— 而收口
    Dify 原生登录恰恰要覆盖那个入口。
    """
    conf = CONSOLE_CONF.read_text(encoding="utf-8")
    assert "dify-login-proxy.conf" in conf


def test_the_login_page_takes_over_the_native_signin_path() -> None:
    """`location = /signin` 指到我们的登录页 —— 这是"运营看到的不是 Dify 登录页"的唯一机制。

    用**精确匹配**（`= /signin`）而不是前缀：前缀会连 `/signin/...` 一类路径一起吃进来，
    而 Dify 只在那一个路径上渲染登录界面。
    """
    conf = CONSOLE_CONF.read_text(encoding="utf-8")
    assert "location = /signin" in conf
    assert "dify-login-page.html" in conf


def test_the_login_page_is_served_with_a_locking_down_content_security_policy() -> None:
    """CSP `default-src 'none'` + `no-referrer`：这一页接公司口令，必须关到最紧。

    `unsafe-inline` 是给内联 `<style>` / `<script>` 用的（单文件资产的前提），
    除此之外不放任何来源。`Referrer-Policy: no-referrer` 防止把带参数的 URL 泄出去。
    """
    conf = CONSOLE_CONF.read_text(encoding="utf-8")
    assert "default-src 'none'" in conf
    assert "Referrer-Policy no-referrer" in conf
    assert "X-Frame-Options DENY" in conf


def test_the_login_proxy_conf_forwards_the_four_routes() -> None:
    """四条路由都要转发到回环上的代理；登录那条的请求体很小，上限跟着小。"""
    conf = PROXY_CONF.read_text(encoding="utf-8")
    for route in ("/__aiops/login", "/__aiops/login/captcha", "/__aiops/login/logout"):
        assert f"location = {route}" in conf, route
    assert "127.0.0.1:10119" in conf


# --------------------------------------------------------------------------- #
# 收口：Dify 原生账号入口（#783 阶段 3）                                        #
# --------------------------------------------------------------------------- #

#: 必须在两个入口都关掉的原生账号入口。判据是"运营不可能拿 Dify 邮箱密码登录"。
CLOSED_NATIVE_ENTRIES = (
    "/signup",
    "/install",
    "/forgot-password",
    "/reset-password",
    "/activate",
    "/console/api/login",
    "/console/api/forgot-password",
    "/console/api/forgot-password/validity",
    "/console/api/forgot-password/resets",
    "/console/api/reset-password",
    "/console/api/email-code-login",
    "/console/api/email-code-login/validity",
    "/console/api/email-register",
    "/console/api/email-register/send-email",
    "/console/api/email-register/validity",
    "/console/api/activate",
    "/console/api/activate/check",
)


def test_every_native_account_entry_is_closed() -> None:
    """逐条断言关闭规则**存在**（#783 阶段 3）。

    为什么要一条条钉住而不是"看起来封了"：这份清单漏掉任何一条，那条入口就是
    **绕过公司身份闭环的活口** —— 而它不会让任何测试变红，只会让运营继续用邮箱密码
    直登。所以清单在这里，加一条就补一条。
    """
    conf = CONSOLE_CONF.read_text(encoding="utf-8")
    missing = [p for p in CLOSED_NATIVE_ENTRIES if f"location = {p}" not in conf]
    assert not missing, f"这些原生入口没有关闭规则：{missing}"


def test_logout_and_refresh_are_deliberately_left_open() -> None:
    """`logout` 与 `refresh-token` **故意不封**，写下来免得被"顺手统一封掉"。

    `logout` 是正常流程；`refresh-token` 封了之后"会话过期"会变成硬 404，
    而不是跳登录页 —— 留着它，refresh 失败会 303 到 `/signin`，**正好落到我们的登录页**。
    """
    conf = CONSOLE_CONF.read_text(encoding="utf-8")
    for keep in ("/console/api/logout", "/console/api/refresh-token"):
        assert f"location = {keep}" not in conf, f"{keep} 被误封了"


def test_the_emergency_owner_entry_binds_loopback_only() -> None:
    """应急 owner 的原生入口只绑回环。

    绑 `0.0.0.0` 就等于给公网留一个绕过全部收口的入口 —— 那与"应急"是两件事。
    """
    conf = CONSOLE_CONF.read_text(encoding="utf-8")
    assert "listen 127.0.0.1:10009;" in conf
    assert "listen 10009;" not in conf and "listen 0.0.0.0:10009;" not in conf
    # 它是**原样**反代到 Dify 自己（所以原生登录在那个入口仍然可用）
    assert "listen 127.0.0.1:10009;" in conf and conf.count("proxy_pass http://127.0.0.1:10018;") >= 2


def test_the_closure_lives_on_the_shared_layer_not_the_tls_layer() -> None:
    """封禁规则必须在 10008 那一层，**不能**只写在 TLS 层。

    10008 被 `:443` 与明文 `:10008` 两个入口共用；只封 TLS 层等于给明文入口留绕过口。
    """
    tls = (ROOT / "ops" / "dify-console-bridge" / "dify-console-TLS.conf").read_text(encoding="utf-8")
    assert "location = /console/api/login" not in tls, "收口写到了 TLS 层 —— 明文入口会绕过它"
