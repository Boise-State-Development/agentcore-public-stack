"""Record every AWS API call and HTTP request a turn makes, and answer it from memory.

Used by ``test_turn_path_io_inventory.py``. Three pieces:

* **The boundary.** A botocore ``before-call`` handler is registered on every client
  the moment it makes its first call (``BaseClient._make_api_call`` is wrapped only
  to do that registration), so clients built on any ``boto3.Session`` — the cached
  ones in ``apis.shared.aws_clients``, the fresh sessions the Memory SDK and Strands
  build — all go through it. The handler logs the call and answers from
  :class:`FakeAws`, so no request is serialized onto a socket. ``after-call`` still
  fires, so the DynamoDB resource layer deserializes as it would in production.
  ``httpx`` transports are patched to log and refuse every request.

* **The lane.** Every task spawned while recording carries a *lineage*: the ids of
  the tasks it descends from, held in a context variable, so ``asyncio.to_thread``
  and executor work inherit it. A call is **awaited** when it ran on the lineage of
  the model call itself, before the next link of that lineage was spawned — the
  turn could not reach the model until it finished. Anything else is
  **concurrent**: it ran in a task beside the path (the title, a search started
  ahead of the build), which costs first-token time only when it outlasts what it
  overlaps. The stream coordinator pumps the agent stream with one task per step,
  all on one shared ``Context``; the lineage lives in that context, so the steps
  chain rather than look like siblings.

* **The hook.** Each Strands hook callback runs with the event name and the
  callback's qualname in a context variable, so a call made from inside a
  ``BeforeModelCallEvent`` hook says so.

Nothing here measures time.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import copy
import dis
import functools
import io
import itertools
import json
import re
import sys
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import botocore.client
from botocore.awsrequest import AWSResponse
from botocore.response import StreamingBody

SRC_DIR = Path(__file__).resolve().parents[4] / "src"
_SRC_PREFIX = str(SRC_DIR) + "/"

_LINEAGE: contextvars.ContextVar[Optional[Tuple[int, ...]]] = contextvars.ContextVar("turn_io_lineage", default=None)
_HOOK: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("turn_io_hook", default=None)
_tls = threading.local()

_HANDLER_ID = "turn-io-recorder"


@dataclass
class Call:
    seq: int
    kind: str  # "aws" or "http"
    service: str
    operation: str
    target: str
    origin: str
    lineage: Tuple[int, ...]
    hook: Optional[str]
    model_call_index: int  # how many ConverseStream calls had been issued before this one

    def key(self) -> str:
        parts = [f"{self.service}.{self.operation}"]
        if self.target:
            parts.append(self.target)
        return " ".join(parts)


@dataclass
class _Spawn:
    """A link in a lineage: a task, or work handed to a thread pool."""

    seq: int
    name: str
    parent: Tuple[int, ...]
    thread: bool = False
    join_seq: Optional[int] = None


# Helpers that only forward a call to a thread; the origin is whoever called them.
_PASS_THROUGH = ("apis/shared/blocking_io.py",)


def _origin_of_frame(frame) -> Optional[str]:
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename.startswith(_SRC_PREFIX):
            module = filename[len(_SRC_PREFIX) :]
            if module not in _PASS_THROUGH:
                return f"{module}:{frame.f_code.co_qualname}"
        frame = frame.f_back
    return None


def _origin_from_stack() -> Optional[str]:
    return _origin_of_frame(sys._getframe(2))


def _submission(frame) -> Tuple[bool, bool, Any]:
    """``(via_to_thread, via_run_in_executor, caller)`` for a ``ThreadPoolExecutor.submit``.

    ``caller`` is the first frame outside asyncio, concurrent.futures and this module.
    """
    via_to_thread = via_executor = False
    while frame is not None:
        filename = frame.f_code.co_filename
        name = frame.f_code.co_name
        if filename.endswith("asyncio/threads.py") and name == "to_thread":
            via_to_thread = True
        elif filename.endswith("asyncio/base_events.py") and name == "run_in_executor":
            via_executor = True
        elif not ("/asyncio/" in filename or "/concurrent/futures/" in filename or filename == __file__):
            return via_to_thread, via_executor, frame
        frame = frame.f_back
    return via_to_thread, via_executor, None


def _awaited_at_call_site(frame) -> bool:
    """Whether the instruction after the frame's current call is an ``await``."""
    if frame is None:
        return False
    seen = False
    for instruction in dis.get_instructions(frame.f_code):
        if seen and instruction.opname not in ("CACHE", "EXTENDED_ARG"):
            return instruction.opname == "GET_AWAITABLE"
        if instruction.offset == frame.f_lasti:
            seen = True
    return False


