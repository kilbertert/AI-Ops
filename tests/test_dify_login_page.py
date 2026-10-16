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
