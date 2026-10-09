"""Content lint for project memory (Shared Projects §4.3 step 6, §9.3).

A deterministic pattern check over text entering a project's memory: text that
reads like an instruction to the assistant ("ignore previous instructions",
"you must now"), prompt or tool-call markup, secret-shaped strings, and the
deployment's own ``MEMORY_SENSITIVE_PATTERNS``. No model and no redaction: the
structural rule (memory enters the prompt as a labelled data block, never as
instructions) is the defence, and this check only tells a person what they
are about to share.

``MEMORY_LINT_MODE`` picks what a finding does: ``warn`` (the default) saves
and reports it, ``block`` refuses the save, ``off`` skips the check. It applies
to project scopes only (a project's shared space and a member's own space in
it); personal spaces and Agent memory are untouched.

**What is checked.** Every item whose text a save adds or changes, on every
write path; an item the file already holds, word for word, is not read again.
An item a split moves keeps its text and is skipped. A file's description is
checked when it changes, and the index (``MEMORY.md``) line by line, new lines
only. **Only new findings block:** a finding the item's previous text already
had is a warning even in ``block`` mode, the way a dead link the file already
had is (2.3a), so turning ``block`` on never stops an unrelated edit.

**Nothing reaches the prompt.** Findings travel on the save's response (and a
tool's result) and are recomputed on read for the Memory page and the review
queue. No marker is written into the file, so the injected index and
``memory_read`` are byte for byte what they were.

Settings come from ``MEMORY_LINT_MODE`` and ``MEMORY_SENSITIVE_PATTERNS``
(app-api, the maintenance worker, local runs). The AgentCore Runtime's
environment is full, and V2 caps it at 2,560 bytes, so it gets one small
packed value, ``MEMORY_LINT``: ``{"mode": "warn"}``, plus ``"patterns":
"ssm:<hash>"`` when the deployment has patterns. Those are then read once per
process from the SSM parameter ``/{PROJECT_PREFIX}/memory/sensitive-patterns``,
which CDK creates only when there are some; the hash changes the Runtime's
environment, and so rolls its containers, whenever the patterns change. A
packed ``"sensitivePatterns"`` value is read inline (local runs). Patterns are
compiled once per distinct setting.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple

from .format import Item

logger = logging.getLogger(__name__)

LintMode = Literal["off", "warn", "block"]
LintCategory = Literal["instruction", "markup", "secret", "sensitive"]
LintWhere = Literal["item", "description", "index"]

LINT_MODES: Tuple[str, ...] = ("off", "warn", "block")
DEFAULT_MODE: LintMode = "warn"

# The scopes lint applies to: a project's shared space and a member's own space in it.
PROJECT_SCOPES = frozenset({"shared", "personal_in_project"})

# Deployment patterns are an operator's input: bounded, and refused when they
# nest a quantifier inside a quantified group, the usual catastrophic-backtracking shape.
MAX_SENSITIVE_PATTERNS = 100
MAX_PATTERN_CHARS = 500
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,)")

# A quoted match is cut to this, and a save's response names at most this many findings.
_EXCERPT_CHARS = 60
MAX_MESSAGES = 5

_CATEGORY_TEXT: Dict[str, str] = {
    "instruction": "reads like an instruction to the assistant",
    "markup": "contains prompt or tool-call markup",
    "secret": "looks like it contains a credential",
    "sensitive": "matches a pattern this deployment treats as sensitive",
}

WARN_ADVICE = (
    "Tasks read memory as reference material, so write facts rather than instructions, "
    "and keep credentials out of it."
)
BLOCK_ADVICE = "The content check doesn't allow this in project memory, so take it out and save again."


@dataclass(frozen=True)
class LintRule:
    """One pattern. ``quote`` says whether a match may be quoted back (never a secret)."""

    rule_id: str
    category: LintCategory
    pattern: "re.Pattern[str]"
    label: Optional[str] = None
    quote: bool = True


@dataclass(frozen=True)
class LintFinding:
    """One rule matched in one place. ``position`` is 1-based (item number, or index line number)."""

    rule: str
    category: LintCategory
    where: LintWhere
    position: Optional[int] = None
    anchor: Optional[str] = None
    label: Optional[str] = None
    excerpt: Optional[str] = None
    pre_existing: bool = False

    @property
    def place(self) -> str:
        if self.where == "description":
            return "The description"
        if self.where == "index":
            return f"Line {self.position} of the index" if self.position else "The index"
        return f"Item {self.position}" if self.position else "An item"

    @property
    def text(self) -> str:
        """What was found, without where: "reads like an instruction to the assistant ("ignore previous instructions")"."""
        what = _CATEGORY_TEXT[self.category]
        if self.label:
            what = f"{what} ({self.label})"
        if self.excerpt:
            what = f"{what}: “{self.excerpt}”"
        return what

    def message(self) -> str:
        return f"{self.place} {self.text}."

    def summary(self) -> str:
        """What was found, as a sentence of its own, for a page that shows it beside the item."""
        return f"{self.text[0].upper()}{self.text[1:]}."

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "rule": self.rule, "category": self.category, "where": self.where,
            "message": self.message(), "summary": self.summary(),
        }
        for key, value in (("position", self.position), ("anchor", self.anchor), ("label", self.label), ("excerpt", self.excerpt)):
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class LintSettings:
    mode: LintMode
    rules: Tuple[LintRule, ...] = field(default_factory=tuple)


def _rule(rule_id: str, category: LintCategory, pattern: str, *, label: Optional[str] = None, flags: int = 0) -> LintRule:
    return LintRule(rule_id, category, re.compile(pattern, flags), label=label, quote=category in ("instruction", "markup"))


_I = re.IGNORECASE

# The built-in rules (§9.3). Kept narrow on purpose: a preference such as
# "Reply to status checks in three bullets" is what memory is for, so plain
# imperatives are not flagged; only the phrasing and markup of an injection are.
BUILTIN_RULES: Tuple[LintRule, ...] = (
    _rule(
        "ignore_instructions", "instruction",
        r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:(?:all|any|the|these|those|of)\s+){0,3}(?:(?:your|my)\s+)?"
        r"(?:previous|prior|above|earlier|preceding|system|original)\s+"
        r"(?:instructions?|prompts?|rules|directions|guidelines|directives)\b"
        r"|\b(?:ignore|disregard|forget|override)\s+(?:all\s+)?(?:of\s+)?your\s+(?:instructions|rules|guidelines|directives|programming)\b"
        r"|\b(?:ignore|disregard)\s+(?:everything\s+|all\s+)?(?:previous|prior|above|earlier)\b|\bforget\s+everything\b",
        flags=_I,
    ),
    _rule("you_must_now", "instruction", r"\byou\s+(?:must|will|shall)\s+now\b", flags=_I),
    _rule(
        "new_instructions", "instruction",
        r"\b(?:new|updated|real|actual|additional)\s+(?:system\s+)?instructions?\s*:",
        flags=_I,
    ),
    _rule(
        "reveal_prompt", "instruction",
        r"\b(?:reveal|print|repeat|output|disclose|leak)\s+(?:your|the)\s+(?:system\s+prompt|hidden\s+instructions|instructions)\b",
        flags=_I,
    ),
    _rule(
        "prompt_tags", "markup",
        r"</?\s*(?:system|user_instructions|project_memory|memory_space|shared_task|user_context|instructions)\s*>"
        r"|<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>|\[/?INST\]|<</?SYS>>",
        flags=_I,
    ),
    _rule(
        "tool_call_markup", "markup",
        r"</?\s*(?:function_calls|function_results|invoke|tool_use|tool_call|tool_result)\b|</?antml:\w+"
        r"|\"(?:toolUse|tool_use|tool_calls|function_call)\"\s*:",
        flags=_I,
    ),
    _rule("aws_access_key", "secret", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b", label="an AWS access key"),
    _rule("private_key", "secret", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----", label="a private key"),
    _rule("github_token", "secret", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})", label="a GitHub token"),
    _rule("slack_token", "secret", r"\bxox[abposr]-[A-Za-z0-9-]{10,}", label="a Slack token"),
    _rule(
        "api_key", "secret",
        r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|(?:sk|rk)_live_[0-9A-Za-z]{20,})",
        label="an API key",
    ),
    _rule("jwt", "secret", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", label="a signed token"),
    _rule(
        "assigned_secret", "secret",
        r"\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)\s*[:=]\s*"
        r"[\"']?(?![$<{])[^\s\"'<>]{8,}",
        label="a password or secret", flags=_I,
    ),
    _rule("url_credentials", "secret", r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]{3,}@", label="a password in a URL", flags=_I),
)


# ---- settings -------------------------------------------------------------


def _parse_patterns(raw: object) -> List[Tuple[str, Optional[str]]]:
    """``[(pattern, label)]`` from a JSON list (strings or ``{pattern, label}``), or one pattern per line."""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except ValueError:
                logger.error("MEMORY_SENSITIVE_PATTERNS is not a valid JSON list; no deployment patterns apply")
                return []
        else:
            return [(line.strip(), None) for line in text.splitlines() if line.strip()]
    if not isinstance(raw, list):
        logger.error("MEMORY_SENSITIVE_PATTERNS must be a list; no deployment patterns apply")
        return []
    out: List[Tuple[str, Optional[str]]] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            out.append((entry.strip(), None))
        elif isinstance(entry, dict) and isinstance(entry.get("pattern"), str) and entry["pattern"].strip():
            label = entry.get("label")
            out.append((entry["pattern"].strip(), label.strip() if isinstance(label, str) and label.strip() else None))
        else:
            logger.error("Skipping a MEMORY_SENSITIVE_PATTERNS entry that is neither a string nor {pattern, label}")
    return out


def compile_sensitive_patterns(raw: object) -> Tuple[LintRule, ...]:
    """The deployment's patterns as rules. A bad one is logged and skipped: a typo never fails a save."""
    rules: List[LintRule] = []
    for n, (pattern, label) in enumerate(_parse_patterns(raw)[:MAX_SENSITIVE_PATTERNS], start=1):
        if len(pattern) > MAX_PATTERN_CHARS:
            logger.error("Skipping sensitive pattern %d: longer than %d characters", n, MAX_PATTERN_CHARS)
            continue
        if _NESTED_QUANTIFIER.search(pattern):
            logger.error("Skipping sensitive pattern %d: a quantified group inside a quantifier can run for minutes", n)
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            logger.error("Skipping sensitive pattern %d: %s", n, exc)
            continue
        rules.append(LintRule(f"sensitive:{n}", "sensitive", compiled, label=label, quote=False))
    return tuple(rules)


