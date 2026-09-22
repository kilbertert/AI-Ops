"""Source-level closure for #367: no finalisation point picks its own shape.

#363-#366 narrowed the guard's input to the :class:`AnswerSurface` contract (or a
plain string) and moved the production callers onto it. What they leave unfixed is
why the defect was possible at all: the resource-name exemption used to be
reachable through exactly one Python shape, and nothing on the repository side
stopped a caller from choosing a different one. A caller that hands the guard a
bare list of block dicts gets its payload judged leaf by leaf, with no block kind
and no field names -- so a Chinese resource name reads as prose, one good card is
replaced with "the knowledge base is unavailable", and the operator alert says
``surface=qa`` as though the model had broken its language contract.

Source-level rather than behavioural, following ``tests/test_transport_convergence.py``:
what is being prevented is a future edit, not a current behaviour. And every
assertion here is proven to fail when the thing it pins is taken away -- this
repository has already fielded guards that passed while the thing they pointed at
had quietly been replaced, so a guard nobody has watched turn red is not evidence.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
#: Dotted path, as `ast` reads it: the module that owns the shared entry point.
GUARD_MODULE = "aiops_diagnostics.answer_language"
#: The one file that may define the entry point rather than call it.
GUARD_FILE = f"{GUARD_MODULE.rsplit('.', 1)[-1]}.py"
#: The shared entry point every finalisation point must judge through.
ENTRY = "answer_chinese_leak"
#: The two spellings that build the contract payload: the class itself, for a
#: surface that finalises on something other than public blocks, and the
#: constructor that reads the public ``blocks[]`` a surface delivers.
CONTRACT_CONSTRUCTORS = ("AnswerSurface", "from_public_blocks")


def _called_name(func: ast.expr) -> str:
    """The leaf name a call goes through, attribute chain or bare name alike."""
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _entry_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> source module for every binding of the shared entry point.

    Resolved through the import rather than by spelling: a caller that writes
    ``from ... import answer_chinese_leak as leak_check`` must still be
    enumerated, or the guard silently stops covering it.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == GUARD_MODULE:
            for alias in node.names:
                if alias.name == ENTRY:
                    aliases[alias.asname or alias.name] = node.module
    return aliases


def _entry_calls(tree: ast.Module) -> list[ast.Call]:
    """Every call that reaches the shared entry point, however it is spelled."""
    spellings = set(_entry_aliases(tree)) | {ENTRY}
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _called_name(node.func) in spellings
    ]


def _violations(source: str, filename: str) -> list[str]:
    """``file:line -- reason`` for every way ``source`` sidesteps the contract."""
    return _tree_violations(ast.parse(source, filename=filename), filename)


def _tree_violations(tree: ast.Module, filename: str) -> list[str]:
    """``file:line -- reason`` for every way ``tree`` sidesteps the contract.

    A module cannot shadow the entry point with a definition of its own either:
    a local ``answer_chinese_leak`` would be enumerated as clean by the call-site
    check while holding a second implementation of the rule.
    """
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    offenders = [
        f"{filename}:{node.lineno} -- a local {ENTRY} shadows the shared entry point"
        for node in ast.walk(tree)
        if filename != GUARD_FILE
        and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == ENTRY
    ]
    for call in _entry_calls(tree):
        if not call.args:
            offenders.append(f"{filename}:{call.lineno} -- no payload for the guard to read a shape from")
            continue
        reason = _shape_reason(call.args[0], tree, parents, call)
        if reason is not None:
            offenders.append(f"{filename}:{call.lineno} -- {reason}")
    return offenders


def _shape_reason(
    argument: ast.expr,
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
    call: ast.Call,
    seen: frozenset[str] = frozenset(),
) -> str | None:
    """Why ``argument`` is not one of the guard's two declared inputs.

    ``None`` when it is the contract type's construction result, or a string the
    caller built or proved. A string cannot sidestep the resource-name exemption:
    the exemption exists for a block's own ``title`` and the descriptor mounted
    on it, and a string carries neither -- there is nothing in one to exempt.
    """
    if isinstance(argument, ast.Call):
        called = _called_name(argument.func)
        if called in CONTRACT_CONSTRUCTORS:
            return None
        if called == "str":
            return None
        return f"{called}(...) is neither the contract payload nor a proven string"
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return None
    if isinstance(argument, ast.Name):
        if argument.id in seen:
            return f"`{argument.id}` is bound from itself"
        return _name_reason(argument, tree, parents, call, seen | {argument.id})
    return f"a {type(argument).__name__} expression is neither the contract payload nor a string"


def _name_reason(
    name: ast.Name,
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
    call: ast.Call,
    seen: frozenset[str],
) -> str | None:
    scope = _enclosing_scope(name, tree, parents)
    if scope is not None and _proves_text(name.id, scope):
        return None
    binding = _binding_before(name.id, scope, call)
    if binding is None:
        return f"`{name.id}` has no value yet where the guard is called; the guard cannot see its shape"
    return _shape_reason(binding, tree, parents, call, seen)


def _enclosing_scope(node: ast.AST, tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The innermost function ``node`` sits in -- or the module, if none."""
    cursor = parents.get(node)
    while cursor is not None:
        if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cursor
        cursor = parents.get(cursor)
    return tree