class Recorder:
    """Installs the patches; holds the calls."""

    def __init__(self, fake: "FakeAws", prefix: str) -> None:
        self.fake = fake
        self.prefix = prefix
        self.calls: List[Call] = []
        self.armed = False
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.spawns: Dict[int, _Spawn] = {}
        self.model_calls: List[Call] = []
        self._patches: List[Tuple[Any, str, Any]] = []
        self.tasks: List[asyncio.Task] = []
        self.thread_futures: List[concurrent.futures.Future] = []

    # ── installation ────────────────────────────────────────────────────

    def install(self) -> None:
        recorder = self
        original_make_api_call = botocore.client.BaseClient._make_api_call

        def _make_api_call(client, operation_name, api_params):
            events = client.meta.events
            if not getattr(events, "_turn_io_registered", False):
                events.register_last("before-parameter-build", _stash_params, unique_id=_HANDLER_ID + "-params")
                events.register_first("before-call", recorder._before_call, unique_id=_HANDLER_ID)
                events._turn_io_registered = True
            return original_make_api_call(client, operation_name, api_params)

        self._patch(botocore.client.BaseClient, "_make_api_call", _make_api_call)

        original_submit = ThreadPoolExecutor.submit

        def submit(executor, fn, /, *args, **kwargs):
            via_to_thread, via_executor, caller = _submission(sys._getframe(1))
            hint = _origin_of_frame(caller)
            ctx = contextvars.copy_context()
            link = recorder._thread_link(ctx, fn, hint, via_to_thread, via_executor, caller)

            def run():
                _tls.hint = hint
                try:
                    return ctx.run(fn, *args, **kwargs)
                finally:
                    _tls.hint = None

            future = original_submit(executor, run)
            if link is not None:
                future._turn_io_link = link
                recorder.thread_futures.append(future)
            return future

        self._patch(ThreadPoolExecutor, "submit", submit)

        original_result = concurrent.futures.Future.result

        def result(future, timeout=None):
            link = getattr(future, "_turn_io_link", None)
            if link is not None and recorder.armed:
                spawn = recorder.spawns.get(link)
                if spawn is not None and spawn.join_seq is None:
                    with recorder._lock:
                        spawn.join_seq = next(recorder._seq)
            return original_result(future, timeout)

        self._patch(concurrent.futures.Future, "result", result)

        import httpx

        def handle_request(transport, request):
            recorder._record_http(request)
            raise httpx.ConnectError("turn-io recorder: no network", request=request)

        async def handle_async_request(transport, request):
            recorder._record_http(request)
            raise httpx.ConnectError("turn-io recorder: no network", request=request)

        self._patch(httpx.HTTPTransport, "handle_request", handle_request)
        self._patch(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)

        from strands.hooks.registry import HookRegistry

        original_get_callbacks_for = HookRegistry.get_callbacks_for

        def get_callbacks_for(registry, event):
            event_name = type(event).__name__
            for callback in original_get_callbacks_for(registry, event):
                yield _tag_callback(callback, event_name)

        self._patch(HookRegistry, "get_callbacks_for", get_callbacks_for)

    def uninstall(self) -> None:
        for owner, name, original in reversed(self._patches):
            setattr(owner, name, original)
        self._patches.clear()

    def _patch(self, owner: Any, name: str, replacement: Any) -> None:
        self._patches.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def _thread_link(self, ctx, fn, hint, via_to_thread, via_executor, caller) -> Optional[int]:
        """A lineage link for work handed to a pool, or None when it runs as part of its caller.

        ``asyncio.to_thread`` and an ``await``ed ``run_in_executor`` block the caller at
        once, so the work is the caller's own. Anything else is a link of its own; it is
        joined back onto the caller only if the caller waits on its ``Future`` before
        doing anything else (see ``_inline``).
        """
        parent = ctx.run(_LINEAGE.get)
        if parent is None or not self.armed or via_to_thread:
            return None
        if via_executor and _awaited_at_call_site(caller):
            return None
        link = next(self._ids)
        name = "thread " + (hint or _function_name(fn) or "?")
        with self._lock:
            self.spawns[link] = _Spawn(seq=next(self._seq), name=name, parent=parent, thread=True)
        ctx.run(_LINEAGE.set, parent + (link,))
        return link

    def task_factory(self, loop, coro, **kwargs):
        ctx = kwargs.pop("context", None) or contextvars.copy_context()
        parent = ctx.run(_LINEAGE.get)
        if parent is not None and self.armed:
            task_id = next(self._ids)
            with self._lock:
                self.spawns[task_id] = _Spawn(seq=next(self._seq), name=_coro_name(coro), parent=parent)
            ctx.run(_LINEAGE.set, parent + (task_id,))
        task = asyncio.Task(coro, loop=loop, context=ctx, **kwargs)
        if parent is not None and self.armed:
            self.tasks.append(task)
        return task

    def start_scenario(self) -> None:
        """Begin a turn: a fresh root lineage on the calling task."""
        self.calls.clear()
        self.model_calls.clear()
        self.spawns.clear()
        self.tasks.clear()
        self.thread_futures.clear()
        self.armed = True
        root = next(self._ids)
        _LINEAGE.set((root,))

    async def settle(self) -> None:
        """Let every task the turn spawned finish, so the concurrent lane is complete."""
        pending = [t for t in self.tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)
        threads = [f for f in self.thread_futures if not f.done()]
        if threads:
            await asyncio.to_thread(concurrent.futures.wait, threads, 10)
        self.armed = False

    # ── recording ───────────────────────────────────────────────────────

    def _lineage(self) -> Tuple[int, ...]:
        lineage = _LINEAGE.get()
        return lineage if lineage is not None else ()

    def _record(self, kind: str, service: str, operation: str, target: str) -> Optional[Call]:
        if not self.armed:
            return None
        origin = _origin_from_stack() or getattr(_tls, "hint", None) or "?"
        with self._lock:
            call = Call(
                seq=next(self._seq),
                kind=kind,
                service=service,
                operation=operation,
                target=target,
                origin=origin,
                lineage=self._lineage(),
                hook=_HOOK.get(),
                model_call_index=len(self.model_calls),
            )
            self.calls.append(call)
        return call

    def _record_http(self, request) -> None:
        self._record("http", "http", request.method, request.url.host or "")

    def _before_call(self, model, params, context, **kwargs):
        service = model.service_model.service_name
        operation = model.name
        api_params = context.get("_turn_io_params") or {}
        target = self._target(service, api_params)
        call = self._record("aws", service, operation, target)
        if call is not None and service == "bedrock-runtime" and operation == "ConverseStream":
            self.model_calls.append(call)
        status, parsed = self.fake.handle(service, operation, api_params)
        parsed.setdefault("ResponseMetadata", {"HTTPStatusCode": status, "RequestId": "turn-io"})
        return AWSResponse(None, status, {}, None), parsed

    def _target(self, service: str, params: dict) -> str:
        if service == "dynamodb":
            if "TableName" in params:
                name = self._strip(params["TableName"])
                index = params.get("IndexName")
                return f"{name}/{index}" if index else name
            for key in ("RequestItems", "TransactItems"):
                if key in params:
                    items = params[key]
                    if isinstance(items, dict):
                        return ",".join(sorted(self._strip(t) for t in items))
                    tables = set()
                    for item in items:
                        for body in item.values():
                            if isinstance(body, dict) and "TableName" in body:
                                tables.add(self._strip(body["TableName"]))
                    return ",".join(sorted(tables))
            return ""
        if service == "s3":
            return self._strip(params.get("Bucket", ""))
        if service == "ssm":
            return params.get("Name", "")
        return ""

    def _strip(self, name: str) -> str:
        return name[len(self.prefix) + 1 :] if name.startswith(self.prefix + "-") else name

    # ── reading ─────────────────────────────────────────────────────────

    def inventory_before_model_call(self, index: int = 0) -> Dict[str, List[str]]:
        """What the turn did before model call ``index`` (0 = the first byte), by lane.

        Before the first call that is the whole turn so far. Before a later one it is
        the window since the previous model call: the tool round.
        """
        assert len(self.model_calls) > index, f"the turn made {len(self.model_calls)} model call(s); expected more than {index}"
        model = self.model_calls[index]
        start = self.model_calls[index - 1].seq if index else 0
        awaited: Counter = Counter()
        concurrent: Counter = Counter()
        for call in self.calls:
            if call is model or call.seq <= start:
                continue
            lane = self._lane(call, model, start)
            if lane is None:
                continue
            line = f"{call.key()}  <- {call.origin}"
            if call.hook:
                line += f"  [hook {call.hook}]"
            if lane == "awaited":
                awaited[line] += 1
            else:
                concurrent[f"{line}  [task {lane}]"] += 1
        return {"awaited": _counted(awaited), "concurrent": _counted(concurrent)}

    def awaited_before_hook_calls(self, after_index: int = 0) -> List[str]:
        """Awaited calls made from inside a ``Before*`` hook after model call ``after_index``.

        Those hooks fire before every model call and every tool call, so their cost
        repeats on each round of a tool-using turn.
        """
        found = []
        for index in range(after_index + 1, len(self.model_calls)):
            model = self.model_calls[index]
            start = self.model_calls[index - 1].seq
            for call in self.calls:
                if not (start < call.seq < model.seq):
                    continue
                if call.hook and call.hook.startswith("Before") and self._lane(call, model, start) == "awaited":
                    found.append(f"before model call {index + 1}: {call.key()}  <- {call.origin}  [hook {call.hook}]")
        return found

    def _lane(self, call: Call, model: Call, start: int) -> Optional[str]:
        """``"awaited"``, the name of the concurrent task the call ran in, or None if out of window.

        Awaited: the call ran on the model call's own lineage, before the next link of
        that lineage was spawned, so the model call could not start until it finished.
        Work handed to a thread counts as its submitter's when the submitter blocked on
        it (``_inline``). Concurrent: it ran in a task or thread that branched off the
        path inside the window. One branched before the window is out of it.
        """
        lineage, seq, path = call.lineage, call.seq, model.lineage
        while True:
            depth = next(
                (d for d, link in enumerate(lineage) if d >= len(path) or path[d] != link),
                None,
            )
            if depth is None:
                break
            spawn = self.spawns.get(lineage[depth])
            if spawn is None or not (start < spawn.seq < model.seq):
                return None
            if spawn.thread and self._inline(lineage[depth], model):
                lineage, seq = spawn.parent, spawn.seq
                continue
            return spawn.name
        if not lineage or seq > model.seq:
            return None
        if len(lineage) == len(path):
            return "awaited"
        next_link = self.spawns.get(path[len(lineage)])
        if next_link is None or seq < next_link.seq:
            return "awaited"
        return "path, after spawning its next step"

    def _inline(self, link: int, model: Call) -> bool:
        """Whether the submitter of thread work ``link`` waited on it before doing anything else.

        Decided from the submitter's own events, which run in program order, so the
        answer does not depend on when the thread got scheduled.
        """
        spawn = self.spawns[link]
        if spawn.join_seq is None or spawn.join_seq > model.seq:
            return False
        between = range(spawn.seq + 1, spawn.join_seq)
        if any(c.lineage == spawn.parent and c.seq in between for c in self.calls):
            return False
        return not any(other.parent == spawn.parent and other.seq in between for other in self.spawns.values())


