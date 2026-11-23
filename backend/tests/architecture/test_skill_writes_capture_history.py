"""Every write to a skill's content keeps a version (docs/specs/skill-version-history.md §4.3).

With ``SKILL_VERSION_HISTORY_ENABLED`` on, history holds every content a skill ever
had only if *every* write path captures. A new path that writes the row or its files
without calling ``_capture`` would skip history with no error anywhere. So this test
finds each call that writes a skill row or bundle files (``create_skill``,
``update_skill``, ``put_imported_files``, and ``SkillBundleFiles``' ``add_resource`` /
``delete_resource``) on a repository or bundle-files receiver, and requires its
enclosing function to be listed below: either as capturing (and it must call
``_capture``, or ``capture`` inside ``history``) or with the reason it is exempt.

When this fails on a new site: call the service's ``_capture`` after the write (and
``_checkpoint`` before it, ``required=True`` if it replaces the whole skill), then add
the site to ``CAPTURES``. If the write cannot change content, add it to ``EXEMPT``
with the reason.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

SRC = Path(__file__).resolve().parents[2] / "src"

WRITE_METHODS = {"create_skill", "update_skill", "put_imported_files", "add_resource", "delete_resource"}

Site = Tuple[str, str]  # (path under src, enclosing Class.function)

CAPTURES: Set[Site] = {
    ("apis/app_api/skills/service.py", "SkillCatalogService.create_skill"),
    ("apis/app_api/skills/service.py", "SkillCatalogService.update_skill"),
    ("apis/app_api/skills/service.py", "SkillCatalogService.add_resource"),
    ("apis/app_api/skills/service.py", "SkillCatalogService.delete_resource"),
    ("apis/app_api/skills/service.py", "SkillCatalogService.import_bundle"),
    ("apis/app_api/skills/service.py", "SkillCatalogService._create_from_bundle"),
    ("apis/shared/skills/user_service.py", "UserSkillService.create_my_skill"),
    ("apis/shared/skills/user_service.py", "UserSkillService.update_my_skill"),
    ("apis/shared/skills/user_service.py", "UserSkillService.add_resource"),
    ("apis/shared/skills/user_service.py", "UserSkillService.delete_resource"),
    ("apis/shared/skills/user_service.py", "UserSkillService.import_bundle"),
    ("apis/shared/skills/user_service.py", "UserSkillService._create_from_bundle"),
    ("apis/shared/skills/history.py", "restore_version"),
}

EXEMPT: Dict[Site, str] = {
    ("apis/shared/skills/bundle_files.py", "SkillBundleFiles._persist_resources"): (
        "the manifest write behind SkillBundleFiles.add_resource/delete_resource; "
        "both services capture around those calls"
    ),
}


def _is_skill_receiver(receiver: str) -> bool:
    return "repo" in receiver or "bundle_files" in receiver


def _write_sites() -> Iterator[Tuple[Site, ast.AST]]:
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(SRC).as_posix()
        yield from _visit(tree, rel, [])


def _visit(node: ast.AST, rel: str, stack: list) -> Iterator[Tuple[Site, ast.AST]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.append(child)
            yield from _visit(child, rel, stack)
            stack.pop()
            continue
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in WRITE_METHODS
            and _is_skill_receiver(ast.unparse(child.func.value))
            and stack
        ):
            name = ".".join(n.name for n in stack)
            yield (rel, name), stack[-1]
        yield from _visit(child, rel, stack)


def _calls_capture(function: ast.AST) -> bool:
    """A call to a service's ``_capture`` or to ``history.capture`` itself."""
    for n in ast.walk(function):
        if not isinstance(n, ast.Call):
            continue
        name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
        if name in ("_capture", "capture"):
            return True
    return False


def test_every_skill_write_site_is_listed():
    found = {site for site, _ in _write_sites()}
    unlisted = sorted(found - CAPTURES - set(EXEMPT))
    assert not unlisted, (
        "New skill write site(s) not covered by version history: "
        f"{unlisted}. Capture after the write and add it to CAPTURES, "
        "or add it to EXEMPT with the reason it cannot change content."
    )


def test_listed_sites_still_exist():
    found = {site for site, _ in _write_sites()}
    stale = sorted((CAPTURES | set(EXEMPT)) - found)
    assert not stale, f"Listed skill write sites no longer exist: {stale}. Remove them."


def test_capturing_sites_call_capture():
    missing = sorted(
        site
        for site, function in _write_sites()
        if site in CAPTURES and not _calls_capture(function)
    )
    assert not missing, f"These skill writes are listed as capturing but never call _capture: {missing}"