def _proves_text(name: str, scope: ast.AST | None) -> bool:
    """Does the caller's own code prove ``name`` holds a string?

    The two text-only surfaces spell it ``if not isinstance(text, str): return
    answer`` before they judge. That proof is what lets the guard accept a string
    payload, so it is looked for the way the caller writes it rather than
    re-derived from the assignment -- a value read out of a dict or a list has no
    shape the guard can see, and refusing it is the point.
    """
    if scope is None:
        return False
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call) or _called_name(node.func) != "isinstance":
            continue
        if len(node.args) != 2:
            continue
        target, proved = node.args
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        if isinstance(proved, ast.Name) and proved.id == "str":
            return True
    return False


def _binding_before(name: str, scope: ast.AST | None, call: ast.Call) -> ast.expr | None:
    """The value ``name`` holds where the guard is called.

    The nearest preceding binding, not every one in the function: a caller often
    binds the same name more than once -- `gateway_api` binds `casual` to the
    model's answer, then to the localized fallback after the guard has already
    judged it -- and only the binding in effect at the call is what the guard
    reads. Everything after it is none of the guard's business.
    """
    if scope is None:
        return None
    in_effect: tuple[tuple[int, int], ast.expr] | None = None
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            targets, value, position = list(node.targets), node.value, (node.lineno, node.col_offset)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value, position = [node.target], node.value, (node.lineno, node.col_offset)
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        if position >= (call.lineno, call.col_offset):
            continue
        if in_effect is None or position > in_effect[0]:
            in_effect = (position, value)
    return in_effect[1] if in_effect is not None else None


def _sources() -> list[Path]:
    return sorted(path for path in SOURCE_ROOT.glob("*.py"))


def _trees() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"), filename=path.name)) for path in _sources()]


def test_every_finalisation_point_hands_the_guard_the_contract_payload() -> None:
    """Enumerate every production call site of the shared entry point.

    A caller that builds a shape of its own is judged leaf by leaf, with no block
    kind and no field names -- which is how the one surface that needed the
    resource-name exemption never received it. The failure names the file and the
    line, so the offender is obvious rather than inferred.
    """
    offenders: list[str] = []
    for path, tree in _trees():
        offenders.extend(_tree_violations(tree, path.name))

    assert not offenders, (
        f"every finalisation point must pass the contract payload or a proven string to {ENTRY}; "
        f"found: {'; '.join(offenders)}"
    )


def test_the_enumeration_covers_every_finalisation_point() -> None:
    """A guard that finds no call site passes vacuously.

    So the enumeration is pinned to what the surfaces actually do: one
    finalisation point per answer surface, in the four modules that finalise an
    answer. A surface added or removed is a deliberate edit here -- a new one must
    travel through the same construction path, and one that disappears must not
    leave the guard claiming a coverage it no longer has.
    """
    sites = [(path.name, call.lineno) for path, tree in _trees() for call in _entry_calls(tree)]

    assert {name for name, _ in sites} == {
        "agent_validator.py",
        "gateway_api.py",
        "gateway_runtime.py",
        "qa_rag.py",
    }
    assert len(sites) == 4, sites


# --------------------------------------------------------------------------
# Proving the guard has teeth
#
# Each assertion above would also pass if the thing it points at had quietly
# been replaced -- which is how this repository came to carry guards nobody had
# watched turn red. So the detection is exercised on its own, on shapes a caller
# really does pick, and on the two files that really carried them.
# --------------------------------------------------------------------------