def _normalize_mode(raw: Optional[str]) -> Optional[LintMode]:
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in LINT_MODES:
        return value  # type: ignore[return-value]
    logger.error("MEMORY_LINT_MODE=%r is not off, warn or block; using %s", raw, DEFAULT_MODE)
    return DEFAULT_MODE


_cache_lock = threading.Lock()
_cache: Dict[Tuple[str, str, str], LintSettings] = {}

# The Runtime's patterns, from SSM (see the module docstring). A failed read is
# retried after this long; until then the deployment's patterns don't apply.
PATTERNS_SSM_SUFFIX = "memory/sensitive-patterns"
_SSM_RETRY_SECONDS = 60.0
_ssm_failed_at: Optional[float] = None


def _patterns_from_ssm() -> Optional[str]:
    """The deployment's patterns from ``/{PROJECT_PREFIX}/memory/sensitive-patterns``, or None if unreadable.

    One ``GetParameter`` per process, on its first project-memory save (a
    tool's call, never a turn's first token). The Runtime's role already
    reads ``/{prefix}/*``. A failure is logged at error and retried a minute
    later; the built-in rules still apply meanwhile.
    """
    global _ssm_failed_at
    if _ssm_failed_at is not None and time.monotonic() - _ssm_failed_at < _SSM_RETRY_SECONDS:
        return None
    prefix = os.environ.get("PROJECT_PREFIX", "").strip()
    if not prefix:
        logger.error("MEMORY_LINT names patterns in SSM but PROJECT_PREFIX is unset; no deployment patterns apply")
        _ssm_failed_at = time.monotonic()
        return None
    try:
        import boto3  # lazily: only the Runtime takes this path

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        response = boto3.client("ssm", region_name=region).get_parameter(Name=f"/{prefix}/{PATTERNS_SSM_SUFFIX}")
    except Exception as exc:  # noqa: BLE001 - a lint setting never fails a save
        logger.error("Could not read the memory sensitive patterns from SSM (%s); retrying in a minute", type(exc).__name__)
        _ssm_failed_at = time.monotonic()
        return None
    _ssm_failed_at = None
    return response["Parameter"]["Value"]