def _counted(counter: Counter) -> List[str]:
    return [f"{n}x {line}" for line, n in sorted(counter.items())]


def _function_name(fn: Any) -> Optional[str]:
    target = fn
    while isinstance(target, functools.partial):
        target = target.func
    code = getattr(target, "__code__", None)
    if code is not None:
        return code.co_qualname
    return getattr(target, "__qualname__", None)


def _coro_name(coro: Any) -> str:
    code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None)
    if code is not None:
        return code.co_qualname
    return type(coro).__name__


def _stash_params(params, context, **kwargs):
    context["_turn_io_params"] = params


def _tag_callback(callback: Callable, event_name: str) -> Callable:
    owner = getattr(callback, "__self__", None)
    name = getattr(callback, "__qualname__", None) or getattr(callback, "__name__", repr(callback))
    if owner is not None and "." not in name:
        name = f"{type(owner).__name__}.{name}"
    label = f"{event_name}:{name}"

    if asyncio.iscoroutinefunction(callback):

        @functools.wraps(callback)
        async def tagged_async(event):
            token = _HOOK.set(label)
            try:
                return await callback(event)
            finally:
                _HOOK.reset(token)

        return tagged_async

    @functools.wraps(callback)
    def tagged(event):
        token = _HOOK.set(label)
        try:
            return callback(event)
        finally:
            _HOOK.reset(token)

    return tagged