_SYNTHETIC_HEADER = (
    "from aiops_diagnostics.answer_language import AnswerSurface, answer_chinese_leak\n"
    "\n"
    "\n"
    "def judge(blocks, payload, language):\n"
)


def _synthetic(argument: str) -> str:
    """A module shaped like a production caller, with the payload replaced."""
    return f"{_SYNTHETIC_HEADER}    return {ENTRY}({argument}, language)\n"


@pytest.mark.parametrize(
    ("argument", "what_it_is"),
    [
        ("blocks", "a bare list of blocks"),
        ('[block.model_dump(mode="json") for block in blocks]', "the root-cause shape"),
        ('{"blocks": blocks}', "the serialised payload"),
        ('payload["blocks"]', "the payload's blocks read back"),
        ("payload", "the whole answer"),
        ('AnswerBlock(kind="text", text="All clear.")', "a single block"),
        ("AnswerSurface", "the type rather than a value of it"),
        ("[b for b in blocks]", "a rebuilt list"),
    ],
    ids=[
        "a bare list",
        "the root cause",
        "the serialised payload",
        "the delivered blocks",
        "the whole answer",
        "one block",
        "the type itself",
        "a rebuilt list",
    ],
)
def test_a_caller_that_picks_its_own_shape_fails_the_guard(argument: str, what_it_is: str) -> None:
    """Every one of these is a payload the guard's contract does not name.

    The second is verbatim what ``qa_rag`` passed before #364: a list
    comprehension of ``model_dump`` dicts, judged leaf by leaf so a block's kind
    -- and with it the resource-name exemption -- never reached the judgement.
    """
    violations = _violations(_synthetic(argument), "synthetic.py")

    assert violations, f"the guard accepted {what_it_is}: `{argument}`"


@pytest.mark.parametrize(
    "argument",
    [
        'AnswerSurface.from_public_blocks(payload["blocks"])',
        "AnswerSurface(blocks=())",
        'str(payload["text"])',
        '"Scan the QR code, then start charging."',
    ],
    ids=["from the public blocks", "built directly", "proven by str()", "a literal string"],
)
def test_the_two_declared_inputs_are_accepted(argument: str) -> None:
    """The guard must refuse the shapes it does not name, not everything in sight."""
    assert _violations(_synthetic(argument), "synthetic.py") == []


def test_a_string_the_caller_proved_is_accepted() -> None:
    """The text-only surfaces finalise on a string, and prove it before judging.

    `payload.get("text")` has no shape the guard can see -- it is a dict read, and
    the dict could hold anything. The `isinstance` the caller already wrote is
    what settles it, so it is accepted; without that proof it is refused below.
    """
    source = (
        f"{_SYNTHETIC_HEADER}"
        '    text = payload.get("text")\n'
        "    if not isinstance(text, str):\n"
        "        return None\n"
        f"    return {ENTRY}(text, language)\n"
    )

    assert _violations(source, "synthetic.py") == []


def test_a_value_the_caller_never_proved_is_refused() -> None:
    """The hole the proof closes: an unproven read is exactly how a bare list,
    or the whole answer dict, arrives at the guard carrying a shape nobody
    declared."""
    source = f'{_SYNTHETIC_HEADER}    text = payload.get("text")\n    return {ENTRY}(text, language)\n'

    assert _violations(source, "synthetic.py")


@pytest.mark.parametrize(
    ("module", "reverted_payload", "what_it_was"),
    [
        ("qa_rag.py", '[block.model_dump(mode="json") for block in blocks]', "a bare list of block dicts"),
        ("agent_validator.py", 'result.model_dump(mode="json")', "the whole serialised diagnosis document"),
    ],
    ids=["qa_rag", "agent_validator"],
)
def test_the_guard_reports_the_payload_that_module_carried_before(
    module: str, reverted_payload: str, what_it_was: str
) -> None:
    """Take the fix back out of a real file and this guard must go red.

    Not a synthetic stand-in: the argument the module actually carried is put
    back, by source span taken from the AST so it survives re-wrapping the call,
    and the guard is run over the rewritten file. What is proven is that the guard
    covers the regression and not a shape invented for the test.
    """
    source = (SOURCE_ROOT / module).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=module)
    call = _entry_calls(tree)[0]
    segment = ast.get_source_segment(source, call.args[0])
    assert segment is not None, f"no payload to revert in {module}"

    violations = _violations(source.replace(segment, reverted_payload), module)

    assert violations, f"{module} passing {what_it_was} was enumerated as clean"
