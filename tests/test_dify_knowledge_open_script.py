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


def test_the_script_carries_no_credential() -> None:
    """脚本里不得出现任何凭据赋值。租户由桥从 Dify 会话解析，脚本一个字节都不传。"""
    text = _script()
    for forbidden in ("access_token", "csrf", "Authorization", "Bearer", "password", "secret"):
        assert forbidden not in text, f"注入脚本里出现了 {forbidden!r} —— 它不该持有任何凭据"


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
    # 不得用 location.href / location.assign 把 Dify 自己导航走
    assert "location.href =" not in text
    assert "location.assign" not in text


def test_the_script_degrades_silently_when_it_cannot_mount() -> None:
    """挂不上时静默 —— 不抛错，也不改 Dify 自己的 DOM。

    注入脚本挂在别人的产品里：它出错时**最多只能让自己不出现**，不能让页面坏掉。
    """
    text = _script()
    assert "document.body" in text
    # 所有对外部状态的读写都在 try 里（localStorage 在隐私模式下会抛）
    assert text.count("try {") >= 2
    # 只创建自己的元素、只 remove 自己的 —— 不改 Dify 的节点
    assert "innerHTML =" not in text, "不该用 innerHTML 往 Dify 的容器里写"
    assert "document.querySelector(" not in text, "不该依赖 Dify 的内部 DOM 结构"


def test_the_card_can_be_dismissed_without_leaving_a_credential_behind() -> None:
    text = _script()
    assert "aiops.kbOpenCardHidden" in text
    # 收起状态只记一个布尔，不记任何身份
    assert "setItem(HIDDEN_KEY, '1')" in text
