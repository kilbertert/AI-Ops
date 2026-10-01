"""L3 answers "who sees which rows" in a decidable form (#478).

The L3 layer is the one where a vague sentence is actively dangerous: "there is
authorisation here" reads as a fact and is not one. So the layer is checked on
the properties that make its answer usable:

* **every mechanism named has a `仓:分支:文件:行` source** — same contract as L2,
  same quiet failure mode when it is dropped;
* **the dead code is named** — this layer's own header says a mechanism that is
  only present inside a comment must be called out, because the next reader
  will otherwise reason from it;
* **the two `client-type` sets are written down**, and they are the disjoint
  sets the layer claims — one of them decides whether isolation happens at all;
* **`@Inside` / `DataScopeInterceptor` / `AdminProxyHeadFilter` stay in the
  table.** Deleting a row because "everyone knows" is how the next reader
  stops knowing.

The live half re-reads the three dead-code sites and asserts the commented-out
bodies are still commented out; skipped without an identity anchor.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_ROOT / "docs" / "agents" / "company-code-baseline.md"
SCRIPT = "deploy/company-gitlab-api.sh"

_CITATION = re.compile(r"`[A-Za-z0-9_./-]+:[A-Za-z0-9_./-]+:[^`]*\.(?:java|xml|yml):\d+(?:-\d+)?`")


def _section(name: str) -> str:
    text = BASELINE.read_text(encoding="utf-8")
    start = text.index(f"## {name} ·")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _between(part: str, start: str, end: str) -> str:
    """Slice between two `### ` headings.

    Splitting on the bare label is not enough: the body *references* other
    parts ("见 L3-4"), so the first occurrence is usually a cross-reference.
    """
    return part.split(f"### {start} ")[1].split(f"### {end} ")[0]


def _tail(part: str, start: str) -> str:
    """From one `### ` heading to the end of the section."""
    return part.split(f"### {start} ")[1]


def test_l3_is_present_and_filled() -> None:
    l3 = _section("L3")
    assert "（尚未填充。）" not in l3, "L3 还是占位符"
    assert "L3-0" in l3 and "L3-2" in l3


def test_every_mechanism_cited_has_a_source() -> None:
    """Full citations are well-formed, and relative ones resolve.

    L3 uses two forms on purpose: a full `仓:分支:文件:行` the first time a file
    is named, and a bare `:NNN` afterwards. The second form is only honest if
    the full one is in the same subsection — otherwise a reader has a line
    number and no file to look in. That is the property checked here.
    """
    l3 = _section("L3")
    citations = _CITATION.findall(l3)
    assert len(citations) >= 5, f"L3 里的完整出处太少（{len(citations)} 条），可能被删了"
    for c in citations:
        assert c.count(":") >= 3, f"出处缺分支或行号（形如 仓:分支:文件:行）：`{c}`"

    # Every `:NNN` reference must sit under a heading that also carries a full
    # citation — no dangling line numbers.
    for block in re.split(r"\n### ", l3)[1:]:
        title = block.split("\n", 1)[0]
        relative = re.findall(r"`:[0-9]+(?:-[0-9]+)?`", block)
        if not relative:
            continue
        assert _CITATION.search(block) or _CITATION.search(l3.split(f"### {title}")[0]), (
            f"「{title}」里有 {len(relative)} 处相对行号，但这段（及其上文）没有完整出处"
        )


def test_the_dead_code_is_named() -> None:
    """Three sites, each of which reads as a live check until you open it."""
    l3 = _section("L3")
    dead = _tail(l3, "L3-5")
    rows_all = [r for r in dead.splitlines() if r.startswith("|")]
    # Match the *name in the row's description*, not anywhere in the line: the
    # file path in the source column also contains e.g. `BaseSecurityInsideAspect`,
    # so a substring search over the whole row passes even after the row has
    # stopped *naming* the mechanism — which is the thing that matters.
    for name in ("`@Inside`", "`DataScopeInterceptor`", "`AdminProxyHeadFilter`"):
        assert any(name in r.split("|")[2] for r in rows_all), (
            f"死代码表里少了 {name} 这一行 —— 它看起来在生效，不点名就会被照着推理"
        )
    # Each row must carry a source; a dead-code claim without one is a rumour.
    rows = [r for r in dead.splitlines() if r.startswith("|") and "`" in r]
    assert len(rows) >= 3
    for row in rows:
        if set(row.replace("|", "").strip()) <= set("-: "):
            continue
        assert _CITATION.search(row) or ":\\d" in row, f"死代码这一行没有出处：{row[:80]}"


def test_the_two_client_type_sets_are_written_down() -> None:
    """The gate that decides whether isolation happens at all."""
    l3 = _section("L3")
    gate = _between(l3, "L3-3", "L3-4")
    for value in ("admin", "supply-admin", "tenant-app"):
        assert value in gate, f"隔离门认的 client-type 少了 {value}"
    for value in ("MA", "H5", "APP"):
        assert value in gate, f"网关产出的 client-type 少了 {value}"
    assert "不相交" in gate, "没写清两套 client-type 不相交 —— 那正是「不隔离」的原因"


def test_it_says_which_side_ai_ops_falls_on() -> None:
    """The layer must not leave the reader to infer it, and must not overstate it.

    Two overdrawn drafts were caught in review — "isolation may already apply"
    and "the steward app sends `admin`, so isolation is on". Both come from the
    same mistake: treating passing the `client-type` gate as *being isolated*.
    The gate is one of five, so the layer has to separate "passes gate 5" from
    "is isolated".
    """
    l3 = _section("L3")
    gate = _between(l3, "L3-3", "L3-4")
    rows = [r for r in gate.splitlines() if r.startswith("|")]
    assert any("AI-Ops" in r for r in rows), "表里没有「AI-Ops 出站调用」那一行"
    assert any("管家端" in r for r in rows), (
        "表里没有「管家端浏览器直连公司后端」那一行 —— 两种调用形态必须分开写"
    )
    assert "绕开" in gate, "没点明 AI-Ops 是**绕开**这套隔离，而不是「隔离对它不适用」"
    assert "D 批" in gate, "没写明这条结论会被 D 批改掉、届时需要重测"
    # Passing gate 5 is not the same as being isolated.
    assert "「过了 5」≠「隔离了」" in gate or "不等于" in gate, (
        "没写清「过了门 5」≠「隔离生效」—— 门 4 还会再放行一类查询"
    )
    # And the outbound direction must not be confused with the inbound one.
    assert "自己构造出站头" in gate, "没写明 AI-Ops 的出站头是自己构造的：D 批改的是入站，不会自动改变出站"


def test_the_three_outbound_call_shapes_are_not_merged() -> None:
    """`/diag/*`, UPMS and TDengine carry different credentials.

    Writing them as one row was a review finding: only the *absence* of
    `client-type` is common to all three, and that is the whole basis for the
    gate-5 conclusion.
    """
    l3 = _section("L3")
    gate = _between(l3, "L3-3", "L3-4")
    for marker in ("X-Internal-Token", "Bearer", "Basic"):
        assert marker in gate, f"三类出站请求里少了 {marker} 那一种"
    assert "都不带 `client-type`" in gate or "共同点" in gate, (
        "没写明三类的共同点只有「不带 client-type」—— 那才是结论的依据"
    )


def test_the_empty_set_branches_are_distinguished() -> None:
    """seePlatform=false 放行 vs true 收紧 —— 这两个分支方向相反。"""
    l3 = _section("L3")
    flow = _between(l3, "L3-2", "L3-3")
    assert "seePlatform" in flow
    assert re.search(r"seePlatform[^\n]*false", flow) and "放行" in flow, (
        "没写清 seePlatform=false 且集合为空时是**放行**"
    )
    assert "true" in flow and "收紧" in flow, (
        "没写清 seePlatform=true 且集合为空时是**收紧**（两个分支方向相反）"
    )


def test_it_does_not_restate_the_ai_ops_position() -> None:
    """The stated division of labour with the other document."""
    l3 = _section("L3")
    head = l3.split("### L3-0 ")[0]
    assert "company-platform-integration-baseline.md" in head, (
        "L3 开头必须写明与 company-platform-integration-baseline.md §2 的分工"
    )


def test_no_company_source_body_is_pasted() -> None:
    l3 = _section("L3")
    assert "```" not in l3, "L3 里出现了代码块 —— 本节声明不搬运公司源码正文"


@pytest.mark.skipif(
    not (os.environ.get("AIOPS_GL_CA") and Path(os.environ["AIOPS_GL_CA"]).exists()),
    reason="未配置 AIOPS_GL_CA（身份锚点），跳过需要公司 GitLab 的复核",
)
def test_the_three_dead_code_sites_are_still_commented_out() -> None:
    """Re-read each site and assert the body is inside a comment.

    This is the claim the layer exists to make; if one of these gets
    re-enabled upstream, the row is stale and a reader would be told a live
    mechanism is dead.
    """
    common = "cloud-common-data/src/main/java/com/qushiyun/cloud/common/data/datascope"
    cases = [
        (
            "300",
            "dev_251103",
            "cloud-common-security/src/main/java/com/qushiyun/cloud/common/security/component/BaseSecurityInsideAspect.java",
            "//if (inside.value()",
        ),
        (
            "300",
            "dev_251103",
            f"{common}/DataScopeInterceptor.java",
            "PluginUtils.MPBoundSql mPBoundSql",
        ),
        (
            "488",
            "release",
            "src/main/java/com/qushiyun/cloud/gateway/filter/AdminProxyHeadFilter.java",
            # Unique to the commented block: `return chain.filter(exchange);`
            # also appears as the one *live* line, so it cannot be the marker.
            "String authorization = exchange.getRequest().getHeaders()",
        ),
    ]
    for project, ref, path, marker in cases:
        proc = subprocess.run(
            ["bash", SCRIPT, "raw", "--project", project, "--ref", ref, "--path", path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, f"{path}\n{proc.stderr}"
        body = proc.stdout
        assert marker in body, f"{path} 里找不到 {marker}"
        # The marker must sit inside a comment block or on a commented line.
        for i, line in enumerate(body.splitlines()):
            if marker in line:
                if line.lstrip().startswith(("//", "*", "/*")):
                    break
                # Otherwise it must be within a block that opened earlier.
                head = "\n".join(body.splitlines()[:i])
                assert head.count("/*") > head.count("*/"), (
                    f"{path}:{i + 1} 的那段看起来已经不在注释里了 —— 死代码那一行要重新核"
                )
                break
