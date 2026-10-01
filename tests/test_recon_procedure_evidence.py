"""The §6 commands of the recon procedure are copy-pasteable (#475).

The procedure's evidence section claims every line in its code block can be
copied verbatim and run. That claim has already been false once: scope notes
like ``（全部）`` sat at the end of command lines, where the shell read them as
arguments — ``pin （全部）`` exited 2, and ``wc -l`` treated the note as a
filename. A document that says "reproducible" while its own commands do not
run is worse than one that says nothing, because the next reader concludes
their environment is broken.

So the claims here are checked mechanically rather than trusted:

* no line inside the §6 block is shell-inert — every command line here must
  actually execute;
* the lines marked "全部" in the coverage table reproduce exactly, which is
  what that word means;
* the truncated rows name the true total, so "前 N 行" is a measurement and
  not a guess.

These commands read the company GitLab, so the live ones are skipped unless
``AIOPS_GL_CA`` points at the identity anchor (the repository registers no
custom markers, so this uses ``skipif`` like the other tests do). Without a
live instance the structural half still runs — and the structural half is the
half that regressed.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCEDURE = PROJECT_ROOT / "docs" / "agents" / "company-code-recon-procedure.md"
SCRIPT = "deploy/company-gitlab-api.sh"

#: A scope note the shell would happily swallow as an argument.
_NOTE_AT_LINE_END = re.compile(r"[（(]\s*(全部|前\s*\d+\s*行)[^）)]*[）)]\s*$")

#: "全部" means the block shows the whole thing. A note may add a count —
#: "全部（该查询共 3 行）" — but nothing may be cut away.
_WHOLE = re.compile(r"^全部")


def _is_whole(note: str) -> bool:
    return bool(_WHOLE.match(note.strip()))


def _section_6() -> str:
    text = PROCEDURE.read_text(encoding="utf-8")
    return text.split("## 6.", 1)[1].split("**脚本的守卫", 1)[0]


def _block_lines() -> list[str]:
    """The fenced block of §6, verbatim, without the fence markers."""
    block = _section_6().split("```", 2)[1]
    return block.rstrip("\n").splitlines()


def _commands() -> list[str]:
    """Every documented command, with `\\`-continuations joined back up."""
    out: list[str] = []
    current: str | None = None
    for line in _block_lines():
        if line.startswith("$ "):
            if current is not None:
                out.append(current)
            current = line[2:]
        elif current is not None and current.endswith("\\"):
            current = current[:-1].rstrip() + " " + line.strip()
        elif not line.strip():
            if current is not None:
                out.append(current)
            current = None
    if current is not None:
        out.append(current)
    return out


def _coverage_totals() -> dict[str, str]:
    """`command → note` from the coverage table that precedes the block.

    The table writes the command without the script path and escapes the pipe in
    ``| wc -l`` (a markdown table needs ``\\|``); both are restored here so the
    keys are the same strings the block's command lines are.
    """
    rows = re.findall(r"^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|$", _section_6(), re.MULTILINE)
    return {cmd.replace(chr(92) + "|", "|"): note for cmd, note in rows}


def test_the_procedure_and_the_script_are_both_present() -> None:
    assert PROCEDURE.exists()
    assert (PROJECT_ROOT / SCRIPT).exists()


def test_no_scope_note_is_shell_inert() -> None:
    """A note at the end of a command line is an argument, not a note.

    This is the exact defect that made the claim false: `pin （全部）` exits 2.
    """
    offenders = [line for line in _block_lines() if _NOTE_AT_LINE_END.search(line)]
    assert offenders == [], (
        "这些行的行末标注会被 shell 当成参数：" + repr(offenders) + "。标注要移出代码块（见 §6 的覆盖表）。"
    )


def test_every_documented_command_is_extractable() -> None:
    commands = _commands()
    assert len(commands) >= 8
    assert all(c.startswith(SCRIPT) for c in commands), commands


def test_the_coverage_table_has_no_stale_rows() -> None:
    """The table and the block name the same commands, and the same count.

    Both directions matter and the first version only checked one: a command
    added to the block but left out of the table would never be checked for
    truncation, which is exactly the gap the table exists to close.
    """
    documented = _commands()
    covered = _coverage_totals()
    assert covered, "§6 的覆盖表不见了 —— 它是「哪条截断了」的唯一出处"
    for cmd in covered:
        assert cmd in documented, f"覆盖表里有一条命令在代码块里找不到：{cmd}"
    for cmd in documented:
        assert cmd in covered, f"代码块里有一条命令不在覆盖表里（没人核它截没截）：{cmd}"
    assert len(covered) == len(documented)


def test_the_script_rejects_unknown_arguments_loudly() -> None:
    """`未知参数` + exit 2 is what makes the note-at-line-end defect detectable."""
    if shutil.which("bash") is None:  # pragma: no cover - bash is always here
        pytest.skip("bash 不可用")
    proc = subprocess.run(
        ["bash", SCRIPT, "pin", "（全部）"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "未知参数" in proc.stderr


#: Live checks need the company instance; skip them rather than fail the suite.
_live = pytest.mark.skipif(
    not (os.environ.get("AIOPS_GL_CA") and Path(os.environ["AIOPS_GL_CA"]).exists()),
    reason="未配置 AIOPS_GL_CA（身份锚点），跳过需要公司 GitLab 的复核",
)


def _recon_env() -> dict[str, str]:
    ca = os.environ.get("AIOPS_GL_CA")
    if not ca or not Path(ca).exists():
        pytest.skip("未配置 AIOPS_GL_CA（身份锚点），跳过需要公司 GitLab 的复核")
    return {**os.environ, "AIOPS_GL_CA": ca}


@_live
def test_the_commands_run_verbatim() -> None:
    """Every documented line executes, and the untruncated ones reproduce.

    A line that fails here means the block is not copy-pasteable, which is the
    only property §6 promises.
    """
    env = _recon_env()
    for command in _commands():
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, f"命令跑不通：{command}\n{proc.stderr}"
        assert "未知参数" not in proc.stderr, command


@functools.cache
def real_lines(cmd: str) -> tuple[str, ...]:
    """Run one documented command once; the live checks share the result."""
    env = _recon_env()
    proc = subprocess.run(
        ["bash", "-c", cmd], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180
    )
    assert proc.returncode == 0, f"{cmd}\n{proc.stderr}"
    return tuple(proc.stdout.rstrip("\n").splitlines())


@_live
def test_the_untruncated_commands_reproduce_exactly() -> None:
    """ "全部" means the block shows the whole output — so it must match."""
    documented = _block_lines()
    totals = _coverage_totals()

    real_lines.cache_clear()
    checked = 0
    for cmd, note in totals.items():
        if not _is_whole(note):
            continue
        assert list(real_lines(cmd)) == _documented_output(documented, cmd), f"「全部」的命令对不上：{cmd}"
        checked += 1
    assert checked >= 5, f"只核对了 {checked} 条，覆盖表可能退化了"


def _documented_output(documented: list[str], cmd: str) -> list[str]:
    """The block's output for `cmd`: everything up to the next `$ `, less padding."""
    start = documented.index(f"$ {cmd}")
    shown: list[str] = []
    for line in documented[start + 1 :]:
        if line.startswith("$ "):
            break
        shown.append(line)
    while shown and not shown[-1].strip():
        shown.pop()
    return shown


@_live
def test_the_truncated_rows_show_the_truth_and_state_the_true_total() -> None:
    """A truncated row is checked on both halves: what is shown, and how much there is.

    Checking only the total let a row keep a stale first line while the count
    stayed right — the reader sees a command whose output no longer matches.
    """
    documented = _block_lines()
    checked_rows = 0
    for cmd, note in _coverage_totals().items():
        m = re.search(r"共\s*(\d+)\s*行", note)
        if not m:
            continue
        shown = _documented_output(documented, cmd)
        assert list(real_lines(cmd))[: len(shown)] == shown, f"截断行贴出的内容对不上：{cmd}"
        assert len(real_lines(cmd)) == int(m.group(1)), cmd
        checked_rows += 1
    assert checked_rows >= 2, f"只核对了 {checked_rows} 条截断行"
