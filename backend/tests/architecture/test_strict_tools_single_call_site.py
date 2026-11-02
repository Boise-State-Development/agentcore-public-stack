"""``strict_tools`` is set in exactly one place, and never on a chat agent.

``docs/specs/structured-extraction.md`` D15. ``BedrockModel(strict_tools=True)``
is a **config** flag, not a per-request one. Read in the pinned SDK
(``strands/models/bedrock.py``), it:

* adds ``"strict": True`` to **every** ``toolSpec`` in that model's requests, and
* runs ``ensure_strict_json_schema`` over every one of their ``inputSchema``s,

and the AWS documentation is explicit that "a schema that uses an unsupported
feature fails at request time with a ``ValidationException``".

Our agents carry MCP tool schemas we do not author and cannot fix, where
``oneOf`` is ordinary and unsupported under strict mode. So setting this on the
chat agent's model would hand a third party's tool definition the ability to
fail an entire turn — a user's conversation broken by a server they connected
weeks ago, with nothing in our own code having changed.

``agent_factory`` builds ``CountTokensBedrockModel(native_projection=False,
**bedrock_config)``, so the flag would arrive silently through
``bedrock_config`` if anyone ever added it there. That is the specific accident
this test exists to catch, which is why it asserts on the whole tree rather
than on the extraction module alone.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

_BACKEND = Path(__file__).resolve().parent.parent.parent
_ROOTS = (_BACKEND / "src", _BACKEND / "scripts")

#: The only module allowed to set it, and why: it constructs a dedicated
#: ``BedrockModel`` for a request carrying exactly one tool — the generated
#: field schema — so "every tool spec in the request" is that one schema, which
#: we author and whose subset compliance is unit-tested.
_ALLOWED = {"src/apis/shared/extraction/extractor.py"}

#: Where a chat agent's model is built. Named explicitly because this is the
#: path the flag must never reach, and naming it makes the failure legible.
_AGENT_FACTORY = "src/agents/main_agent/core/agent_factory.py"


def _python_files() -> Iterator[Tuple[str, Path]]:
    for root in _ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            yield path.relative_to(_BACKEND).as_posix(), path


def _strict_tools_sites() -> List[str]:
    """Every place ``strict_tools`` is named, as ``path:line``."""
    sites: List[str] = []
    for rel, path in _python_files():
        text = path.read_text(encoding="utf-8")
        if "strict_tools" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - not our file to fix
            continue
        for node in ast.walk(tree):
            # A keyword argument: BedrockModel(strict_tools=...)
            if isinstance(node, ast.keyword) and node.arg == "strict_tools":
                sites.append(f"{rel}:{node.value.lineno}")
            # A dict entry: bedrock_config["strict_tools"] = ... / {"strict_tools": ...}
            elif (
                isinstance(node, ast.Constant)
                and node.value == "strict_tools"
                and isinstance(node.value, str)
            ):
                sites.append(f"{rel}:{node.lineno}")
    return sites


def test_strict_tools_is_set_in_exactly_one_module() -> None:
    offenders = [
        site for site in _strict_tools_sites() if site.rsplit(":", 1)[0] not in _ALLOWED
    ]
    assert not offenders, (
        "strict_tools restricts the JSON Schema features EVERY tool in that "
        "model's requests may use, and we carry MCP schemas we did not author. "
        "It belongs only on the dedicated extraction model:\n  "
        + "\n  ".join(offenders)
    )


def test_the_extraction_module_does_set_it() -> None:
    """The other half of the assertion above.

    Without this, deleting the flag entirely would leave the test suite green
    while silently turning the typed schema into a suggestion — exactly the
    unenforced path D11 insists must be marked rather than default.
    """
    sites = [site for site in _strict_tools_sites() if site.rsplit(":", 1)[0] in _ALLOWED]
    assert sites, "the extraction model must still request strict tool use"


def test_the_agent_factory_never_names_it() -> None:
    """Stated separately so a failure says *where* it went wrong.

    ``bedrock_config`` is splatted into the model constructor, so a key added
    there would arrive with no call site mentioning ``BedrockModel`` at all.
    """
    text = (_BACKEND / _AGENT_FACTORY).read_text(encoding="utf-8")
    assert "strict_tools" not in text, (
        "agent_factory builds the model that carries the turn's MCP tool "
        "schemas; strict mode there lets a third party's tool definition fail "
        "the whole turn"
    )


def test_the_extraction_model_is_not_the_turns_model() -> None:
    """The extraction call builds its own instance and shares nothing.

    If it ever reached for the agent's model, D15 would be unenforceable: the
    flag would be on an object whose other tools we do not control.

    Checked against the parsed *code* — imports, names and attributes — rather
    than the file's text, because this module's own docstrings explain the
    decision by naming ``agent_factory``, and a substring check would flag the
    explanation as the violation.
    """
    tree = ast.parse(
        (_BACKEND / "src/apis/shared/extraction/extractor.py").read_text(
            encoding="utf-8"
        )
    )
    borrowed = {"agent_factory", "CountTokensBedrockModel", "AgentFactory"}
    offenders: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(part in borrowed for part in alias.name.split(".")):
                    offenders.append(f"import {alias.name} (line {node.lineno})")
        elif isinstance(node, ast.ImportFrom):
            module_parts = (node.module or "").split(".")
            for alias in node.names:
                if alias.name in borrowed or any(p in borrowed for p in module_parts):
                    offenders.append(
                        f"from {node.module} import {alias.name} (line {node.lineno})"
                    )
        elif isinstance(node, ast.Name) and node.id in borrowed:
            offenders.append(f"{node.id} (line {node.lineno})")
        elif isinstance(node, ast.Attribute) and node.attr in borrowed:
            offenders.append(f".{node.attr} (line {node.lineno})")

    assert not offenders, (
        "the extraction path must construct its own BedrockModel rather than "
        "reach for the agent's:\n  " + "\n  ".join(offenders)
    )
