"""``run_code``: skill scripts and model-written Python in the isolated sandbox.

The sandbox is a fake pool entry that really runs what it is sent, in a temp
directory: ``writeFiles`` writes there, ``readFiles`` reads from there, and
``executeCommand`` runs the command with ``sh`` (the in-sandbox ``timeout`` is
stripped, since macOS has none). So the snapshot / entry / finish programs and
the base64 staging run end to end instead of being string-matched.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import agents.builtin_tools.code_sandbox.run_code_tool as rc
from agents.builtin_tools.code_sandbox import make_run_code_tool
from agents.builtin_tools.spreadsheet_analysis import sandbox_pool
from agents.main_agent.skills.skill_scripts import TurnSkillRecords
from apis.shared.skills.models import SkillResourceRef

USER = "user-1"
SESSION = "sess-1"


class FakeEntry:
    """A pooled sandbox entry backed by a temp directory."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.lock = asyncio.Lock()
        self.closed = False
        self.started_with: List[str] = []
        self.commands: List[str] = []
        self.stopped = False

    async def start(self, ci_id: str) -> None:
        self.started_with.append(ci_id)

    async def stop(self) -> None:
        self.stopped = True
        self.closed = True

    async def invoke(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "writeFiles":
            for item in params["content"]:
                path = self.root / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(item["text"], encoding="utf-8")
            return {"stream": [{"result": {"isError": False}}]}
        if method == "readFiles":
            path = self.root / params["paths"][0]
            if not path.exists():
                return {"stream": [{"result": {"content": []}}]}
            data = base64.b64encode(path.read_bytes()).decode()
            return {"stream": [{"result": {"content": [{"data": data}]}}]}
        if method == "executeCommand":
            command = params["command"]
            self.commands.append(command)
            command = re.sub(r"timeout -k \d+ \d+ ", "", command).replace("python3", sys.executable)
            proc = subprocess.run(["sh", "-c", command], cwd=self.root, capture_output=True, text=True, timeout=60)
            return {
                "stream": [
                    {
                        "result": {
                            "isError": proc.returncode != 0,
                            "structuredContent": {
                                "stdout": proc.stdout,
                                "stderr": proc.stderr,
                                "exitCode": proc.returncode,
                            },
                        }
                    }
                ]
            }
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    entry = FakeEntry(tmp_path / "sandbox")
    entry.root.mkdir()
    monkeypatch.setattr(sandbox_pool, "acquire", lambda session_id, user_id: entry)
    monkeypatch.setattr(rc, "_get_code_interpreter_id", lambda: "ci-vpc")
    monkeypatch.setattr(rc, "_network_mode", lambda interpreter_id: "VPC")
    rc._reset_isolation_cache()
    yield entry
    rc._reset_isolation_cache()


@pytest.fixture
def saved(monkeypatch):
    stored: List[Dict[str, Any]] = []

    async def fake_store(user_id, session_id, filename, data, mime):
        stored.append({"filename": filename, "bytes": data, "mime": mime})
        return f"upl-{len(stored)}", f"{len(data) / 1024:.1f} KB"

    monkeypatch.setattr(rc, "_store_document", fake_store)
    return stored


@pytest.fixture
def audits(monkeypatch):
    seen: List[tuple] = []
    monkeypatch.setattr(rc, "_audit_script_run", lambda *a: seen.append(("script", a)))
    monkeypatch.setattr(rc, "_audit_code_run", lambda *a: seen.append(("code", a)))
    return seen


def _skill(owner: str = USER, files: Dict[str, bytes] | None = None):
    files = files or {}
    resources = []
    for path, data in files.items():
        kind = "script" if path.startswith("scripts/") else "reference"
        resources.append(
            SkillResourceRef(
                filename=path.split("/")[-1],
                content_hash=hashlib.sha256(data).hexdigest(),
                content_type="text/x-python" if path.endswith(".py") else "text/markdown",
                size=len(data),
                s3_key=f"k/{hashlib.sha256(data).hexdigest()}",
                kind=kind,
                path=path.split("/", 1)[1],
            )
        )
    return SimpleNamespace(skill_id="pdf", owner_id=owner, resources=resources)


@pytest.fixture
def skill_store(monkeypatch):
    blobs: Dict[str, bytes] = {}
    import apis.shared.skills.resource_store as store_mod

    monkeypatch.setattr(store_mod, "get_skill_resource_store", lambda: SimpleNamespace(get=lambda key: blobs[key]))
    return blobs


def _bind(skill, blobs: Dict[str, bytes], files: Dict[str, bytes]) -> TurnSkillRecords:
    from agents.main_agent.skills.strands_mapping import _manifest_path

    for ref in skill.resources:
        blobs[ref.s3_key] = files[_manifest_path(ref)]
    holder = TurnSkillRecords()
    holder.bind([skill])
    return holder


def _call(holder: TurnSkillRecords | None = None, **kwargs: Any) -> Dict[str, Any]:
    return asyncio.run(
        rc._run(
            SESSION, USER, holder or TurnSkillRecords(), kwargs.get("code"), kwargs.get("skill_name"),
            kwargs.get("path"), kwargs.get("argv"), kwargs.get("files"), kwargs.get("show_images"),
            kwargs.get("language"),
        )
    )


def _text(result: Dict[str, Any]) -> str:
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Running code and scripts
# ---------------------------------------------------------------------------


def test_code_runs_in_its_own_process_and_reports_output(sandbox, saved, audits):
    result = _call(code="import sys\nprint('hello from the sandbox')\nprint(sys.argv[0])")
    assert result["status"] == "success"
    text = _text(result)
    assert "finished (exit code 0)" in text
    assert "hello from the sandbox" in text
    assert ".run_code/snippet.py" in text
    assert sandbox.started_with == ["ci-vpc"]
    assert audits and audits[0][0] == "code"
    # The code's hash and size reach the audit, never its text.
    assert "hello" not in json.dumps([str(a) for a in audits[0][1][3:]])


def test_failing_code_is_an_error_with_its_traceback(sandbox, saved, audits):
    result = _call(code="raise ValueError('bad input')")
    assert result["status"] == "error"
    assert "failed (exit code 1)" in _text(result)
    assert "ValueError: bad input" in _text(result)


def test_skill_script_runs_with_argv_and_sibling_imports(sandbox, saved, audits, skill_store):
    files = {
        "scripts/helper.py": b"def shout(s):\n    return s.upper()\n",
        "scripts/fill.py": (
            b"import sys\nfrom helper import shout\n"
            b"open(sys.argv[2], 'w').write(shout(open(sys.argv[1]).read()))\n"
            b"print('wrote', sys.argv[2])\n"
        ),
        "references/forms.md": b"# forms\n",
    }
    skill = _skill(files=files)
    holder = _bind(skill, skill_store, files)
    (sandbox.root / "inputs").mkdir()
    (sandbox.root / "inputs" / "in.txt").write_text("quiet words")
    result = _call(holder, skill_name="pdf", path="scripts/fill.py", argv=["inputs/in.txt", "outputs/out.txt"])
    assert result["status"] == "success", _text(result)
    assert (sandbox.root / "outputs" / "out.txt").read_text() == "QUIET WORDS"
    # The whole bundle is staged, references included.
    assert (sandbox.root / "skills" / "pdf" / "references" / "forms.md").exists()
    assert saved == [{"filename": "out.txt", "bytes": b"QUIET WORDS", "mime": "text/plain"}]
    assert any('"ui_type": "file_download"' in c.get("text", "") for c in result["content"][1:])
    kind, args = audits[0]
    assert kind == "script"
    assert args[2] == "scripts/fill.py"
    assert args[3] == hashlib.sha256(files["scripts/fill.py"]).hexdigest()
    assert args[4] == "ok"


def test_script_exit_code_comes_back(sandbox, saved, audits, skill_store):
    files = {"scripts/check.py": b"import sys\nprint('usage: check.py <file>')\nsys.exit(2)\n"}
    holder = _bind(_skill(files=files), skill_store, files)
    result = _call(holder, skill_name="pdf", path="scripts/check.py")
    assert result["status"] == "error"
    assert "exit code 2" in _text(result)
    assert "usage: check.py" in _text(result)


def test_a_skill_by_another_user_is_refused_before_any_sandbox(sandbox, saved, audits, skill_store):
    files = {"scripts/x.py": b"print(1)\n"}
    holder = _bind(_skill(owner="someone-else", files=files), skill_store, files)
    result = _call(holder, skill_name="pdf", path="scripts/x.py")
    assert result["status"] == "error"
    assert "another user" in _text(result)
    assert sandbox.commands == [] and sandbox.started_with == []
    assert audits == []


def test_an_admin_catalog_skill_runs(sandbox, saved, audits, skill_store):
    files = {"scripts/x.py": b"print('admin skill')\n"}
    holder = _bind(_skill(owner="system", files=files), skill_store, files)
    assert _call(holder, skill_name="pdf", path="scripts/x.py")["status"] == "success"


def test_a_reference_file_cannot_run(sandbox, saved, audits, skill_store):
    files = {"references/notes.md": b"print('no')\n"}
    holder = _bind(_skill(files=files), skill_store, files)
    result = _call(holder, skill_name="pdf", path="references/notes.md")
    assert result["status"] == "error"
    assert "not a script" in _text(result)


# ---------------------------------------------------------------------------
# JavaScript (spec D2): a skill's .js/.cjs/.mjs and `language: "javascript"`
# ---------------------------------------------------------------------------

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def test_code_defaults_to_python_and_audits_its_language(sandbox, saved, audits):
    result = _call(code="print('still python')")
    assert result["status"] == "success"
    assert not (sandbox.root / ".run_code" / "snippet.js").exists()
    assert audits[0][1][5] == "python"


@needs_node
def test_javascript_code_runs_with_node_and_saves_its_outputs(sandbox, saved, audits):
    code = (
        "const fs = require('fs');\n"
        "fs.writeFileSync('outputs/hello.txt', 'from node ' + process.argv[1].split('/').pop());\n"
        "console.log('node', process.version);\n"
    )
    result = _call(code=code, language="javascript")
    assert result["status"] == "success", _text(result)
    assert "node v" in _text(result)
    assert (sandbox.root / ".run_code" / "snippet.js").read_text() == code
    assert saved == [{"filename": "hello.txt", "bytes": b"from node snippet.js", "mime": "text/plain"}]
    kind, args = audits[0]
    assert kind == "code" and args[5] == "javascript"


@needs_node
def test_javascript_errors_and_exit_codes_come_back(sandbox, saved, audits):
    result = _call(code="throw new Error('bad js input')", language="JavaScript")
    assert result["status"] == "error"
    assert "failed (exit code 1)" in _text(result)
    assert "bad js input" in _text(result)
    result = _call(code="process.exit(3)", language="javascript")
    assert "exit code 3" in _text(result)


def test_an_unknown_language_is_refused_before_any_sandbox(sandbox, saved, audits):
    result = _call(code="print('x')", language="ruby")
    assert result["status"] == "error"
    assert '"python" (the default) or "javascript"' in _text(result)
    assert sandbox.commands == [] and audits == []


@needs_node
@pytest.mark.parametrize("name", ["build.js", "build.cjs"])
def test_a_skill_js_script_runs_with_argv_and_sibling_requires(sandbox, saved, audits, skill_store, name):
    files = {
        "scripts/helper.js": b"module.exports = (s) => s.toUpperCase();\n",
        f"scripts/{name}": (
            b"const fs = require('fs');\nconst shout = require('./helper.js');\n"
            b"const [src, dst] = process.argv.slice(2);\n"
            b"fs.writeFileSync(dst, shout(fs.readFileSync(src, 'utf8')));\n"
            b"console.log('wrote', dst);\n"
        ),
    }
    holder = _bind(_skill(files=files), skill_store, files)
    (sandbox.root / "inputs").mkdir()
    (sandbox.root / "inputs" / "in.txt").write_text("quiet words")
    result = _call(holder, skill_name="pdf", path=f"scripts/{name}", argv=["inputs/in.txt", "outputs/out.txt"])
    assert result["status"] == "success", _text(result)
    assert f"Ran skills/pdf/scripts/{name}" in _text(result)
    assert (sandbox.root / "outputs" / "out.txt").read_text() == "QUIET WORDS"
    kind, args = audits[0]
    assert kind == "script"
    assert args[2] == f"scripts/{name}"
    assert args[3] == hashlib.sha256(files[f"scripts/{name}"]).hexdigest()
    assert args[4] == "ok"


@needs_node
def test_a_skill_mjs_script_runs_as_a_module_and_its_exit_code_comes_back(sandbox, saved, audits, skill_store):
    files = {"scripts/check.mjs": b"import fs from 'fs';\nconsole.log('usage: check.mjs', fs.existsSync('inputs'));\nprocess.exit(2);\n"}
    holder = _bind(_skill(files=files), skill_store, files)
    result = _call(holder, skill_name="pdf", path="scripts/check.mjs")
    assert result["status"] == "error"
    assert "exit code 2" in _text(result)
    assert "usage: check.mjs true" in _text(result)
    assert audits[0][1][4] == "error"


def test_a_script_that_is_neither_python_nor_javascript_is_refused(sandbox, saved, audits, skill_store):
    files = {"scripts/run.sh": b"echo no\n"}
    holder = _bind(_skill(files=files), skill_store, files)
    result = _call(holder, skill_name="pdf", path="scripts/run.sh")
    assert result["status"] == "error"
    assert "Only .py, .js, .cjs and .mjs scripts can be run" in _text(result)
    assert sandbox.commands == [] and audits == []


def test_a_javascript_reference_is_refused_with_a_js_fix(sandbox, saved, audits, skill_store):
    files = {"references/theme.js": b"console.log('no')\n"}
    holder = _bind(_skill(files=files), skill_store, files)
    result = _call(holder, skill_name="pdf", path="references/theme.js")
    assert result["status"] == "error"
    assert "not a script" in _text(result)
    assert "scripts/theme.js" in _text(result)
    assert sandbox.commands == []


def test_a_js_skill_by_another_user_waits_for_the_approval_card(sandbox, saved, audits, skill_store):
    files = {"scripts/x.js": b"console.log(1)\n"}
    holder = _bind(_skill(owner="someone-else", files=files), skill_store, files)
    result = _call(holder, skill_name="pdf", path="scripts/x.js")
    assert "another user" in _text(result)
    assert sandbox.commands == [] and audits == []


def test_the_entry_program_sends_exactly_the_node_extensions_to_node():
    literal = ", ".join(f'"{ext}"' for ext in rc.programs.NODE_EXTENSIONS)
    assert f"in ({literal}):" in rc.programs.ENTRY_PY
    assert rc.SCRIPT_EXTENSIONS == (".py", ".js", ".cjs", ".mjs")


# ---------------------------------------------------------------------------
# Outputs and images
# ---------------------------------------------------------------------------


def test_only_new_or_changed_outputs_are_saved_and_unsaveable_types_are_named(sandbox, saved, audits):
    _call(code="import os\nos.makedirs('outputs', exist_ok=True)\nopen('outputs/a.csv','w').write('x,y\\n1,2\\n')")
    assert [s["filename"] for s in saved] == ["a.csv"]
    result = _call(code="open('outputs/b.json','w').write('{}')\nopen('outputs/c.md','w').write('# hi')")
    assert [s["filename"] for s in saved] == ["a.csv", "c.md"]
    assert "outputs/b.json: .json files can't be saved" in _text(result)


def test_requested_images_come_back_downscaled(sandbox, saved, audits):
    code = (
        "from PIL import Image\nimport os\nos.makedirs('outputs', exist_ok=True)\n"
        "Image.new('RGB', (3000, 2000), 'white').save('outputs/page_1.png')\n"
    )
    result = _call(code=code, show_images=["outputs/page_1.png", "../etc/passwd"])
    images = [c for c in result["content"] if "image" in c]
    assert len(images) == 1
    from PIL import Image

    with Image.open(io.BytesIO(images[0]["image"]["source"]["bytes"])) as im:
        w, h = im.size
    assert max(w, h) <= rc.IMAGE_MAX_EDGE and w * h <= rc.IMAGE_MAX_PIXELS
    assert "../etc/passwd: not shown" in _text(result)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def test_conversation_files_are_copied_into_inputs(sandbox, saved, audits, monkeypatch):
    row = SimpleNamespace(upload_id="u1", filename="Q3 Report.csv", user_id=USER, size_bytes=8,
                          s3_bucket="b", s3_key="k")

    async def files(session_id, user_id):
        return [row]

    async def view(meta, *, reader, repository=None):
        return meta

    import apis.shared.files.linked_files as linked

    monkeypatch.setattr(rc, "_session_files", files)
    monkeypatch.setattr(linked, "readable_view", view)
    monkeypatch.setattr(rc, "_download_s3_bytes", lambda bucket, key: b"a,b\n1,2\n")
    result = _call(code="print(open('inputs/Q3_Report.csv').read())", files=["Q3 Report.csv"])
    assert result["status"] == "success", _text(result)
    assert "a,b" in _text(result)
    missing = _call(code="print(1)", files=["nope.csv"])
    assert missing["status"] == "error" and "Q3 Report.csv" in _text(missing)


# ---------------------------------------------------------------------------
# Refusals and limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["SANDBOX", "PUBLIC"])
def test_refuses_an_interpreter_that_is_not_isolated(sandbox, saved, audits, monkeypatch, mode):
    monkeypatch.setattr(rc, "_network_mode", lambda interpreter_id: mode)
    rc._reset_isolation_cache()
    result = _call(code="print(1)")
    assert result["status"] == "error"
    assert "not the isolated" in _text(result)
    assert sandbox.commands == []


def test_an_unreadable_network_mode_refuses_and_is_not_cached(sandbox, saved, audits, monkeypatch):
    def boom(interpreter_id):
        raise RuntimeError("throttled")

    monkeypatch.setattr(rc, "_network_mode", boom)
    rc._reset_isolation_cache()
    assert "could not confirm" in _text(_call(code="print(1)"))
    monkeypatch.setattr(rc, "_network_mode", lambda interpreter_id: "VPC")
    assert _call(code="print(1)")["status"] == "success"


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({}, "exactly one"),
        ({"code": "print(1)", "skill_name": "pdf", "path": "scripts/x.py"}, "exactly one"),
        ({"skill_name": "pdf"}, "both"),
        ({"code": "print(1)", "argv": ["a"]}, "`argv` is for a skill script"),
        ({"code": "x" * (rc.MAX_CODE_BYTES + 1)}, "over 64 KB"),
        ({"code": "print(1)", "show_images": ["a", "b", "c", "d", "e"]}, "at most 4"),
    ],
)
def test_argument_problems_are_explained(sandbox, saved, audits, kwargs, fragment):
    result = _call(**kwargs)
    assert result["status"] == "error"
    assert fragment in _text(result)
    assert sandbox.commands == []


