"""The question list now says *who does what*, in three buckets (#480).

The old table mixed "already answered by the baseline" with "still needs a
person" and "the data is wrong", so a reader had to read every row's status to
find out whether anything was theirs. The reclassification's claims are
checkable:

* three buckets exist, and the answered one **points at the baseline instead of
  restating its conclusions** — a restatement drifts the moment the baseline
  changes;
* every item in "only a person can answer" says *why source code cannot answer
  it* and *who answers* — that pair is the whole point of the bucket;
* the coverage breakdown stays four-part with C/D marked fail-closed, and B
  stays out of the "data problem" framing;
* the archival sections are still marked as archive, so the historical entries
  are not mistaken for a current to-do list.

The live half re-reads the one code fact the R-5 row rests on.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = PROJECT_ROOT / "docs" / "agents" / "operator-authorization-questions.md"
SCRIPT = "deploy/company-gitlab-api.sh"


def _text() -> str:
    return QUESTIONS.read_text(encoding="utf-8")


def _triage() -> str:
    """The reclassified section, up to the archival part that follows it."""
    text = _text()
    start = text.index("## 2''. 重新定级")
    end = text.index("### 5.5 R-1 追查结论")
    return text[start:end]


def _bucket(name: str) -> str:
    """One `### （X）` bucket, up to the next `### ` heading.

    Splitting on a stray arrow or a fixed marker is not enough: the bucket
    bodies contain both `→` and `### ` references, so the delimiter has to be a
    *heading*.
    """
    tail = _triage().split(f"### （{name}）", 1)[1]
    nxt = re.search(r"\n### ", tail)
    return tail[: nxt.start()] if nxt else tail


def test_the_triage_section_exists_and_precedes_the_archive() -> None:
    text = _text()
    assert "## 2''. 重新定级" in text
    assert text.index("## 2''. 重新定级") < text.index("## 2. 待答问题")


def test_the_header_points_at_the_current_section() -> None:
    head = _text().split("---")[0]
    assert "§2''" in head or "2''" in head, "文件头没有把读者指向当前该看的 §2''"
    assert "存证" in head or "存档" in head, "文件头没说明后面的内容是存证而不是待办"


def test_the_archive_is_marked_as_archive() -> None:
    text = _text()
    idx = text.index("## 2. 待答问题")
    banner = text[idx : idx + 400]
    assert "存档" in banner or "存档：" in banner, "§2 没有标注为存档 —— 会被当成当前待办"
    assert "§2''" in banner or "2''" in banner


def test_the_answered_bucket_points_at_the_baseline() -> None:
    """Restating a conclusion is how the two documents start to disagree."""
    answered = _bucket("一")
    assert "company-code-baseline.md" in answered, "「基线已回答」没有指向基线文档"
    refs = set(re.findall(r"\bL([1-4])-(\d)\b", answered))
    assert {layer for layer, _ in refs} >= {"2", "3"}, (
        "「基线已回答」应当指向 L2/L3 的具体章节，实际指向：" + repr(sorted(refs))
    )
    # The layer anchors it names must exist in the baseline.
    baseline = (PROJECT_ROOT / "docs" / "agents" / "company-code-baseline.md").read_text(encoding="utf-8")
    for layer, digit in sorted(refs):
        assert re.search(rf"^### L{layer}-{digit} ", baseline, re.MULTILINE), (
            f"指向了基线里不存在的章节 L{layer}-{digit}"
        )


def test_the_person_bucket_says_why_and_who() -> None:
    """Both columns must be filled for every row — that is the bucket's value."""
    people = _bucket("二")
    rows = [r for r in people.splitlines() if r.startswith("|") and r.count("|") >= 4]
    rows = [r for r in rows if not set(r.replace("|", "").strip()) <= set("-: ")]
    # Drop the header row: its first cell is , and its second is the label.
    rows = [r for r in rows if "为什么读源码答不出来" not in r]
    assert len(rows) >= 3, f"「只有人能答」只有 {len(rows)} 条"
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        why, who = cells[2], cells[3]
        # A dash or a placeholder is not a reason. Non-empty is too weak a
        # check: "—" passes it, and the whole value of this column is that the
        # reason is stated.
        assert len(why) >= 15 and why not in {"—", "-", "（无）"}, (
            f"「为什么读源码答不出来」写得不像理由：{cells[1][:30]} → {why!r}"
        )
        assert len(who) >= 2 and who not in {"—", "-", "（无）"}, (
            f"这一条没写「谁来答」：{cells[1][:30]} → {who!r}"
        )
        # "没去查" is not an answer to "why can't source answer it".
        assert "没查" not in why and "没去查" not in why
    for who in ("产品", "后端", "数据侧"):
        assert who in people, f"「谁来答」里少了 {who} 这一类"


