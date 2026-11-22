"""LibreOffice for ``run_code``: the read-only mount its sessions start with (spec D9).

Uses the same temp-directory sandbox as ``test_run_code.py``: what the run
command does to ``PATH`` is checked by running it, against a fake ``soffice``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import stat
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import agents.builtin_tools.code_sandbox.run_code_tool as rc
from agents.builtin_tools.code_sandbox import office_mount
from agents.builtin_tools.spreadsheet_analysis import sandbox_pool

from .test_run_code import FakeEntry, _call, _text

FS_ARN = "arn:aws:elasticfilesystem:us-west-2:123456789012:file-system/fs-0123456789abcdef0"
RO_ARN = "arn:aws:elasticfilesystem:us-west-2:123456789012:access-point/fsap-0123456789abcdef0"
RW_ARN = "arn:aws:elasticfilesystem:us-west-2:123456789012:access-point/fsap-0fedcba9876543210"
PARAMETER = json.dumps({
    "fileSystemArn": FS_ARN,
    "readOnlyAccessPointArn": RO_ARN,
    "writeAccessPointArn": RW_ARN,
    "seederInterpreterId": "seeder-abc",
    "mountPath": "/mnt/office",
})


class MountingEntry(FakeEntry):
    """A fake entry that records the mounts it was started with, and can fail them."""

    def __init__(self, root: pathlib.Path, fail_mount: bool = False) -> None:
        super().__init__(root)
        self.fail_mount = fail_mount
        self.started = False
        self.mounted = False
        self.starts: List[Optional[List[Dict[str, Any]]]] = []
        self.resets = 0

    async def start(self, ci_id: str, filesystem_configurations: Optional[List[Dict[str, Any]]] = None) -> None:
        if self.started:
            return
        self.starts.append(filesystem_configurations)
        if filesystem_configurations and self.fail_mount:
            raise RuntimeError("EFS mount failed: resource not found")
        self.started_with.append(ci_id)
        self.started = True
        self.mounted = bool(filesystem_configurations)

    async def reset(self) -> None:
        self.resets += 1
        self.started = False
        self.mounted = False


@pytest.fixture
def saved(monkeypatch):
    stored: List[Dict[str, Any]] = []

    async def fake_store(user_id, session_id, filename, data, mime):
        stored.append({"filename": filename})
        return f"upl-{len(stored)}", "1 KB"

    monkeypatch.setattr(rc, "_store_document", fake_store)
    return stored


@pytest.fixture
def audits(monkeypatch):
    monkeypatch.setattr(rc, "_audit_script_run", lambda *a: None)
    monkeypatch.setattr(rc, "_audit_code_run", lambda *a: None)


@pytest.fixture
def office_on(monkeypatch):
    monkeypatch.setenv("CODE_SANDBOX_OFFICE_ENABLED", "true")
    monkeypatch.setenv("PROJECT_PREFIX", "test-prefix")
    reads: List[str] = []

    def read(name: str) -> str:
        reads.append(name)
        return PARAMETER

    monkeypatch.setattr(office_mount, "_read_parameter", read)
    office_mount._reset_for_tests()
    yield reads
    office_mount._reset_for_tests()


@pytest.fixture
def fake_office(tmp_path, monkeypatch):
    """A ``program/`` folder holding an ``soffice`` that says where it ran from."""
    program = tmp_path / "office" / "program"
    program.mkdir(parents=True)
    soffice = program / "soffice"
    soffice.write_text("#!/bin/sh\necho \"fake soffice $SAL_USE_VCLPLUGIN $*\"\n")
    soffice.chmod(soffice.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(office_mount, "PROGRAM_DIR", str(program))
    return program


def _use(entry: MountingEntry, monkeypatch) -> None:
    entry.root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sandbox_pool, "acquire", lambda session_id, user_id: entry)
    monkeypatch.setattr(rc, "_get_code_interpreter_id", lambda: "ci-vpc")
    monkeypatch.setattr(rc, "_network_mode", lambda interpreter_id: "VPC")
    rc._reset_isolation_cache()


RUN_SOFFICE = "import subprocess; print(subprocess.run(['soffice', '--version'], capture_output=True, text=True).stdout)"


# ---------------------------------------------------------------------------
# office_mount.session_mount
# ---------------------------------------------------------------------------


def test_no_mount_while_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("CODE_SANDBOX_OFFICE_ENABLED", raising=False)
    monkeypatch.setattr(office_mount, "_read_parameter", lambda name: pytest.fail("read while off"))
    office_mount._reset_for_tests()
    assert office_mount.session_mount() is None


def test_the_mount_is_the_read_only_access_point_and_read_once(office_on):
    first = office_mount.session_mount()
    second = office_mount.session_mount()
    assert first == second == [{
        "efsConfiguration": {"fileSystemArn": FS_ARN, "accessPointArn": RO_ARN, "mountPath": "/mnt/office"},
    }]
    assert RW_ARN not in json.dumps(first)
    assert office_on == ["/test-prefix/agentcore/code-sandbox-office-mount"]


@pytest.mark.parametrize("value", ["not json", "{}", json.dumps({"fileSystemArn": "x", "readOnlyAccessPointArn": "y"})])
def test_an_unusable_parameter_means_no_mount_and_is_read_again(office_on, monkeypatch, value):
    reads: List[str] = []
    monkeypatch.setattr(office_mount, "_read_parameter", lambda name: reads.append(name) or value)
    assert office_mount.session_mount() is None
    assert office_mount.session_mount() is None
    assert len(reads) == 2


def test_a_failed_mount_is_skipped_for_a_while(office_on, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(office_mount.time, "monotonic", lambda: now[0])
    office_mount.mark_failed()
    assert office_mount.session_mount() is None
    now[0] += office_mount.RETRY_AFTER_FAILURE_SECONDS + 1
    assert office_mount.session_mount() is not None


# ---------------------------------------------------------------------------
# The run command
# ---------------------------------------------------------------------------


def test_the_run_command_is_unchanged_without_the_mount():
    assert rc.programs.run_command(120) == rc.programs.run_command(120, None)
    assert "PATH=" not in rc.programs.run_command(120)


def test_the_run_command_puts_libreoffice_first_on_path():
    command = rc.programs.run_command(120, "/mnt/office/current/program")
    assert 'PATH=/mnt/office/current/program:"$PATH" SAL_USE_VCLPLUGIN=svp timeout -k 5 120 python3' in command


# ---------------------------------------------------------------------------
# run_code with the mount
# ---------------------------------------------------------------------------


def test_run_code_mounts_libreoffice_and_finds_soffice(tmp_path, monkeypatch, office_on, fake_office, saved, audits):
    entry = MountingEntry(tmp_path / "sandbox")
    _use(entry, monkeypatch)
    result = _call(code=RUN_SOFFICE)
    assert result["status"] == "success", _text(result)
    assert "fake soffice svp --version" in _text(result)
    assert entry.starts == [office_mount.session_mount()]


def test_without_the_flag_there_is_no_mount_and_no_soffice(tmp_path, monkeypatch, fake_office, saved, audits):
    monkeypatch.delenv("CODE_SANDBOX_OFFICE_ENABLED", raising=False)
    office_mount._reset_for_tests()
    entry = MountingEntry(tmp_path / "sandbox")
    _use(entry, monkeypatch)
    _call(code="print(1)")
    assert entry.starts == [None]
    assert all("PATH=" not in c for c in entry.commands)


def test_a_failed_mount_runs_without_libreoffice(tmp_path, monkeypatch, office_on, fake_office, saved, audits):
    entry = MountingEntry(tmp_path / "sandbox", fail_mount=True)
    _use(entry, monkeypatch)
    result = _call(code="print('ran')")
    assert result["status"] == "success", _text(result)
    assert "ran" in _text(result)
    assert [bool(s) for s in entry.starts] == [True, False]
    assert all("PATH=" not in c for c in entry.commands)
    # And the next call does not pay for another failed mount.
    assert office_mount.session_mount() is None


def test_a_session_started_without_the_mount_is_replaced(tmp_path, monkeypatch, office_on, fake_office, saved, audits):
    entry = MountingEntry(tmp_path / "sandbox")
    _use(entry, monkeypatch)
    # analyze_spreadsheet started this turn's sandbox first, with no mount.
    asyncio.run(entry.start("ci-vpc"))
    state = rc._state_of(entry)
    state.programs_written = True
    state.inputs["upl-1"] = "inputs/a.xlsx"
    result = _call(code=RUN_SOFFICE)
    assert result["status"] == "success", _text(result)
    assert entry.resets == 1
    assert entry.starts[-1] == office_mount.session_mount()
    assert state.inputs == {}
    assert "fake soffice" in _text(result)


def test_a_mounted_session_is_kept_for_the_next_call(tmp_path, monkeypatch, office_on, fake_office, saved, audits):
    entry = MountingEntry(tmp_path / "sandbox")
    _use(entry, monkeypatch)
    _call(code="print(1)")
    _call(code="print(2)")
    assert len(entry.starts) == 1
    assert entry.resets == 0
    assert all("SAL_USE_VCLPLUGIN=svp" in c for c in entry.commands)


# ---------------------------------------------------------------------------
# The pool entry's mounted start
# ---------------------------------------------------------------------------


def test_a_mounted_start_passes_the_mount_to_the_data_plane(monkeypatch):
    calls: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self, region: str) -> None:
            self.identifier = None
            self.session_id = None
            self.data_plane_client = SimpleNamespace(start_code_interpreter_session=self._start)
            self.stopped = False

        def _start(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(kwargs)
            return {"codeInterpreterIdentifier": kwargs["codeInterpreterIdentifier"], "sessionId": "s-1"}

        def start(self, **kwargs: Any) -> None:
            calls.append({"sdk": kwargs})

        def stop(self) -> None:
            self.stopped = True

    import bedrock_agentcore.tools.code_interpreter_client as sdk

    monkeypatch.setattr(sdk, "CodeInterpreter", FakeClient)
    mount = [{"efsConfiguration": {"fileSystemArn": FS_ARN, "accessPointArn": RO_ARN, "mountPath": "/mnt/office"}}]

    entry = sandbox_pool.SandboxEntry(("s", "u"), "tok", pooled=True)
    asyncio.run(entry.start("ci-vpc", filesystem_configurations=mount))
    assert entry.started and entry.mounted
    assert calls[0]["filesystemConfigurations"] == mount
    assert calls[0]["sessionTimeoutSeconds"] == sandbox_pool.sandbox_timeout_seconds()
    assert entry._client.session_id == "s-1"

    entry.staged["x"] = object()  # type: ignore[assignment]
    asyncio.run(entry.reset())
    assert not entry.started and not entry.mounted and entry.staged == {}
    assert not entry.closed

    plain = sandbox_pool.SandboxEntry(("s", "u"), None, pooled=False)
    asyncio.run(plain.start("ci-vpc"))
    assert calls[-1] == {"sdk": {"identifier": "ci-vpc"}}
    assert plain.started and not plain.mounted
