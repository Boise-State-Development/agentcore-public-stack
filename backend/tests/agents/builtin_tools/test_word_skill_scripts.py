"""Word tools running a bound skill's script in Code Interpreter (``WORD_SKILL_SCRIPTS_ENABLED``).

Resolution goes only through the turn's skill records; the sandbox gets the
script as a file plus a fixed entry; with the flag off both tool specs are
byte-identical to what they were before the feature existed.

The sandbox is a fake ``CodeInterpreter`` that really executes the code it is
sent, in a temp directory, against a stub ``docx`` module (python-docx is not a
backend dependency; it only exists inside Code Interpreter). That runs the
assembled preamble + entry end to end rather than string-matching it.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import traceback
import types
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import agents.builtin_tools.word_document_tool as word
import agents.main_agent.skills.skill_scripts as skill_scripts
from agents.builtin_tools.word_document_tool import (
    _SANDBOX_SKILL_SCRIPT,
    make_create_word_document_tool,
    make_modify_word_document_tool,
)
from agents.main_agent.skills.skill_scripts import (
    MAX_PARAMS_BYTES,
    SkillScriptError,
    TurnSkillRecords,
    attach_turn_skill_records,
    bind_turn_skill_records,
    load_skill_script,
    parse_script_params,
    resolve_skill_script,
)
from apis.shared.feature_flags import word_skill_scripts_enabled
from apis.shared.skills.bundle_files import MAX_RESOURCE_BYTES
from apis.shared.skills.models import SkillResourceRef

ENGINE = b'''
def run(doc, params):
    for clause in params.get("clauses", []):
        doc.add_paragraph("redlined " + clause)
    print("engine log line")
    return "applied %d change(s)" % len(params.get("clauses", []))
'''


def _ref(filename: str, kind: str, size: int = 100, content_type: str = "text/plain") -> SkillResourceRef:
    return SkillResourceRef(
        filename=filename,
        content_hash="0" * 64,
        size=size,
        content_type=content_type,
        s3_key=f"skills/contract-redliner/{kind}s/{filename}",
        kind=kind,
    )


def _record(skill_id: str = "contract-redliner", resources=None) -> Any:
    return SimpleNamespace(
        skill_id=skill_id,
        resources=list(
            resources
            if resources is not None
            else [
                _ref("engine.py", "script"),
                _ref("Engine.txt", "reference"),
                _ref("process.md", "reference", content_type="text/markdown"),
                _ref("run.sh", "script"),
            ]
        ),
    )


class _Store:
    def __init__(self, blobs: Dict[str, bytes]):
        self.blobs = blobs
        self.keys: List[str] = []

    def get(self, key: str) -> bytes:
        self.keys.append(key)
        return self.blobs[key]


@pytest.fixture()
def store(monkeypatch):
    s = _Store({"skills/contract-redliner/scripts/engine.py": ENGINE})
    monkeypatch.setattr(skill_scripts, "get_skill_resource_store", lambda: s)
    return s


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("", False), ("false", False), ("1", False), ("yes", False), ("true", True), (" TRUE ", True)],
)
def test_the_flag_is_off_unless_true(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("WORD_SKILL_SCRIPTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("WORD_SKILL_SCRIPTS_ENABLED", value)
    assert word_skill_scripts_enabled() is expected


# ---------------------------------------------------------------------------
# Resolution: only the turn's skills, only manifest paths, only scripts
# ---------------------------------------------------------------------------


class TestResolution:
    def test_a_skill_not_bound_on_the_turn_is_refused(self):
        with pytest.raises(SkillScriptError, match="not available in this conversation.*contract-redliner"):
            resolve_skill_script([_record()], "someone-elses-skill", "scripts/engine.py")

    def test_no_skills_on_the_turn_names_none(self):
        with pytest.raises(SkillScriptError, match=r"Available skills: \(none\)"):
            resolve_skill_script([], "contract-redliner", "scripts/engine.py")

    @pytest.mark.parametrize(
        "path",
        [
            "scripts/other.py",
            "../other-skill/scripts/engine.py",
            "skills/contract-redliner/scripts/engine.py",  # the S3 key itself
            "assets/engine.py",
            "",
        ],
    )
    def test_a_path_outside_the_manifest_is_refused(self, path):
        with pytest.raises(SkillScriptError, match="No file"):
            resolve_skill_script([_record()], "contract-redliner", path)

    @pytest.mark.parametrize("path", ["scripts/engine.py", "engine.py", "/scripts/engine.py"])
    def test_a_script_resolves_by_bundle_path_or_filename(self, path):
        ref = resolve_skill_script([_record()], "contract-redliner", path)
        assert ref.filename == "engine.py"

    def test_a_reference_is_refused_whatever_it_contains(self):
        with pytest.raises(SkillScriptError) as exc:
            resolve_skill_script([_record()], "contract-redliner", "references/Engine.txt")
        message = str(exc.value)
        assert "is a reference file, not a script" in message
        assert "scripts/Engine.py" in message

    def test_a_script_that_is_not_python_is_refused(self):
        with pytest.raises(SkillScriptError, match="not a Python file"):
            resolve_skill_script([_record()], "contract-redliner", "scripts/run.sh")

    def test_a_script_over_the_bundle_limit_is_refused_from_the_manifest(self):
        record = _record(resources=[_ref("engine.py", "script", size=MAX_RESOURCE_BYTES + 1)])
        with pytest.raises(SkillScriptError, match="at most 1048576 bytes"):
            resolve_skill_script([record], "contract-redliner", "scripts/engine.py")

    def test_the_bytes_are_checked_too(self, monkeypatch):
        big = _Store({"skills/contract-redliner/scripts/engine.py": b"#" * (MAX_RESOURCE_BYTES + 1)})
        monkeypatch.setattr(skill_scripts, "get_skill_resource_store", lambda: big)
        with pytest.raises(SkillScriptError, match="at most"):
            load_skill_script([_record()], "contract-redliner", "scripts/engine.py")

    def test_load_reads_only_the_manifest_key(self, store):
        script = load_skill_script([_record()], "contract-redliner", "engine.py")
        assert store.keys == ["skills/contract-redliner/scripts/engine.py"]
        assert script.source == ENGINE
        assert script.label == "contract-redliner/scripts/engine.py"


class TestParams:
    def test_absent_is_an_empty_object(self):
        assert parse_script_params(None) == "{}"

    def test_must_be_an_object(self):
        with pytest.raises(SkillScriptError, match="must be an object"):
            parse_script_params(["a"])

    def test_is_bounded(self):
        with pytest.raises(SkillScriptError, match="too large"):
            parse_script_params({"x": "a" * MAX_PARAMS_BYTES})


def test_bind_reaches_only_tools_that_carry_a_holder():
    holder = TurnSkillRecords()
    with_holder = attach_turn_skill_records(SimpleNamespace(), holder)
    without = SimpleNamespace()
    records = [_record()]
    assert bind_turn_skill_records([without, with_holder], records) == 1
    assert holder.records == records


# ---------------------------------------------------------------------------
# The sandbox: a fake Code Interpreter that really runs what it is sent
# ---------------------------------------------------------------------------


def _install_stub_docx(monkeypatch):
    """A tiny ``docx`` stand-in: paragraphs as text, saved as JSON."""

    class Document:
        def __init__(self, path=None):
            self.paragraphs: List[str] = []
            if path is not None:
                with open(path, "rb") as f:
                    self.paragraphs = json.loads(f.read().decode())["paragraphs"]

        def add_paragraph(self, text=""):
            self.paragraphs.append(text)

        def save(self, filename):
            with open(filename, "wb") as f:
                f.write(json.dumps({"paragraphs": self.paragraphs}).encode())

    docx = types.ModuleType("docx")
    docx.Document = Document
    shared = types.ModuleType("docx.shared")
    shared.Pt = shared.RGBColor = shared.Inches = object
    enum = types.ModuleType("docx.enum")
    text = types.ModuleType("docx.enum.text")
    text.WD_ALIGN_PARAGRAPH = object
    for name, mod in {"docx": docx, "docx.shared": shared, "docx.enum": enum, "docx.enum.text": text}.items():
        monkeypatch.setitem(sys.modules, name, mod)


class FakeCodeInterpreter:
    instances: List["FakeCodeInterpreter"] = []

    def __init__(self, region):
        self.namespace: Dict[str, Any] = {}
        self.executed: List[str] = []
        self.written: Dict[str, str] = {}
        self.stopped = False
        FakeCodeInterpreter.instances.append(self)

    def start(self, identifier):
        self.identifier = identifier

    def stop(self):
        self.stopped = True

    def invoke(self, op, payload):
        if op == "writeFiles":
            for item in payload["content"]:
                self.written[item["path"]] = item["text"]
                with open(item["path"], "w") as f:
                    f.write(item["text"])
            return {"stream": []}
        if op == "executeCode":
            code = payload["code"]
            self.executed.append(code)
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    exec(compile(code, "<sandbox>", "exec"), self.namespace)
            except Exception:
                return {"stream": [{"result": {"isError": True, "structuredContent": {"stderr": traceback.format_exc()}}}]}
            return {"stream": [{"result": {"structuredContent": {"stdout": out.getvalue()}}}]}
        if op == "readFiles":
            path = payload["paths"][0]
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                return {"stream": []}
            return {"stream": [{"result": {"content": [{"data": data}]}}]}
        raise AssertionError(op)


@pytest.fixture()
def sandbox(monkeypatch, tmp_path, store):
    _install_stub_docx(monkeypatch)
    monkeypatch.chdir(tmp_path)
    FakeCodeInterpreter.instances = []
    import bedrock_agentcore.tools.code_interpreter_client as cic

    monkeypatch.setattr(cic, "CodeInterpreter", FakeCodeInterpreter)
    monkeypatch.setattr(word, "_get_code_interpreter_id", lambda: "ci-test")
    monkeypatch.setattr(word, "_storage_configured", lambda: True)

    saved: Dict[str, bytes] = {}

    async def save(user_id, session_id, filename, file_bytes, mime):
        saved[filename] = file_bytes
        return "upload-1", "0.1 KB"

    async def find(user_id, session_id, name):
        return SimpleNamespace(filename="services-agreement.docx", s3_bucket="b", s3_key="k")

    source = json.dumps({"paragraphs": ["original clause"]}).encode()
    monkeypatch.setattr(word, "_store_document", save)
    monkeypatch.setattr(word, "_find_word_document", find)
    monkeypatch.setattr(word, "_download_s3_bytes", lambda bucket, key: source)
    return saved


def _bound_holder() -> TurnSkillRecords:
    holder = TurnSkillRecords()
    holder.bind([_record()])
    return holder


def _summary(result) -> str:
    return json.loads(result)["summary"]


def _error_text(result) -> str:
    assert result["status"] == "error"
    return result["content"][0]["text"]


class TestSandbox:
    @pytest.mark.asyncio
    async def test_modify_runs_the_script_on_the_loaded_document(self, sandbox):
        tool = make_modify_word_document_tool("s", "u", _bound_holder())
        result = await tool._tool_func(
            document_name="services-agreement",
            skill_name="contract-redliner",
            path="scripts/engine.py",
            params={"clauses": ["indemnity", "term"]},
            output_name="services-agreement-redline",
        )
        doc = json.loads(sandbox["services-agreement-redline.docx"])
        assert doc["paragraphs"] == ["original clause", "redlined indemnity", "redlined term"]

        summary = _summary(result)
        assert "Ran skill script contract-redliner/scripts/engine.py." in summary
        assert "engine log line" in summary and "applied 2 change(s)" in summary

        ci = FakeCodeInterpreter.instances[-1]
        assert ci.stopped
        # The script travels as a file, never inline in the code that runs.
        assert f"{_SANDBOX_SKILL_SCRIPT}.b64" in ci.written
        assert all("redlined " not in code for code in ci.executed)
        entry = ci.executed[-1]
        assert "doc = Document('_source.docx')" in entry
        assert f"spec_from_file_location('_skill_script', {_SANDBOX_SKILL_SCRIPT!r})" in entry
        assert "_skill_mod.run(doc, _skill_json.loads(" in entry
        assert entry.rstrip().endswith("doc.save('services-agreement-redline.docx')")

    @pytest.mark.asyncio
    async def test_create_runs_the_script_on_a_new_document(self, sandbox):
        tool = make_create_word_document_tool("s", "u", _bound_holder())
        result = await tool._tool_func(
            document_name="fresh",
            skill_name="contract-redliner",
            path="engine.py",
        )
        assert json.loads(sandbox["fresh.docx"])["paragraphs"] == []
        assert "Ran skill script contract-redliner/scripts/engine.py." in _summary(result)
        assert "doc = Document()\n" in FakeCodeInterpreter.instances[-1].executed[-1]

    @pytest.mark.asyncio
    async def test_a_script_error_is_surfaced_like_a_code_error(self, sandbox, store):
        store.blobs["skills/contract-redliner/scripts/engine.py"] = b"def run(doc, params):\n    raise ValueError('clause 7 not found')\n"
        tool = make_modify_word_document_tool("s", "u", _bound_holder())
        text = _error_text(
            await tool._tool_func(document_name="x", skill_name="contract-redliner", path="scripts/engine.py")
        )
        assert "Failed to modify 'services-agreement.docx'" in text
        assert "clause 7 not found" in text
        assert "The error came from skill script contract-redliner/scripts/engine.py." in text
        assert not sandbox

    @pytest.mark.asyncio
    async def test_a_script_without_run_is_named(self, sandbox, store):
        store.blobs["skills/contract-redliner/scripts/engine.py"] = b"X = 1\n"
        tool = make_modify_word_document_tool("s", "u", _bound_holder())
        text = _error_text(
            await tool._tool_func(document_name="x", skill_name="contract-redliner", path="scripts/engine.py")
        )
        assert "contract-redliner/scripts/engine.py defines no run(doc, params) function." in text

    @pytest.mark.asyncio
    async def test_an_unbound_holder_sees_no_skills_and_starts_no_sandbox(self, sandbox):
        tool = make_modify_word_document_tool("s", "u", TurnSkillRecords())
        text = _error_text(
            await tool._tool_func(document_name="x", skill_name="contract-redliner", path="scripts/engine.py")
        )
        assert "not available in this conversation" in text
        assert FakeCodeInterpreter.instances == []

    @pytest.mark.asyncio
    async def test_a_reference_is_refused_before_the_sandbox(self, sandbox):
        tool = make_modify_word_document_tool("s", "u", _bound_holder())
        text = _error_text(
            await tool._tool_func(document_name="x", skill_name="contract-redliner", path="references/Engine.txt")
        )
        assert "not a script" in text
        assert FakeCodeInterpreter.instances == []

    @pytest.mark.asyncio
    async def test_python_code_still_works_with_the_flag_on(self, sandbox):
        tool = make_modify_word_document_tool("s", "u", _bound_holder())
        result = await tool._tool_func(document_name="x", python_code="doc.add_paragraph('added')")
        assert json.loads(sandbox["services-agreement.docx"])["paragraphs"] == ["original clause", "added"]
        assert "skill script" not in _summary(result)

    @pytest.mark.parametrize(
        "kwargs,problem",
        [
            ({"python_code": "x", "skill_name": "contract-redliner", "path": "engine.py"}, "not both"),
            ({}, "Pass `python_code`"),
            ({"skill_name": "contract-redliner"}, "needs both"),
            ({"python_code": "x", "params": {"a": 1}}, "only used with a skill script"),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_arguments_name_one_source_of_code(self, sandbox, kwargs, problem):
        tool = make_modify_word_document_tool("s", "u", _bound_holder())
        assert problem in _error_text(await tool._tool_func(document_name="x", **kwargs))
        assert FakeCodeInterpreter.instances == []


# ---------------------------------------------------------------------------
# Tool specs: constant per deployment, unchanged while off
# ---------------------------------------------------------------------------


def _digest(tool) -> str:
    return hashlib.sha256(json.dumps(tool.tool_spec, sort_keys=True).encode()).hexdigest()


# Taken from the code before this feature existed: off, nothing changes.
OFF_DIGESTS = {
    "create_word_document": "d5638bea3d9f4f1f2ddb65e84fe1f40cd0c1e0512467037110eacf07e0a8fa24",
    "modify_word_document": "79509eb731a6b250cee820e346cfff8f0299c790af4ea942e73afea73c59317d",
}
# On: one toolConfig rewrite per deployment when the flag turns on. A change
# here is a deliberate spec change; update the pin with it.
ON_DIGESTS = {
    "create_word_document": "cdb34e502561c5df63603325bfb2cac2b960c05cd54097805e51285e4b9c4aa4",
    "modify_word_document": "73bb925967ed30f133c388d5f713a94092bc0b83feb3833f40e658310890c35d",
}


def _word_family(monkeypatch, flag: str) -> Dict[str, Any]:
    from apis.inference_api.chat.routes import _build_word_document_tools

    monkeypatch.setenv("WORD_SKILL_SCRIPTS_ENABLED", flag)
    tools = _build_word_document_tools(["create_word_document"], "s", "u", "User")
    return {t.tool_spec["name"]: t for t in tools}


def test_flag_off_specs_are_unchanged(monkeypatch):
    family = _word_family(monkeypatch, "false")
    for name, digest in OFF_DIGESTS.items():
        assert _digest(family[name]) == digest, name
        assert "skill_name" not in family[name].tool_spec["inputSchema"]["json"]["properties"]
        assert getattr(family[name], "_turn_skill_records", None) is None


def test_flag_on_specs_are_pinned_and_share_one_holder(monkeypatch):
    family = _word_family(monkeypatch, "true")
    for name, digest in ON_DIGESTS.items():
        spec = family[name].tool_spec
        assert _digest(family[name]) == digest, name
        props = spec["inputSchema"]["json"]
        assert {"skill_name", "path", "params"} <= set(props["properties"])
        assert "python_code" not in props["required"]
    holders = {id(family[n]._turn_skill_records) for n in ON_DIGESTS}
    assert len(holders) == 1


def test_flag_on_specs_do_not_depend_on_identity_or_records(monkeypatch):
    a = make_modify_word_document_tool("s1", "u1", TurnSkillRecords())
    holder = TurnSkillRecords()
    holder.bind([_record()])
    b = make_modify_word_document_tool("s2", "u2", holder)
    assert _digest(a) == _digest(b)


def test_the_agent_build_binds_the_turns_records(monkeypatch):
    """``ChatAgent._create_agent`` fills the holder from the records its own
    skills runtime fetched, the same records ``read_skill_file`` reads."""
    import agents.main_agent.chat_agent as chat_agent
    from agents.main_agent.skills.strands_mapping import SkillsRuntime

    holder = TurnSkillRecords()
    word_tool = make_modify_word_document_tool("s", "u", holder)
    records = [_record()]

    class Stub(chat_agent.ChatAgent):
        def __init__(self) -> None:
            self._accessible_skill_ids = ["contract-redliner"]
            self.session_id = None
            self.user_id = None
            self.model_config = None
            self.session_manager = None
            self.system_prompt = "prompt"

        def _build_filtered_tools(self):
            return [word_tool]

        def _create_hooks(self):
            return []

    built: Dict[str, Any] = {}
    monkeypatch.setattr(chat_agent, "build_skills_runtime", lambda ids: SkillsRuntime(None, None, records))
    monkeypatch.setattr(chat_agent.AgentFactory, "create_agent", staticmethod(lambda **kw: built.update(kw)))
    Stub()._create_agent()

    assert holder.records == records
    assert word_tool in built["tools"]


@pytest.mark.parametrize("flag,expected", [("false", "NOT executable"), ("true", "skill_name and path")])
def test_read_skill_file_notice_follows_the_flag(monkeypatch, store, flag, expected):
    """Off, scripts stay inert and the notice says so; on, it points at the Word
    tools instead of telling the model to retype the program."""
    import agents.main_agent.skills.strands_mapping as mapping

    monkeypatch.setenv("WORD_SKILL_SCRIPTS_ENABLED", flag)
    monkeypatch.setattr(mapping, "get_skill_resource_store", lambda: store)
    read = mapping.make_read_skill_file_tool([_record()])
    text = read._tool_func(skill_name="contract-redliner", path="scripts/engine.py")
    assert expected in text.split("\n", 1)[0]
    assert text.endswith(ENGINE.decode())