def lint_settings() -> LintSettings:
    """The deployment's settings: ``MEMORY_LINT_MODE`` / ``MEMORY_SENSITIVE_PATTERNS``, else the packed ``MEMORY_LINT``.

    Read from the environment on every call and compiled once per distinct
    value, so a save pays a dict lookup.
    """
    key = (
        os.environ.get("MEMORY_LINT_MODE", ""),
        os.environ.get("MEMORY_SENSITIVE_PATTERNS", ""),
        os.environ.get("MEMORY_LINT", ""),
    )
    cached = _cache.get(key)
    if cached is not None:
        return cached
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        mode_raw, patterns_raw, packed_raw = key
        packed: Dict[str, object] = {}
        if packed_raw.strip():
            try:
                loaded = json.loads(packed_raw)
                packed = loaded if isinstance(loaded, dict) else {}
            except ValueError:
                logger.error("MEMORY_LINT is not valid JSON; using the defaults")
        mode = _normalize_mode(mode_raw) or _normalize_mode(str(packed.get("mode") or "")) or DEFAULT_MODE
        patterns: object = patterns_raw if patterns_raw.strip() else packed.get("sensitivePatterns")
        complete = True
        if not patterns_raw.strip() and str(packed.get("patterns") or "").startswith("ssm"):
            patterns = _patterns_from_ssm()
            complete = patterns is not None
        settings = LintSettings(mode=mode, rules=BUILTIN_RULES + compile_sensitive_patterns(patterns))
        if complete:
            _cache[key] = settings
        return settings


