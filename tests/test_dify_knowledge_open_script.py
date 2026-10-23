"""知识库入口注入脚本的**静态判据**（#983）。

这一页是注入到 Dify 的第三方界面里的脚本，本仓没有前端构建，也没有单测会执行它 ——
所以用"读文件 + 断言形状"钉住，先例是 `test_dify_login_page.py`。

判的**是形状，不是渲染**。真正"点一下能不能进"在 #985 的真机验收里；这里拦的是
          下一版把凭据塞进脚本、把路径判据写成模糊匹配、或者把 Dify 的 DOM 改坏。
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "ops" / "dify-console-bridge" / "dify-knowledge-open.js"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _code() -> str:
    """去掉 `/* … */` 与 `// …` 注释后的代码。

    注释里会引用反例（"不要写成 `indexOf('/datasets')`"）—— 断言扫注释会把**警告**
    当成**违规**。判据必须只看会被执行的字符。
    """
    text = re.sub(r"/\*.*?\*/", "", _script(), flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def test_the_script_reads_only_the_csrf_token_it_must_forward() -> None:
    """脚本**只读 CSRF**（那是 Dify 双提交要求的），不碰访问令牌、不碰任何密钥。

    CSRF 不是凭据：它必须与同域的会话 cookie 一起用才有意义，单独拿到它什么也做不了。
    而 `access_token` / `Authorization` / `password` / `secret` 一个都不许出现 ——
    那些才是凭据，脚本没有理由碰它们。
    """
    text = _script()
    for forbidden in ("access_token", "Authorization", "Bearer", "password", "secret"):
        assert forbidden not in text, f"注入脚本里出现了 {forbidden!r} —— 它不该持有任何凭据"
    # 只读 CSRF，且只从 document.cookie 读（那是页面自己的域）
    assert "csrf_token" in text
    assert "document.cookie" in text


def test_the_href_carries_no_identity_parameter() -> None:
    """入口的 URL **不带任何 workspace / tenant 参数**。

    带了就等于让 URL 决定"进哪个租户" —— 那正是 #979 那个洞的形状。桥侧也有同样的
    判据（`test_url_parameters_cannot_change_the_tenant`），两层各自钉住。
    """
    text = _code()
    href_line = next(line for line in text.splitlines() if "knowledge-open" in line)
    assert re.search(r"knowledge-open'?\s*;?\s*$", href_line.strip().rstrip(";")), href_line
    assert "?" not in href_line, href_line
    assert "workspace" not in href_line and "tenant" not in href_line, href_line


def test_the_route_predicate_is_a_prefix_match_not_a_substring() -> None:
    """路径判据必须是前缀精确匹配。

    `indexOf('/datasets') !== -1` 会命中任何包含它的路径；一个匹配过宽的脚本会在
    别人的页面上出现。判据只认 `/datasets` 与 `/datasets/…`。
    """
    text = _code()
    assert "pathname === '/datasets'" in text
    assert "indexOf('/datasets/') === 0" in text
    assert "indexOf('/datasets')" not in text.replace("indexOf('/datasets/')", "")


def test_the_entry_opens_in_a_new_tab_and_does_not_navigate_the_console() -> None:
    text = _script()
    assert "target = '_blank'" in text or '.target = "_blank"' in text
    assert "rel = 'noopener" in text or 'rel = "noopener' in text
    # 正常路径必须是 window.open；location.href 只能出现在**弹窗被拦**的兜底分支里。
    assert "window.open(" in text
    href_uses = [line for line in text.splitlines() if "location.href" in line]
    assert len(href_uses) == 1, f"location.href 只该出现在兜底分支：{href_uses}"
    assert "if (!w)" in text, "兜底必须由弹窗被拦触发"


def test_the_script_degrades_silently_when_it_cannot_mount() -> None:
    """挂不上时静默 —— 不抛错，也不改 Dify 自己的 DOM。

    注入脚本挂在别人的产品里：它出错时**最多只能让自己不出现**，不能让页面坏掉。
    """
    text = _script()
    assert "document.body" in text
    # 对外部状态的读写都在 try 里 —— 注入脚本碰别人的产品，任何一处抛错都会连累页面。
    assert text.count("try {") >= 1
    # 只创建自己的元素、只 remove 自己的 —— 不改 Dify 的节点
    assert "innerHTML =" not in text, "不该用 innerHTML 往 Dify 的容器里写"
    assert "document.querySelector(" not in text, "不该依赖 Dify 的内部 DOM 结构"


def test_the_card_can_be_dismissed_without_leaving_a_credential_behind() -> None:
    """收起状态**不携带任何身份**，也**不落盘**（#1015）。

    原来这里断言的是 `localStorage.setItem(HIDDEN_KEY, '1')` —— 那正是后来的缺陷：
    标记永久存在，而页面上没有恢复入口，一次误触等于永久失去入口。
    这条用例当初是为了"收起不携带身份"，而不是为了"收起必须持久"；现在两件事一起守：
    没有任何身份信息，且没有持久化。
    """
    text = _script()
    code = _code()
    assert "dismissedThisPage" in text
    assert "localStorage" not in code and "sessionStorage" not in code
    # 收起状态只是一个内存布尔；**不写盘、不带身份**。
    # （`workspace=` 是入口 URL 的一部分，那是"我在哪个工作区"，不是身份，别误伤。）
    assert "setItem" not in code and "getItem" not in code


def test_the_poller_never_stops() -> None:
    """轮询**不能有次数上限**（#1012，2026-10-23 真机）。

    原来写的是"40 次（20 秒）之后停"，理由是省电。那个账算错了：脚本只活在控制台里，
    500ms 一次路径比较的代价是零；而停掉之后，**先看别处、再看知识库**这条主路径上
    卡片根本不会出现 —— 用户看到的是"按钮又没了，不稳当"。

    真机复现：落 `/apps` 停留 24 秒 → 点侧栏「知识库」→ `card: false`；直接落
    `/datasets` → `card: true`。所以判据是：定时器里**不许**出现清掉自己的分支。
    """
    code = _code()
    poller = code[code.index("setInterval") :]
    # 定时器回调体到 `}, 500);` 为止
    body = poller[: poller.index("}, 500);")] if "}, 500);" in poller else poller
    assert "clearInterval" not in body, (
        "轮询里出现了 clearInterval —— 一旦停掉，晚于它再进入知识库页就再也挂不上卡片"
    )
    assert "tries" not in body and "> 40" not in body, "轮询里仍然有次数上限"


def test_dismissing_the_card_is_scoped_to_one_page_visit() -> None:
    """「收起」不能永久生效（#1015，2026-10-23 真机）。

    原来点一次 × 就写 `localStorage[...] = '1'`，而页面上**没有任何恢复入口**。
    一次误触等于永久失去入口 —— 用户看到的现象与"卡片又没了"一模一样，根因却与轮询
    那条无关。真机复现：点 × → `card:false, flag:'1'`；刷新 → 仍 `card:false`。

    判据：脚本**不写任何持久化标记**（`localStorage` / `sessionStorage` 一律不许出现），
    收起状态只存在于内存变量里。
    """
    code = _code()
    assert "localStorage" not in code, (
        "脚本不应写 localStorage —— 收起必须是'这次先别挡着我'，不是'我以后都不要了'"
    )
    assert "sessionStorage" not in code
    assert "dismissedThisPage" in code, "收起状态应是一个内存变量"
    # 关闭按钮只改内存变量，不调用任何持久化函数
    close_start = code.index("aria-label")
    close_body = code[close_start : close_start + 600]
    assert "dismissedThisPage = true" in close_body