# ── the fake ────────────────────────────────────────────────────────────


def _scalar(value: dict) -> Any:
    if "S" in value:
        return value["S"]
    if "N" in value:
        return Decimal(value["N"])
    if "BOOL" in value:
        return value["BOOL"]
    if "B" in value:
        return value["B"]
    return json.dumps(value, sort_keys=True, default=str)


_CLAUSE_PATTERNS = [
    ("between", re.compile(r"^(\S+)\s+BETWEEN\s+(:\S+)\s+AND\s+(:\S+)$", re.I)),
    ("begins_with", re.compile(r"^BEGINS_WITH\s+(\S+)\s+(:\S+)$")),
    ("cmp", re.compile(r"^(\S+)\s*(=|<=|>=|<|>)\s*(:\S+)$")),
]


def _key_clauses(expression: str) -> List[Tuple[str, Tuple[str, ...]]]:
    """A key condition as ``(kind, operands)`` clauses; raises on anything it can't read."""
    text = re.sub(r"begins_with\s*\(\s*([^,\s]+)\s*,\s*([^)\s]+)\s*\)", r"BEGINS_WITH \1 \2", expression, flags=re.I)
    text = text.replace("(", " ").replace(")", " ").strip()
    text = re.sub(r"BETWEEN\s+(:\S+)\s+AND\s+(:\S+)", r"BETWEEN \1 __AND__ \2", text, flags=re.I)
    clauses = []
    for raw in re.split(r"\s+AND\s+", text, flags=re.I):
        raw = raw.replace("__AND__", "AND").strip()
        for kind, pattern in _CLAUSE_PATTERNS:
            match = pattern.match(raw)
            if match:
                clauses.append((kind, match.groups()))
                break
        else:
            raise ValueError(f"turn-io fake cannot read key condition clause {raw!r} in {expression!r}")
    return clauses