def test_calls_per_turn_are_capped(sandbox, saved, audits):
    rc._state_of(sandbox).calls = rc.MAX_CALLS_PER_TURN
    result = _call(code="print(1)")
    assert "its limit" in _text(result)
    assert sandbox.commands == []


def test_a_private_sandbox_is_stopped_when_no_turn_is_registered(tmp_path, monkeypatch, saved, audits):
    entry = FakeEntry(tmp_path / "private")
    entry.root.mkdir()
    monkeypatch.setattr(sandbox_pool, "acquire", lambda session_id, user_id: None)
    monkeypatch.setattr(sandbox_pool, "new_transient_entry", lambda session_id, user_id: entry)
    monkeypatch.setattr(rc, "_get_code_interpreter_id", lambda: "ci-vpc")
    monkeypatch.setattr(rc, "_network_mode", lambda interpreter_id: "VPC")
    rc._reset_isolation_cache()
    assert _call(code="print(1)")["status"] == "success"
    assert entry.stopped


def test_the_run_command_times_out_inside_the_sandbox():
    command = rc.programs.run_command(rc.CALL_TIMEOUT_SECONDS)
    assert f"timeout -k 5 {rc.CALL_TIMEOUT_SECONDS} python3 .run_code/entry.py" in command
    assert command.endswith("python3 .run_code/finish.py $?")


