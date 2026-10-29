"""注入到 Dify 编排页的那颗按钮脚本：**离线**判据（静态资产，跑不进单测）。

脚本是纯前端资产，本仓没有前端构建，所以它不会被任何单测跑到 —— 而它接的是运营的
会话与订单号。这个文件用「读文件 + 断言内容」把它钉住（先例：`test_dify_login_page.py`）。

这里判的是**形状与契约**，不是渲染：真正的验收是浏览器里点一遍（见
`docs/validation.md`）。静态判据拦的是"下一版把请求发到别处""轮询没有上限"
"把终态词表抄一份到前端"这三类回归 —— 它们在浏览器里看起来完全正常。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "ops" / "dify-console-bridge" / "dify-publish-button.js"
NGINX_SNIPPET = ROOT / "ops" / "dify-console-bridge" / "dify-console-publish.conf"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_it_speaks_only_to_our_own_routes(script: str) -> None:
    """**不往任何第三方或 Dify 自己的 API 发请求**：只有这几条同源路径。

    M3-01 加了三个运营动作（撤回 / 恢复历史版 / 版本列表），它们同样是**我们自己的**
    `/__aiops/*` 路由 —— 白名单逐条列出，是为了让"新加一条外部目标"必须显式修改
    这个断言，而不是悄悄滑过去。
    """
    targets = set()
    marker = "fetch("
    index = 0
    while True:
        found = script.find(marker, index)
        if found == -1:
            break
        quote = script[found + len(marker)]
        end = script.find(quote, found + len(marker) + 1)
        targets.add(script[found + len(marker) + 1 : end])
        index = end
    assert targets == {
        "/__aiops/publish",
        "/__aiops/retract",
        "/__aiops/restore",
        "/__aiops/versions",
        "/__aiops/trial",
        "/__aiops/trial/result",
    }, targets


def test_it_carries_no_credentials_of_its_own(script: str) -> None:
    """脚本里**没有**服务端密钥，也没有把 Dify 的 cookie 读出来再发出去。

    它只读 ``csrf_token``（Dify 自己的双提交规则要求的那一枚），且两种前缀都试。
    """
    assert "Bearer" not in script
    assert "aiopsReadCookie('__Host-csrf_token')" in script
    assert "aiopsReadCookie('csrf_token')" in script
    # 只读这一枚 cookie；读别的（例如 access_token）就是把凭据搬进脚本的作用域。
    reads = [
        line.strip() for line in script.splitlines() if "aiopsReadCookie(" in line and "function" not in line
    ]
    assert all(("csrf_token" in line) for line in reads), reads


def test_the_result_panel_has_a_hard_polling_limit(script: str) -> None:
    """轮询**必须有上限**：页面开着的时候不会永远发请求。"""
    assert "AIOPS_TRIAL_MAX_POLLS" in script
    assert "polls >= AIOPS_TRIAL_MAX_POLLS" in script
    assert "aiopsTrialStop()" in script


def test_the_panel_takes_its_terminal_verdict_from_the_server(script: str) -> None:
    """终态由服务端说（``d.terminal``），前端**不抄一份状态词表**。

    抄一份的后果是两份词表会漂：一边加了 ``expired``，另一边还在等 ``completed`` ——
    面板永远转圈，而看起来什么都正常。
    """
    assert "d.terminal" in script
    for word in ("'completed'", '"completed"', "'expired'", '"expired"', "'cancelled'", '"cancelled"'):
        assert word not in script, f"前端自己判了终态词 {word}"


def test_the_submit_path_hands_the_job_id_to_the_panel(script: str) -> None:
    """提交成功且拿到 ``trial_id`` ⇒ 开面板；没有就不开（没有可轮询的东西）。"""
    assert "aiopsWatchTrial(d.trial_id, orderNo)" in script
    assert "if (d.submitted && d.trial_id)" in script


def test_the_nginx_snippet_routes_every_route_the_script_calls(script: str) -> None:
    """脚本打的每一条路径，nginx 都得有一条**精确匹配**的 location 转给桥。

    缺一条的表现是 404 —— 而按钮还在、点击也有响应，看起来像"桥挂了"。
    """
    snippet = NGINX_SNIPPET.read_text(encoding="utf-8")
    for path in ("/__aiops/publish", "/__aiops/trial", "/__aiops/trial/result"):
        assert f"location = {path} " in snippet, f"nginx snippet 缺 location {path}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不在本机（CI 有）")
def test_the_script_parses_as_javascript() -> None:
    """语法错误在浏览器里表现为"整颗按钮消失"，而没有任何服务端信号。"""
    result = subprocess.run(
        ["node", "--check", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_a_trial_id_is_only_sent_back_as_a_bounded_string(script: str) -> None:
    """``trial_id`` 原样回给桥，但**放进 body**，不由前端拼 URL。

    拼 URL 会绕开桥那条 POST 门（同源 + CSRF + Referer），而浏览器同源 GET 不发
    ``Origin`` —— 那正是这条路由刻意做成 POST 的原因。目标路径本身由
    ``test_it_speaks_only_to_our_own_routes`` 钉住。
    """
    assert "JSON.stringify({ trial_id: trialId })" in script