class FakeDynamo:
    """Rows held in wire format. Every answer is a deep copy: the resource layer
    deserializes responses in place, and would otherwise rewrite the stored row."""

    def __init__(self) -> None:
        self.tables: Dict[str, List[dict]] = {}
        self.key_names: Dict[str, Tuple[str, ...]] = {}

    def _key_of(self, table: str, item: dict) -> Tuple[str, ...]:
        names = self.key_names.get(table)
        if names is None:
            if "PK" in item:
                names = ("PK", "SK") if "SK" in item else ("PK",)
            else:
                names = (next(iter(item)),)
        return names

    def _find(self, table: str, key: dict) -> Optional[dict]:
        for item in self.tables.get(table, []):
            if all(item.get(k) == v for k, v in key.items()):
                return item
        return None

    def _put(self, table: str, item: dict) -> None:
        names = self._key_of(table, item)
        rows = self.tables.setdefault(table, [])
        key = {k: item.get(k) for k in names}
        for i, existing in enumerate(rows):
            if all(existing.get(k) == v for k, v in key.items()):
                rows[i] = item
                return
        rows.append(item)

    def handle(self, operation: str, p: dict) -> Tuple[int, dict]:
        status, body = self._handle(operation, copy.deepcopy(p))
        return status, copy.deepcopy(body)

    def _handle(self, operation: str, p: dict) -> Tuple[int, dict]:
        table = p.get("TableName", "")
        if operation == "GetItem":
            self.key_names.setdefault(table, tuple(p["Key"]))
            item = self._find(table, p["Key"])
            return 200, ({"Item": item} if item else {})
        if operation == "PutItem":
            item = p["Item"]
            condition = p.get("ConditionExpression", "")
            if "attribute_not_exists" in condition:
                names = self._key_of(table, item)
                if self._find(table, {k: item[k] for k in names if k in item}):
                    return _conditional_failure()
            self._put(table, item)
            return 200, {}
        if operation == "UpdateItem":
            self.key_names.setdefault(table, tuple(p["Key"]))
            item = self._find(table, p["Key"])
            old = dict(item) if item else None
            condition = p.get("ConditionExpression", "")
            if condition.strip().startswith("attribute_exists") and item is None:
                return _conditional_failure()
            if item is None:
                item = dict(p["Key"])
                self.tables.setdefault(table, []).append(item)
            _apply_update(
                item,
                p.get("UpdateExpression", ""),
                p.get("ExpressionAttributeNames", {}),
                p.get("ExpressionAttributeValues", {}),
            )
            returns = p.get("ReturnValues")
            if returns == "ALL_NEW":
                return 200, {"Attributes": item}
            if returns == "ALL_OLD" and old:
                return 200, {"Attributes": old}
            if returns in ("UPDATED_NEW", "UPDATED_OLD"):
                return 200, {"Attributes": item}
            return 200, {}
        if operation == "DeleteItem":
            item = self._find(table, p["Key"])
            if item:
                self.tables[table].remove(item)
            if p.get("ReturnValues") == "ALL_OLD" and item:
                return 200, {"Attributes": item}
            return 200, {}
        if operation in ("Query", "Scan"):
            items = list(self.tables.get(table, []))
            sort_name = None
            if operation == "Query":
                items, sort_name = self._query(items, p)
                if sort_name:
                    items.sort(key=lambda i: _scalar(i.get(sort_name, {"S": ""})))
                if p.get("ScanIndexForward") is False:
                    items.reverse()
            limit = p.get("Limit")
            if limit:
                items = items[: int(limit)]
            if p.get("Select") == "COUNT":
                return 200, {"Count": len(items), "ScannedCount": len(items)}
            return 200, {"Items": items, "Count": len(items), "ScannedCount": len(items)}
        if operation == "BatchGetItem":
            responses = {}
            for name, request in p.get("RequestItems", {}).items():
                found = [self._find(name, k) for k in request.get("Keys", [])]
                responses[name] = [i for i in found if i]
            return 200, {"Responses": responses, "UnprocessedKeys": {}}
        if operation == "BatchWriteItem":
            for name, requests in p.get("RequestItems", {}).items():
                for request in requests:
                    if "PutRequest" in request:
                        self._put(name, request["PutRequest"]["Item"])
            return 200, {"UnprocessedItems": {}}
        if operation == "TransactGetItems":
            out = []
            for entry in p.get("TransactItems", []):
                get = entry.get("Get", {})
                item = self._find(get.get("TableName", ""), get.get("Key", {}))
                out.append({"Item": item} if item else {})
            return 200, {"Responses": out}
        if operation == "TransactWriteItems":
            for entry in p.get("TransactItems", []):
                for kind, op in (("Put", "PutItem"), ("Update", "UpdateItem"), ("Delete", "DeleteItem")):
                    if kind in entry:
                        self._handle(op, dict(entry[kind], ConditionExpression=""))
            return 200, {}
        if operation == "DescribeTable":
            return 200, {"Table": {"TableName": table, "TableStatus": "ACTIVE"}}
        return 200, {}

    def _query(self, items: List[dict], p: dict) -> Tuple[List[dict], Optional[str]]:
        names = p.get("ExpressionAttributeNames", {})
        values = p.get("ExpressionAttributeValues", {})
        expression = p.get("KeyConditionExpression")
        sort_name = None
        if not expression and "KeyConditions" in p:
            for attr, cond in p["KeyConditions"].items():
                wanted = cond["AttributeValueList"][0]
                items = [i for i in items if i.get(attr) == wanted]
            return items, None
        clauses = _key_clauses(expression or "")
        for index, (kind, groups) in enumerate(clauses):
            attr = names.get(groups[0], groups[0])
            if index > 0:
                sort_name = attr
            if kind == "cmp":
                op, wanted = groups[1], _scalar(values[groups[2]])
                items = [i for i in items if attr in i and _compare(_scalar(i[attr]), op, wanted)]
            elif kind == "begins_with":
                prefix = _scalar(values[groups[1]])
                items = [i for i in items if attr in i and str(_scalar(i[attr])).startswith(str(prefix))]
            elif kind == "between":
                low, high = _scalar(values[groups[1]]), _scalar(values[groups[2]])
                items = [i for i in items if attr in i and low <= _scalar(i[attr]) <= high]
        return items, sort_name