# ---------------------------------------------------------------------------
# The spec, the flag and the notice
# ---------------------------------------------------------------------------

RUN_CODE_SPEC_DIGEST = "299c379606eb30b36420e146558732f5fa1112d9857facca69f7ad64c6d8e317"


def test_spec_is_pinned_and_says_there_is_no_network():
    spec = make_run_code_tool(SESSION, USER, TurnSkillRecords()).tool_spec
    assert spec["name"] == "run_code"
    assert "NO network access" in spec["description"]
    props = spec["inputSchema"]["json"]["properties"]
    assert set(props) == {"code", "skill_name", "path", "argv", "files", "show_images", "language"}
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    assert digest == RUN_CODE_SPEC_DIGEST


def test_builder_needs_the_flag_and_the_catalog_id(monkeypatch):
    from apis.inference_api.chat.routes import _build_code_sandbox_tools

    monkeypatch.delenv("CODE_SANDBOX_ENABLED", raising=False)
    assert _build_code_sandbox_tools(["run_code"], SESSION, USER) == []
    monkeypatch.setenv("CODE_SANDBOX_ENABLED", "true")
    assert _build_code_sandbox_tools(["create_word_document"], SESSION, USER) == []
    tools = _build_code_sandbox_tools(["run_code"], SESSION, USER)
    assert [t.tool_spec["name"] for t in tools] == ["run_code"]
    # ChatAgent fills the holder through the same attribute scan as the Word tools.
    from agents.main_agent.skills.skill_scripts import bind_turn_skill_records

    assert bind_turn_skill_records(tools, [_skill()]) == 1


