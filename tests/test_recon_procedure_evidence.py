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
    """Every covered command must exist in the block, and vice versa."""
    documented = _commands()
    covered = _coverage_totals()
    assert covered, "§6 的覆盖表不见了 —— 它是「哪条截断了」的唯一出处"
    for cmd, _ in covered.items():
        assert cmd in documented, f"覆盖表里有一条命令在代码块里找不到：{cmd}"


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


@_live
def test_the_untruncated_commands_reproduce_exactly() -> None:
    """ "全部" means the block shows the whole output — so it must match."""
    env = _recon_env()
    documented = _block_lines()
    totals = _coverage_totals()

    def real(cmd: str) -> list[str]:
        proc = subprocess.run(
            ["bash", "-c", cmd], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180
        )
        assert proc.returncode == 0, f"{cmd}\n{proc.stderr}"
        return proc.stdout.rstrip("\n").splitlines()

    checked = 0
    for cmd, note in totals.items():
        if note.strip() != "全部":
            continue
        start = documented.index(f"$ {cmd}")
        shown: list[str] = []
        for line in documented[start + 1 :]:
            if line.startswith("$ "):
                break
            # Blank lines separate commands in the block, so they are part of
            # what is shown; only a `$ ` ends it.
            shown.append(line)
        # …but the block pads the last output before the closing fence, and the
        # real output has no such padding.
        while shown and not shown[-1].strip():
            shown.pop()
        assert real(cmd) == shown, f"「全部」的命令对不上：{cmd}"
        checked += 1
    assert checked >= 5, f"只核对了 {checked} 条，覆盖表可能退化了"


@_live
def test_the_truncated_rows_state_the_true_total() -> None:
    """`前 N 行（共 M 行）` — M has to be the real line count."""
    env = _recon_env()
    for cmd, note in _coverage_totals().items():
        m = re.search(r"共\s*(\d+)\s*行", note)
        if not m:
            continue
        proc = subprocess.run(
            ["bash", "-c", cmd], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180
        )
        assert proc.returncode == 0, f"{cmd}\n{proc.stderr}"
        assert len(proc.stdout.rstrip("\n").splitlines()) == int(m.group(1)), cmd