def _split_top(text: str, sep: str = ",") -> List[str]:
    parts, depth, current = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if "".join(current).strip():
        parts.append("".join(current).strip())
    return parts


def _resolve_path(path: str, names: dict) -> List[str]:
    path = re.sub(r"\[\d+\]", "", path.strip())
    return [names.get(part, part) for part in path.split(".")]


def _get_path(item: dict, parts: List[str]) -> Optional[dict]:
    value: Any = {"M": item}
    for part in parts:
        if not isinstance(value, dict) or "M" not in value or part not in value["M"]:
            return None
        value = value["M"][part]
    return value


def _set_path(item: dict, parts: List[str], value: dict) -> None:
    target = item
    for part in parts[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict) or "M" not in nested:
            nested = {"M": {}}
            target[part] = nested
        target = nested["M"]
    target[parts[-1]] = value


def _remove_path(item: dict, parts: List[str]) -> None:
    target = item
    for part in parts[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict) or "M" not in nested:
            return
        target = nested["M"]
    target.pop(parts[-1], None)


def _operand(item: dict, text: str, names: dict, values: dict) -> Optional[dict]:
    text = text.strip()
    match = re.match(r"^if_not_exists\s*\((.*)\)$", text)
    if match:
        path, default = _split_top(match.group(1))
        existing = _get_path(item, _resolve_path(path, names))
        return existing if existing is not None else _operand(item, default, names, values)
    match = re.match(r"^list_append\s*\((.*)\)$", text)
    if match:
        left, right = (_operand(item, x, names, values) for x in _split_top(match.group(1)))
        return {"L": (left or {"L": []}).get("L", []) + (right or {"L": []}).get("L", [])}
    for op in ("+", "-"):
        pieces = _split_top(text, op)
        if len(pieces) == 2:
            left = _operand(item, pieces[0], names, values) or {"N": "0"}
            right = _operand(item, pieces[1], names, values) or {"N": "0"}
            a, b = Decimal(left.get("N", "0")), Decimal(right.get("N", "0"))
            return {"N": str(a + b if op == "+" else a - b)}
    if text.startswith(":"):
        return values.get(text)
    return _get_path(item, _resolve_path(text, names))