@pytest.mark.parametrize(
    "code_on, word_on, expect, absent",
    [
        ("", "", "NOT executable", "run_code"),
        ("", "true", "create_word_document", "run_code"),
        ("true", "", "run it with run_code", "create_word_document"),
        ("true", "true", "run it with run_code", None),
    ],
)
def test_read_skill_file_notice_follows_the_flags(monkeypatch, code_on, word_on, expect, absent):
    from agents.main_agent.skills.strands_mapping import _script_notice

    monkeypatch.setenv("CODE_SANDBOX_ENABLED", code_on)
    monkeypatch.setenv("WORD_SKILL_SCRIPTS_ENABLED", word_on)
    notice = _script_notice()
    assert expect in notice
    if absent:
        assert absent not in notice
    if code_on and word_on:
        assert "create_word_document" in notice


def test_run_code_is_injected_and_key_described():
    from apis.shared.tools.injected import (
        CODE_SANDBOX_TOOL_IDS,
        INJECTED_TOOL_IDS,
        KEY_DESCRIBED_INJECTED_TOOL_IDS,
    )

    assert CODE_SANDBOX_TOOL_IDS == frozenset({"run_code"})
    assert "run_code" in INJECTED_TOOL_IDS
    assert "run_code" in KEY_DESCRIBED_INJECTED_TOOL_IDS