def test_the_data_bucket_keeps_the_four_parts_and_b_is_not_a_gap() -> None:
    data = _bucket("三")
    for part in ("B", "C", "D", "E"):
        assert f"| {part}." in data, f"数据问题那一类里少了 {part} 类"
    for count in ("9,307", "57", "297", "20,782"):
        assert count in data, f"四分解少了 {count}"
    assert data.count("fail closed") >= 2, "C/D 必须各自标 fail closed"
    assert "B 不是缺口" in data or "B 是正常" in data, "没写明 B 不是缺口 —— 31.7% 会被读成「31.7% 有问题」"
    # …and the B row itself must say so, not just a sentence further down.
    b_row = next(r for r in data.splitlines() if r.startswith("| B."))
    assert "正常" in b_row, "B 那一行自身没标「正常」—— 把 B 描述成缺口正是这张表要防的读法"
    assert "缺口" not in b_row, "B 那一行不该出现「缺口」"
    assert "未重测" in data, "引用了生产库实测却没标明本票未重测"
    assert "数据侧" in data, "没写处置方是数据侧"


def test_it_does_not_restate_the_baseline_conclusions() -> None:
    """The answered bucket's job is to point, not to copy."""
    answered = _bucket("一")
    # These are conclusions the baseline owns; naming them here is fine, but
    # reproducing their measured values is what starts the drift.
    for value in ("954 行", "20782", "68.3%"):
        assert value not in answered, (
            f"「基线已回答」里复述了基线的事实（{value}）—— 复述会漂移，应当只指向章节"
        )


def test_the_internal_items_were_reviewed() -> None:
    """`## 3` was a to-do list; it must now say what happened to each item."""
    text = _text()
    start = text.index("## 3. 我方内部事项")
    end = text.index("## 4. 结论")
    section = text[start:end]
    assert "复核" in section, "内部事项没有复核标记 —— 会被当成待办"
    assert "C-2" in section and "D-5" in section, "C-2（operator 正向会话）没写明已由 D 批的 D-5 解决"
    assert "撤回" in section, "C-3/C-4 的撤回没有记下来"


def test_no_credentials_or_host_addresses() -> None:
    text = _text()
    for banned in ("PRIVATE-TOKEN", "password", "ssh ", "git-credentials"):
        assert banned not in text, f"这份会跨团队转发的文档里出现了 {banned}"


@pytest.mark.skipif(
    not (os.environ.get("AIOPS_GL_CA") and Path(os.environ["AIOPS_GL_CA"]).exists()),
    reason="未配置 AIOPS_GL_CA（身份锚点），跳过需要公司 GitLab 的复核",
)
def test_the_client_type_gate_still_accepts_exactly_three_values() -> None:
    """R-5's row says the value set is in source and the mapping is not.

    If the gate's accepted set changed, that row's reasoning needs redoing —
    the three values are what the mapping has to land on.
    """
    proc = subprocess.run(
        [
            "bash",
            SCRIPT,
            "raw",
            "--project",
            "300",
            "--ref",
            "dev_251103",
            "--path",
            "cloud-common-data/src/main/java/com/qushiyun/cloud/common/data/datascope/shop/ShopIdInterceptor.java",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert 'equalsAny(clientType, "admin", "supply-admin", "tenant-app")' in proc.stdout, (
        "隔离门认的 client-type 取值变了 —— R-5 那一行的推理要重做"
    )
