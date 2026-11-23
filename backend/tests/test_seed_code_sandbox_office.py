"""The seed script's Node half: the Office skills' packages on the office mount (spec D9).

What the sandbox runs is whatever this lockfile names, installed on an
operator's machine and copied onto a file system every user's session mounts.
So these pin the lockfile's own guarantees (exact, hashed, registry-only, no
install scripts, sharp's native packages optional so they are never
installed) and that the folder the script fills is the one ``run_code`` puts
on ``NODE_PATH``. The install itself (``npm ci``) needs the network and is
exercised by the script's dry run and the seeding session's own check.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import shlex

import pytest

from agents.builtin_tools.code_sandbox import office_mount

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "seed_code_sandbox_office.py"
_spec = importlib.util.spec_from_file_location("seed_code_sandbox_office", _SCRIPT)
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)  # type: ignore[union-attr]

MANIFEST = json.loads((seed.NODE_MANIFEST_DIR / "package.json").read_text())
LOCK = json.loads((seed.NODE_MANIFEST_DIR / "package-lock.json").read_text())


#: What the docx and pptx skills load, and nothing else at the top level.
SKILL_PACKAGES = {"docx", "pptxgenjs", "react", "react-dom", "react-icons", "sharp", "@img/sharp-wasm32"}


def test_the_committed_lockfile_passes_its_own_check():
    locked = seed.check_lockfile(LOCK)
    for name in SKILL_PACKAGES:
        assert locked[f"node_modules/{name}"] == MANIFEST["dependencies"][name]
    # apply_theme.js loads the jszip that comes with pptxgenjs.
    assert "node_modules/jszip" in locked


def test_the_skills_packages_are_the_only_direct_dependencies_and_pinned_exactly():
    assert set(MANIFEST["dependencies"]) == SKILL_PACKAGES
    assert all(version[0].isdigit() for version in MANIFEST["dependencies"].values())
    assert "devDependencies" not in MANIFEST
    assert "optionalDependencies" not in MANIFEST
    assert LOCK["packages"][""]["dependencies"] == MANIFEST["dependencies"]


def test_only_sharps_native_platform_packages_are_optional_and_none_is_installed():
    optional = {p for p, e in LOCK["packages"].items() if e.get("optional")}
    assert optional, "sharp's native platform packages should be in the lockfile as optional"
    assert all(p.startswith("node_modules/@img/sharp-") for p in optional)
    assert "node_modules/@img/sharp-wasm32" not in optional
    assert not optional & set(seed.check_lockfile(LOCK))


def test_an_optional_package_is_still_held_to_the_hash_and_script_rules():
    lock = copy.deepcopy(LOCK)
    path = next(p for p, e in lock["packages"].items() if e.get("optional"))
    lock["packages"][path]["hasInstallScript"] = True
    with pytest.raises(SystemExit, match="install script"):
        seed.check_lockfile(lock)


def test_npm_ci_omits_optional_packages_and_sharps_sources_are_removed():
    source = _SCRIPT.read_text()
    assert '"--omit=optional"' in source
    assert seed.UNBUILT_SOURCES == ("sharp/src",)


def test_the_seeded_folder_is_the_one_run_code_puts_on_node_path():
    assert office_mount.NODE_MODULES_DIR == f"{seed.MOUNT_PATH}/current/{seed.NODE_DIR}/node_modules"


def _lock_with(**changes):
    lock = copy.deepcopy(LOCK)
    lock["packages"]["node_modules/docx"].update(changes)
    return lock


@pytest.mark.parametrize("changes, message", [
    ({"resolved": "https://example.com/docx.tgz"}, "hashed npm registry"),
    ({"integrity": ""}, "hashed npm registry"),
    ({"integrity": "sha1-abc"}, "hashed npm registry"),
    ({"hasInstallScript": True}, "install script"),
    ({"gypfile": True}, "install script"),
])
def test_a_package_that_is_not_hashed_or_pure_js_is_refused(changes, message):
    with pytest.raises(SystemExit, match=message):
        seed.check_lockfile(_lock_with(**changes))


def test_a_range_pin_is_refused():
    lock = copy.deepcopy(LOCK)
    lock["packages"][""]["dependencies"] = {"docx": "^9.16.0"}
    with pytest.raises(SystemExit, match="exact version"):
        seed.check_lockfile(lock)


def test_an_old_lockfile_format_is_refused():
    lock = copy.deepcopy(LOCK)
    lock["lockfileVersion"] = 2
    with pytest.raises(SystemExit, match="lockfileVersion 3"):
        seed.check_lockfile(lock)


def test_the_sandbox_check_runs_from_a_scratch_folder_with_only_node_path():
    command = seed._node_check_command("/mnt/office/releases/r 1/node/node_modules")
    parts = shlex.split(command.split("&& ", 1)[1])
    assert parts[0] == "NODE_PATH=/mnt/office/releases/r 1/node/node_modules"
    assert parts[1:3] == ["node", "-e"]
    assert parts[3] == seed.NODE_CHECK_JS
    assert command.startswith('cd "$(mktemp -d)" && ')


def test_the_check_exercises_every_skill_path():
    check = seed.NODE_CHECK_JS
    for needle in ("require('docx')", "require('pptxgenjs')", "require('sharp')", "react-dom/server",
                   "react-icons/fa", "paths:[require.resolve('pptxgenjs')]", "sharp.versions.emscripten"):
        assert needle in check


@pytest.mark.parametrize("output, passed", [
    ("docx ok 9454\nsharp wasm ok 3348\npptx ok 31 parts\n", True),
    ("docx ok 9454\npptx ok 31 parts\n", False),
    ("docx ok 9454\n", False),
    ("", False),
])
def test_the_check_passes_only_when_every_part_printed(output, passed):
    assert seed.node_check_passed(output) is passed