def _apply_update(item: dict, expression: str, names: dict, values: dict) -> None:
    sections = re.split(r"\b(SET|ADD|REMOVE|DELETE)\b", expression, flags=re.I)
    action = None
    for chunk in sections:
        upper = chunk.strip().upper()
        if upper in ("SET", "ADD", "REMOVE", "DELETE"):
            action = upper
            continue
        if not chunk.strip() or action is None:
            continue
        for clause in _split_top(chunk):
            if action == "SET" and "=" in clause:
                path, value = clause.split("=", 1)
                resolved = _operand(item, value, names, values)
                if resolved is not None:
                    _set_path(item, _resolve_path(path, names), resolved)
            elif action == "REMOVE":
                _remove_path(item, _resolve_path(clause, names))
            elif action == "ADD":
                path, value = clause.split(None, 1)
                parts = _resolve_path(path, names)
                existing = _get_path(item, parts)
                increment = values.get(value.strip(), {"N": "0"})
                if "N" in increment:
                    base = Decimal((existing or {"N": "0"}).get("N", "0"))
                    _set_path(item, parts, {"N": str(base + Decimal(increment["N"]))})
                elif "SS" in increment:
                    merged = sorted(set((existing or {}).get("SS", [])) | set(increment["SS"]))
                    _set_path(item, parts, {"SS": merged})


def _compare(left: Any, op: str, right: Any) -> bool:
    try:
        if op == "=":
            return left == right
        if op == "<":
            return left < right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        if op == ">=":
            return left >= right
    except TypeError:
        return False
    return False


def _conditional_failure() -> Tuple[int, dict]:
    return 400, {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}}


def _error(code: str, status: int = 400, message: str = "") -> Tuple[int, dict]:
    return status, {"Error": {"Code": code, "Message": message or code}}


@dataclass
class ScriptedModel:
    """What ConverseStream answers, one response per call, last one repeating."""

    responses: List[List[dict]] = field(default_factory=list)
    _calls: int = 0

    def next(self) -> List[dict]:
        if not self.responses:
            return text_response("Hello.")
        response = self.responses[min(self._calls, len(self.responses) - 1)]
        self._calls += 1
        return response


def _usage() -> dict:
    return {
        "metadata": {
            "usage": {"inputTokens": 1200, "outputTokens": 8, "totalTokens": 1208},
            "metrics": {"latencyMs": 1},
        }
    }


def text_response(text: str) -> List[dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        _usage(),
    ]


