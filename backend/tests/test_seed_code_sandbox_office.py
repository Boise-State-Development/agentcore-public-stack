"""The seed script's Node half: docx-js on the office mount (spec D9).

What the sandbox runs is whatever this lockfile names, installed on an
operator's machine and copied onto a file system every user's session mounts.
So these pin the lockfile's own guarantees (exact, hashed, registry-only, no
install scripts) and that the folder the script fills is the one ``run_code``
puts on ``NODE_PATH``. The install itself (``npm ci``) needs the network and is
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


def test_the_committed_lockfile_passes_its_own_check():
    locked = seed.check_lockfile(LOCK)
    assert locked["node_modules/docx"] == MANIFEST["dependencies"]["docx"]


def test_docx_is_the_only_direct_dependency_and_pinned_exactly():
    assert MANIFEST["dependencies"] == {"docx": MANIFEST["dependencies"]["docx"]}
    assert MANIFEST["dependencies"]["docx"][0].isdigit()
    assert "devDependencies" not in MANIFEST
    assert LOCK["packages"][""]["dependencies"] == MANIFEST["dependencies"]


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
    command = seed._docx_check_command("/mnt/office/releases/r 1/node/node_modules")
    parts = shlex.split(command.split("&& ", 1)[1])
    assert parts[0] == "NODE_PATH=/mnt/office/releases/r 1/node/node_modules"
    assert parts[1:3] == ["node", "-e"]
    assert parts[3] == seed.DOCX_CHECK_JS
    assert command.startswith('cd "$(mktemp -d)" && ')