def test_seed_row_is_off_by_default_and_not_public():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "scripts"))
    from seed_bootstrap_data import DEFAULT_TOOLS  # noqa: E402

    row = next(r for r in DEFAULT_TOOLS if r["toolId"] == "run_code")
    assert row["enabledByDefault"] is False
    assert row["isPublic"] is False


# ---------------------------------------------------------------------------
# The storage quota every generated file is held to
# ---------------------------------------------------------------------------


class _QuotaRepo:
    def __init__(self, used: int) -> None:
        self.used = used
        self.created: List[Any] = []
        self.incremented: List[int] = []

    async def get_user_quota(self, user_id):
        return SimpleNamespace(total_bytes=self.used)

    async def create_file(self, metadata):
        self.created.append(metadata)

    async def increment_quota(self, user_id, size_bytes):
        self.incremented.append(size_bytes)


def _patch_storage(monkeypatch, repo: _QuotaRepo) -> List[str]:
    import agents.builtin_tools.office._storage as storage
    import apis.shared.files as files_pkg

    puts: List[str] = []
    monkeypatch.setattr(files_pkg, "get_file_upload_repository", lambda: repo)
    monkeypatch.setattr(storage, "_user_files_bucket", lambda: "bucket")
    monkeypatch.setattr(storage, "_s3", lambda: SimpleNamespace(put_object=lambda **kw: puts.append(kw["Key"])))

    async def no_preview(*args):
        return None

    monkeypatch.setattr(storage, "_store_card_preview", no_preview)
    monkeypatch.setenv("FILE_UPLOAD_USER_QUOTA_BYTES", "100")
    return puts


def test_store_document_refuses_past_the_quota_and_writes_nothing(monkeypatch):
    import agents.builtin_tools.office._storage as storage

    repo = _QuotaRepo(used=95)
    puts = _patch_storage(monkeypatch, repo)
    with pytest.raises(storage.StorageQuotaExceededError, match="file storage limit"):
        asyncio.run(storage._store_document(USER, SESSION, "big.csv", b"x" * 10, "text/csv"))
    assert puts == [] and repo.created == [] and repo.incremented == []


def test_store_document_counts_what_it_saves(monkeypatch):
    import agents.builtin_tools.office._storage as storage

    repo = _QuotaRepo(used=10)
    puts = _patch_storage(monkeypatch, repo)
    upload_id, size_kb = asyncio.run(storage._store_document(USER, SESSION, "a.csv", b"x" * 10, "text/csv"))
    assert len(puts) == 1 and len(repo.created) == 1
    assert repo.incremented == [10]