def lint_mode_for(scope: Optional[str], settings: Optional[LintSettings] = None) -> LintMode:
    """The mode a space's saves run under: ``off`` outside a project's scopes.

    The seam for Phase 4.2, whose regulated-data designation forces ``block``
    for one project.
    """
    if scope not in PROJECT_SCOPES:
        return "off"
    return (settings or lint_settings()).mode


# ---- checks ---------------------------------------------------------------


def _excerpt(match: "re.Match[str]") -> str:
    text = " ".join(match.group(0).split())
    return text if len(text) <= _EXCERPT_CHARS else text[: _EXCERPT_CHARS - 1].rstrip() + "…"


def scan(text: str, rules: Sequence[LintRule]) -> List[Tuple[LintRule, Optional[str]]]:
    """Each rule that matches ``text``, once, with a quotable excerpt where the rule allows one."""
    found: List[Tuple[LintRule, Optional[str]]] = []
    if not text:
        return found
    for rule in rules:
        match = rule.pattern.search(text)
        if match is not None:
            found.append((rule, _excerpt(match) if rule.quote else None))
    return found


def lint_items(
    items: Sequence[Item],
    settings: LintSettings,
    *,
    previous: Mapping[str, str] = {},
    skip: Iterable[str] = (),
    only_changed: bool = True,
) -> List[LintFinding]:
    """Findings for the items whose text is new to the file (all items when ``only_changed`` is off).

    ``previous`` maps each anchor the file holds now to its text. A rule the
    anchor's previous text also matched is ``pre_existing``.
    """
    skipped = set(skip)
    checked: List[Tuple[int, Item, Optional[str]]] = []
    for position, item in enumerate(items, start=1):
        if item.anchor in skipped:
            continue
        before = previous.get(item.anchor) if item.anchor else None
        if only_changed and before is not None and before == item.text:
            continue
        checked.append((position, item, before))
    findings: List[LintFinding] = []
    for position, item, before in checked:
        found = scan(item.text, settings.rules)
        if not found:
            continue
        had = {rule.rule_id for rule, _ in scan(before, settings.rules)} if before else set()
        for rule, excerpt in found:
            findings.append(
                LintFinding(
                    rule=rule.rule_id, category=rule.category, where="item", position=position, anchor=item.anchor,
                    label=rule.label, excerpt=excerpt, pre_existing=rule.rule_id in had,
                )
            )
    return findings


