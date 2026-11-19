"""Skills reach Strands only as ``Skill`` objects ``record_to_strands_skill`` built.

agentskills-conformance S11. The Strands SDK can load a skill from a URL
(``Skill.from_url``) or from the filesystem (``Skill.from_file``,
``Skill.from_directory``). Every ingest control this deployment has (bounded
parsing, validation, provenance, audit, pinning) runs on the catalog row; a skill
loaded any of those three ways would reach the model having passed none of them.

So under ``backend/src``:

* ``AgentSkills(`` is constructed in exactly one module,
  ``agents/main_agent/skills/strands_mapping.py``, whose skills come from catalog
  records; and
* ``Skill.from_url``, ``Skill.from_file`` and ``Skill.from_directory`` appear
  nowhere. Tests may use them.

The check reads the syntax tree, so a comment or docstring naming them is fine.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

_SRC = Path(__file__).resolve().parent.parent.parent / "src"

_AGENT_SKILLS_HOME = "agents/main_agent/skills/strands_mapping.py"
_REMOTE_LOADERS = frozenset({"from_url", "from_file", "from_directory"})


def _modules() -> Iterator[Tuple[str, ast.AST]]:
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "AgentSkills" not in text and "from_" not in text:
            continue
        yield path.relative_to(_SRC).as_posix(), ast.parse(text, filename=str(path))


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _agent_skills_constructions() -> List[str]:
    sites: List[str] = []
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name_of(node.func) == "AgentSkills":
                sites.append(f"{rel}:{node.lineno}")
    return sites


def _remote_loader_uses() -> List[str]:
    """Every ``Skill.from_url`` / ``from_file`` / ``from_directory`` reference."""
    sites: List[str] = []
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _REMOTE_LOADERS
                and _name_of(node.value) == "Skill"
            ):
                sites.append(f"{rel}:{node.lineno}")
    return sites


def test_agent_skills_is_constructed_only_in_strands_mapping() -> None:
    sites = _agent_skills_constructions()
    assert sites, "AgentSkills( was not found at all; the scan is looking in the wrong place"
    assert {s.split(":")[0] for s in sites} == {_AGENT_SKILLS_HOME}, sites


def test_no_skill_is_loaded_from_a_url_or_the_filesystem() -> None:
    assert _remote_loader_uses() == []


def test_the_loader_scan_would_see_a_use() -> None:
    tree = ast.parse("from strands import Skill\nSkill.from_url('https://x')\n")
    found = [
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr in _REMOTE_LOADERS and _name_of(n.value) == "Skill"
    ]
    assert found == ["from_url"]