def tool_use_response(tool_use_id: str, name: str, tool_input: dict) -> List[dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}},
                "contentBlockIndex": 0,
            }
        },
        {
            "contentBlockDelta": {
                "delta": {"toolUse": {"input": json.dumps(tool_input)}},
                "contentBlockIndex": 0,
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        _usage(),
    ]


class FakeAws:
    """In-memory answers for every service the turn path reaches."""

    def __init__(self) -> None:
        self.dynamo = FakeDynamo()
        self.s3: Dict[Tuple[str, str], bytes] = {}
        self.ssm: Dict[str, str] = {}
        self.memory_events: Dict[Tuple[str, str, str], List[dict]] = {}
        self.vectors: List[dict] = []
        self.model = ScriptedModel()
        self.unhandled: Counter = Counter()
        self.errors: List[str] = []

    def handle(self, service: str, operation: str, p: dict) -> Tuple[int, dict]:
        """Answer one call. A failure here is the fake's, not the code's: it is kept in
        ``errors`` for the driver to report, because the code under test may swallow
        the exception and quietly take another branch."""
        try:
            return self._answer(service, operation, p)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            self.errors.append(f"{service}.{operation}: {type(exc).__name__}: {exc}")
            return _error("InternalServerError", 500, "turn-io fake failed")

    def _answer(self, service: str, operation: str, p: dict) -> Tuple[int, dict]:
        if service == "dynamodb":
            return self.dynamo.handle(operation, p)
        if service == "s3":
            return self._s3(operation, p)
        if service == "ssm":
            if operation == "GetParameter":
                if p["Name"] in self.ssm:
                    return 200, {"Parameter": {"Name": p["Name"], "Value": self.ssm[p["Name"]]}}
                return _error("ParameterNotFound")
            return 200, {}
        if service == "bedrock-agentcore":
            return self._memory(operation, p)
        if service == "bedrock-agentcore-control":
            if operation == "GetMemory":
                return 200, {"memory": _memory_resource(p.get("memoryId", "mem"))}
            return 200, {}
        if service == "bedrock-runtime":
            return self._runtime(operation, p)
        if service == "s3vectors":
            if operation == "QueryVectors":
                return 200, {"vectors": list(self.vectors), "distanceMetric": "cosine"}
            return 200, {}
        if service == "sts":
            return 200, {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/t", "UserId": "t"}
        self.unhandled[f"{service}.{operation}"] += 1
        return 200, {}

    def _s3(self, operation: str, p: dict) -> Tuple[int, dict]:
        key = (p.get("Bucket", ""), p.get("Key", ""))
        if operation == "GetObject":
            if key not in self.s3:
                return _error("NoSuchKey", 404)
            data = self.s3[key]
            return 200, {
                "Body": StreamingBody(io.BytesIO(data), len(data)),
                "ContentLength": len(data),
                "ContentType": "application/octet-stream",
            }
        if operation == "HeadObject":
            if key not in self.s3:
                return _error("404", 404)
            return 200, {"ContentLength": len(self.s3[key])}
        if operation == "PutObject":
            body = p.get("Body", b"")
            if hasattr(body, "read"):
                body = body.read()
            self.s3[key] = body if isinstance(body, bytes) else str(body).encode()
            return 200, {"ETag": '"etag"'}
        if operation == "ListObjectsV2":
            prefix = p.get("Prefix", "")
            contents = [
                {"Key": k, "Size": len(v), "LastModified": datetime(2026, 11, 1, tzinfo=timezone.utc)}
                for (bucket, k), v in sorted(self.s3.items())
                if bucket == p.get("Bucket") and k.startswith(prefix)
            ]
            return 200, {"Contents": contents, "KeyCount": len(contents), "IsTruncated": False}
        return 200, {}

    def _memory(self, operation: str, p: dict) -> Tuple[int, dict]:
        key = (p.get("memoryId", ""), p.get("actorId", ""), p.get("sessionId", ""))
        if operation == "ListEvents":
            events = list(self.memory_events.get(key, []))
            if not p.get("includePayloads", True):
                events = [{k: v for k, v in e.items() if k != "payload"} for e in events]
            return 200, {"events": events}
        if operation == "CreateEvent":
            events = self.memory_events.setdefault(key, [])
            event = {
                "memoryId": key[0],
                "actorId": key[1],
                "sessionId": key[2],
                "eventId": f"evt-{len(events) + 1:04d}-{uuid.uuid4().hex[:6]}",
                "eventTimestamp": p.get("eventTimestamp") or datetime(2026, 11, 1, tzinfo=timezone.utc),
                "payload": p.get("payload", []),
            }
            if "metadata" in p:
                event["metadata"] = p["metadata"]
            if "branch" in p:
                event["branch"] = p["branch"]
            events.append(event)
            return 200, {"event": event}
        if operation == "RetrieveMemoryRecords":
            return 200, {"memoryRecordSummaries": []}
        if operation == "ListSessions":
            return 200, {"sessionSummaries": []}
        if operation == "ListMemoryRecords":
            return 200, {"memoryRecordSummaries": []}
        if operation == "GetWorkloadAccessTokenForJWT" or operation.startswith("GetWorkloadAccessToken"):
            return 200, {"workloadAccessToken": "workload-token"}
        if operation == "GetResourceOauth2Token":
            return 200, {}
        self.unhandled[f"bedrock-agentcore.{operation}"] += 1
        return 200, {}

    def _runtime(self, operation: str, p: dict) -> Tuple[int, dict]:
        if operation == "ConverseStream":
            return 200, {"stream": iter(self.model.next())}
        if operation == "Converse":
            return 200, {
                "output": {"message": {"role": "assistant", "content": [{"text": "Quarterly Planning"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 40, "outputTokens": 3, "totalTokens": 43},
                "metrics": {"latencyMs": 1},
            }
        if operation == "InvokeModel":
            body = json.dumps({"embedding": [0.01] * 1024, "inputTextTokenCount": 8}).encode()
            return 200, {"body": StreamingBody(io.BytesIO(body), len(body)), "contentType": "application/json"}
        if operation == "CountTokens":
            return 200, {"inputTokens": 1200}
        self.unhandled[f"bedrock-runtime.{operation}"] += 1
        return 200, {}


def _memory_resource(memory_id: str) -> dict:
    return {
        "id": memory_id,
        "arn": f"arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/{memory_id}",
        "name": memory_id,
        "status": "ACTIVE",
        "eventExpiryDuration": 90,
        "strategies": [
            {
                "strategyId": "pref-1",
                "name": "preferences",
                "type": "USER_PREFERENCE",
                "namespaces": ["/preferences/{actorId}"],
                "status": "ACTIVE",
            },
            {
                "strategyId": "facts-1",
                "name": "facts",
                "type": "SEMANTIC",
                "namespaces": ["/facts/{actorId}"],
                "status": "ACTIVE",
            },
        ],
    }