def lint_description(description: str, previous: str, settings: LintSettings) -> List[LintFinding]:
    """Findings for a description that changed; it becomes the file's index line."""
    if not description or description == previous:
        return []
    had = {rule.rule_id for rule, _ in scan(previous, settings.rules)}
    return [
        LintFinding(
            rule=rule.rule_id, category=rule.category, where="description", label=rule.label, excerpt=excerpt,
            pre_existing=rule.rule_id in had,
        )
        for rule, excerpt in scan(description, settings.rules)
    ]


def lint_index(text: str, previous: str, settings: LintSettings) -> List[LintFinding]:
    """Findings for the index's lines that it didn't have before."""
    before = set(previous.split("\n"))
    lines = [(n, line) for n, line in enumerate(text.split("\n"), start=1) if line.strip() and line not in before]
    findings: List[LintFinding] = []
    for number, line in lines:
        for rule, excerpt in scan(line, settings.rules):
            findings.append(
                LintFinding(rule=rule.rule_id, category=rule.category, where="index", position=number, label=rule.label, excerpt=excerpt)
            )
    return findings


def warning_messages(findings: Sequence[LintFinding], *, limit: int = MAX_MESSAGES) -> List[str]:
    """One sentence per finding (at most ``limit``), then one line of advice."""
    if not findings:
        return []
    lines = [f.message() for f in findings[:limit]]
    if len(findings) > limit:
        lines.append(f"…and {len(findings) - limit} more.")
    return lines + [WARN_ADVICE]


def block_message(findings: Sequence[LintFinding], *, limit: int = MAX_MESSAGES) -> str:
    """The one message a refused save returns."""
    lines = [f.message() for f in findings[:limit]]
    if len(findings) > limit:
        lines.append(f"…and {len(findings) - limit} more.")
    return " ".join(lines) + " " + BLOCK_ADVICE


def lint_for_read(
    scope: Optional[str],
    items: Sequence[Item],
    description: str = "",
    *,
    previous: Optional[Mapping[str, str]] = None,
    previous_description: str = "",
) -> List[LintFinding]:
    """Findings for the Memory page (a file as it stands) or a review (what a proposal changes).

    Without ``previous`` every item and the description are read. With it (the
    file now, by anchor), only what a save of ``items`` would add or change, the
    same rule a save applies, so a review never blames a proposal for text the
    file already holds. Recomputed on every read rather than stored, so a flag
    follows the file and the deployment's current patterns, and nothing about
    it is written anywhere a task reads. Empty when the scope's mode is ``off``.
    """
    settings = lint_settings()
    if lint_mode_for(scope, settings) == "off":
        return []
    if previous is None:
        return lint_description(description, "", settings) + lint_items(items, settings, only_changed=False)
    return lint_description(description, previous_description, settings) + lint_items(items, settings, previous=previous)
