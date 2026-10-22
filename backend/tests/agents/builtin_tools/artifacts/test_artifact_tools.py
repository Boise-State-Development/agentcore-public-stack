"""Tests for the artifact authoring tools.

The headline guarantee is `test_record_satisfies_minter`: a row written
by this tool must be accepted byte-for-byte by #310's app-api minter
(the real downstream reader) and resolve to the S3 object #309's render
Lambda would serve.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import pytest
from moto import mock_aws

from agents.builtin_tools.artifacts import service
from apis.inference_api.chat.routes import _build_artifact_tools
from lambdas.artifact_render import handler as render_handler

REGION = "us-east-1"
TABLE = "test-user-artifacts"
BUCKET = "test-artifacts-content"
USER = "user-123"
SESSION = "sess-9"
DOC = "<!doctype html><html><body><h1>hi</h1></body></html>"
MD = "# Title\n\nSome **bold** text and a list:\n\n- one\n- two\n"


def _embedded_markdown(body: str) -> str:
    """Decode the base64 source the render wrapper embeds (no `<` in
    base64, so the cheap regex is safe)."""
    m = re.search(r'id="md-src">([^<]+)</script>', body)
    assert m, "render wrapper is missing the embedded markdown block"
    return base64.b64decode(m.group(1)).decode()


@pytest.fixture(autouse=True)
def _reset() -> None:
    service._reset_caches_for_tests()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", REGION)
        monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
        monkeypatch.setenv("DYNAMODB_ARTIFACTS_TABLE_NAME", TABLE)

        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "SessionIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION), boto3.client(
            "s3", region_name=REGION
        )


def _item(ddb, artifact_id: str, sk_suffix: str) -> dict:
    return ddb.Table(TABLE).get_item(
        Key={"PK": f"USER#{USER}", "SK": f"ARTIFACT#{artifact_id}#{sk_suffix}"}
    ).get("Item")


def test_create_writes_s3_and_rows(aws) -> None:
    ddb, s3 = aws
    aid, ver = service.create_artifact_record(USER, SESSION, "My Art", DOC, "")
    assert ver == 1

    key = f"{USER}/{aid}/v1/index.html"
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode() == DOC

    vrow = _item(ddb, aid, "V#00001")
    assert vrow["storage"] == "s3"
    assert vrow["content_key"] == key
    assert vrow["content_type"] == "text/html; charset=utf-8"

    head = _item(ddb, aid, "HEAD")
    assert head["version"] == 1
    assert head["GSI1PK"] == f"SESSION#{SESSION}"
    assert head["GSI1SK"].startswith("ARTIFACT#") and head["GSI1SK"].endswith(aid)


def test_update_increments_and_preserves_old(aws) -> None:
    ddb, s3 = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "T", DOC, "")
    new_doc = "<!doctype html><html><body>v2</body></html>"
    ver = service.update_artifact_record(USER, aid, new_doc, None, None)
    assert ver == 2

    # Old version object is immutable / still present.
    assert s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v1/index.html"
    )["Body"].read().decode() == DOC
    assert s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v2/index.html"
    )["Body"].read().decode() == new_doc

    assert _item(ddb, aid, "V#00002")["content_key"] == f"{USER}/{aid}/v2/index.html"
    head = _item(ddb, aid, "HEAD")
    assert head["version"] == 2
    assert head["title"] == "T"  # carried forward


def test_head_rows_carry_user_index_keys_on_both_write_paths(aws) -> None:
    """GSI2PK/GSI2SK are stamped ahead of the index that will consume them.

    No index exists yet (the fixture table declares only SessionIndex), so
    nothing queries these — but a sparse GSI only ever contains rows that
    already carry its key attributes. A write path that stops stamping them
    produces rows permanently invisible to the future UserArtifactsIndex,
    and does so silently. Both paths are asserted because `update` re-puts
    HEAD wholesale: dropping the attributes there would strip them from
    every artifact that is ever edited.
    """
    ddb, _ = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "T", DOC, "")

    head = _item(ddb, aid, "HEAD")
    assert head["GSI2PK"] == f"USER#{USER}"
    # Sorts newest-first within the user's partition, so it must track
    # updated_at rather than creation time.
    assert head["GSI2SK"] == f"ARTIFACT#{head['updated_at']}#{aid}"

    # Sparse by design: one indexed row per artifact, not one per version.
    assert "GSI2PK" not in _item(ddb, aid, "V#00001")

    service.update_artifact_record(USER, aid, "<p>v2</p>", None, None)

    updated = _item(ddb, aid, "HEAD")
    assert updated["GSI2PK"] == f"USER#{USER}"
    assert updated["GSI2SK"] == f"ARTIFACT#{updated['updated_at']}#{aid}"
    assert updated["GSI2SK"] > head["GSI2SK"]
    assert "GSI2PK" not in _item(ddb, aid, "V#00002")


def test_update_unknown_artifact_raises(aws) -> None:
    with pytest.raises(service.ArtifactNotFoundError):
        service.update_artifact_record(USER, "nope", DOC, None, None)


def test_update_foreign_artifact_raises(aws) -> None:
    aid, _ = service.create_artifact_record(USER, SESSION, "T", DOC, "")
    with pytest.raises(service.ArtifactNotFoundError):
        service.update_artifact_record("someone-else", aid, DOC, None, None)


def test_content_type_default(aws) -> None:
    ddb, _ = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "T", DOC, "")
    assert _item(ddb, aid, "V#00001")["content_type"] == "text/html; charset=utf-8"


def test_markdown_create_wraps_and_preserves_type(aws) -> None:
    ddb, s3 = aws
    aid, ver = service.create_artifact_record(
        USER, SESSION, "Notes", MD, "text/markdown"
    )
    assert ver == 1

    # DDB keeps the authored Markdown type — drives the SPA card badge
    # and list; the render Lambda maps it to text/html when serving.
    assert _item(ddb, aid, "V#00001")["content_type"] == "text/markdown"

    body = s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v1/index.html"
    )["Body"].read().decode()
    assert body.lstrip().startswith("<!doctype html>")
    assert "https://esm.sh/marked@14.1.4" in body
    # Source is base64-embedded, never inlined raw (escaping/XSS-safe).
    assert MD not in body
    assert _embedded_markdown(body) == MD


def test_markdown_charset_suffix_still_markdown(aws) -> None:
    _, s3 = aws
    aid, _ = service.create_artifact_record(
        USER, SESSION, "Doc", MD, "text/markdown; charset=utf-8"
    )
    body = s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v1/index.html"
    )["Body"].read().decode()
    assert _embedded_markdown(body) == MD


def test_markdown_update_rewraps_inherited_type(aws) -> None:
    _, s3 = aws
    aid, _ = service.create_artifact_record(
        USER, SESSION, "Doc", MD, "text/markdown"
    )
    new_md = "## v2\n\nrevised body\n"
    # content_type omitted → inherits Markdown from HEAD, must re-wrap.
    ver = service.update_artifact_record(USER, aid, new_md, None, None)
    assert ver == 2
    body = s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v2/index.html"
    )["Body"].read().decode()
    assert body.lstrip().startswith("<!doctype html>")
    assert _embedded_markdown(body) == new_md


def test_markdown_content_under_default_type_is_reclassified(aws) -> None:
    """Regression: raw Markdown authored under the default HTML type (the
    model forgot content_type="text/markdown") must be treated as Markdown
    so it renders, not stored verbatim as run-together HTML source."""
    ddb, s3 = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "Recipe", MD, "")

    # Row is corrected to Markdown → card badge reads "MD", not "HTML".
    assert _item(ddb, aid, "V#00001")["content_type"] == "text/markdown"

    body = s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v1/index.html"
    )["Body"].read().decode()
    assert body.lstrip().startswith("<!doctype html>")
    assert "https://esm.sh/marked@14.1.4" in body
    assert _embedded_markdown(body) == MD


def test_markdown_content_under_explicit_html_type_is_reclassified(aws) -> None:
    ddb, s3 = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "Recipe", MD, "text/html")
    assert _item(ddb, aid, "V#00001")["content_type"] == "text/markdown"
    body = s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v1/index.html"
    )["Body"].read().decode()
    assert _embedded_markdown(body) == MD


def test_full_html_document_not_reclassified(aws) -> None:
    """A genuine standalone HTML document keeps its HTML type and is
    stored verbatim (the safety net must not touch real HTML)."""
    ddb, s3 = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "Page", DOC, "text/html")
    assert _item(ddb, aid, "V#00001")["content_type"] == "text/html"
    assert s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v1/index.html"
    )["Body"].read().decode() == DOC


def test_update_reclassifies_markdown_under_inherited_html_type(aws) -> None:
    """An HTML artifact updated with raw Markdown (content_type omitted →
    inherits HTML from HEAD) is reclassified so the new version renders."""
    ddb, s3 = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "Page", DOC, "text/html")
    new_md = "# Rewritten\n\nNow it's **markdown**.\n"
    ver = service.update_artifact_record(USER, aid, new_md, None, None)
    assert ver == 2
    assert _item(ddb, aid, "V#00002")["content_type"] == "text/markdown"
    body = s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v2/index.html"
    )["Body"].read().decode()
    assert _embedded_markdown(body) == new_md


def test_html_artifact_not_wrapped(aws) -> None:
    _, s3 = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "Page", DOC, "text/html")
    assert s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v1/index.html"
    )["Body"].read().decode() == DOC


# --------------------------------------------------------------------------
# Math in the Markdown render wrapper. The wrapper's JS can't run here, so
# these pin the contract around it: exact CDN versions with SRI, the same
# KaTeX version and currency rule as the chat, and an artifact-origin CSP
# that admits every host the wrapper loads from.
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[5]
_CDN_URL_RE = re.compile(r'https://[^"\s]+')


def _wrapper() -> str:
    return service._wrap_markdown("Doc", MD)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _csp_sources(csp: str, name: str) -> list[str]:
    for directive in csp.split(";"):
        tokens = directive.split()
        if tokens and tokens[0] == name:
            return tokens[1:]
    raise AssertionError(f"{name} missing from CSP: {csp}")


def test_markdown_wrapper_loads_katex_pinned_with_sri() -> None:
    body = _wrapper()
    for asset in ("katex.min.css", "katex.min.js"):
        tag = re.search(
            rf"<(?:link|script)[^>]*/dist/{re.escape(asset)}\"[^>]*>", body
        )
        assert tag, f"wrapper does not load {asset}"
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/=]{64}"', tag.group(0))
        assert 'crossorigin="anonymous"' in tag.group(0)
    # Exact pins only: a range or dist-tag would let a CDN publish change
    # what every stored artifact renders (and break its SRI hash).
    for url in _CDN_URL_RE.findall(body):
        assert re.search(r"@\d+\.\d+\.\d+(/|$)", url), url


def test_markdown_wrapper_katex_matches_chat_version() -> None:
    package = REPO_ROOT / "frontend" / "ai.client" / "package.json"
    if not package.exists():
        pytest.skip("frontend package not in this checkout")
    chat = json.loads(package.read_text())["dependencies"]["katex"]
    versions = set(re.findall(r"katex@(\d+\.\d+\.\d+)/", _wrapper()))
    assert versions == {chat}


def test_markdown_wrapper_currency_rule_matches_chat() -> None:
    """The guarded `$…$` rule is what keeps "$5 fee" prose. It must be
    the chat's rule byte for byte (the raw-string template keeps every
    backslash), so a fix to one side can't silently skip the other."""
    source = (
        REPO_ROOT / "frontend" / "ai.client" / "src" / "app" / "shared"
        / "utils" / "katex-math-markdown.ts"
    )
    if not source.exists():
        pytest.skip("frontend source not in this checkout")
    pattern = re.compile(r"const DOLLAR_INLINE_MATH = (/.+/);")
    chat = pattern.search(source.read_text())
    ours = pattern.search(_wrapper())
    assert chat and ours
    assert ours.group(1) == chat.group(1)


def test_markdown_wrapper_hosts_are_allowed_by_render_csp() -> None:
    body = _wrapper()
    csp = render_handler._csp_header()
    construct = (
        REPO_ROOT / "infrastructure" / "lib" / "constructs" / "artifacts"
        / "artifact-render-lambda-construct.ts"
    )
    script_src = re.search(
        r'CSP_SCRIPT_SRC:\s*"([^"]+)"', construct.read_text()
    ).group(1).split()

    stylesheets = re.findall(r'<link rel="stylesheet"[^>]*href="([^"]+)"', body)
    scripts = re.findall(r'<script[^>]*\ssrc="([^"]+)"', body) + re.findall(
        r'import [^;]+ from "([^"]+)"', body
    )
    assert stylesheets and scripts
    for url in stylesheets:
        assert _origin(url) in _csp_sources(csp, "style-src"), url
        # The stylesheet's fonts resolve relative to it, on the same host.
        assert _origin(url) in _csp_sources(csp, "font-src"), url
    for url in scripts:
        assert _origin(url) in script_src, url
    # Loading math assets must not reopen a channel out of the sandbox.
    assert _csp_sources(csp, "connect-src") == ["'none'"]


def test_ssm_fallback(aws, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env unset → resolve bucket/table from /{PROJECT_PREFIX}/artifacts/*."""
    monkeypatch.delenv("S3_ARTIFACTS_BUCKET_NAME", raising=False)
    monkeypatch.delenv("DYNAMODB_ARTIFACTS_TABLE_NAME", raising=False)
    monkeypatch.setenv("PROJECT_PREFIX", "myproj")
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name="/myproj/artifacts/bucket-name", Value=BUCKET, Type="String")
    ssm.put_parameter(Name="/myproj/artifacts/table-name", Value=TABLE, Type="String")
    service._reset_caches_for_tests()

    aid, ver = service.create_artifact_record(USER, SESSION, "T", DOC, "")
    assert ver == 1 and aid


def test_record_satisfies_minter(aws) -> None:
    """Cross-PR contract: the written version row must be accepted by
    #310's app-api minter and resolve to the S3 object #309 serves."""
    _, s3 = aws
    aid, ver = service.create_artifact_record(USER, SESSION, "T", DOC, "")

    from apis.app_api.artifacts import service as minter

    minter._reset_caches_for_tests()
    # Minter reads its own table handle from the same env we set.
    minter._assert_version_exists(USER, aid, ver)  # must not raise

    # And the content_key the readers trust actually points at content.
    vrow = _item(boto3.resource("dynamodb", region_name=REGION), aid, "V#00001")
    assert s3.get_object(
        Bucket=BUCKET, Key=vrow["content_key"]
    )["Body"].read().decode() == DOC


@pytest.mark.parametrize(
    "enabled,expected",
    # create_artifact is the single gate key: it provisions create + update.
    # A stale "update_artifact" preference on its own grants nothing.
    [(None, 0), ([], 0), (["other"], 0), (["update_artifact"], 0),
     (["create_artifact"], 2),
     (["create_artifact", "update_artifact"], 2)],
)
def test_routes_gating(enabled, expected) -> None:
    tools = _build_artifact_tools(enabled, SESSION, USER)
    assert len(tools) == expected


def test_list_session_artifacts_returns_heads_newest_first(aws) -> None:
    a1, _ = service.create_artifact_record(USER, SESSION, "First", DOC, "")
    a2, _ = service.create_artifact_record(USER, SESSION, "Second", DOC, "")
    # Bump a1 to v2 so it becomes the most-recently-updated HEAD.
    service.update_artifact_record(USER, a1, DOC, None, None)

    rows = service.list_session_artifacts(USER, SESSION)
    by_id = {r["artifact_id"]: r for r in rows}
    assert set(by_id) == {a1, a2}
    assert by_id[a1]["version"] == 2  # reflects current HEAD, not v1
    assert by_id[a2]["title"] == "Second"
    # Newest-first: a1 (just updated) precedes a2.
    assert [r["artifact_id"] for r in rows] == [a1, a2]


def test_list_session_artifacts_scopes_to_user(aws) -> None:
    mine, _ = service.create_artifact_record(USER, SESSION, "Mine", DOC, "")
    service.create_artifact_record("someone-else", SESSION, "Theirs", DOC, "")

    rows = service.list_session_artifacts(USER, SESSION)
    assert [r["artifact_id"] for r in rows] == [mine]


def test_list_session_artifacts_empty_session(aws) -> None:
    assert service.list_session_artifacts(USER, "no-such-session") == []


def test_set_produced_by_message_index_stamps_version_and_head(aws) -> None:
    ddb, _ = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "Doc", DOC, "")
    assert service.list_session_artifacts(USER, SESSION)[0][
        "produced_by_message_index"
    ] is None

    service.set_produced_by_message_index(USER, aid, 1, 7)

    # Per-version linkage: the v1 row itself carries the index — this is
    # what survives reload via the all-versions list endpoint.
    assert _item(ddb, aid, "V#00001")["produced_by_message_index"] == 7
    # HEAD is stamped too, so the writer's HEAD-based live list still sees it.
    rows = service.list_session_artifacts(USER, SESSION)
    assert rows[0]["produced_by_message_index"] == 7
    # The stamp must leave the optimistic-lock `version` untouched so a
    # later update_artifact still re-points HEAD cleanly.
    assert service.update_artifact_record(USER, aid, DOC, None, None) == 2


def test_set_produced_by_message_index_requires_existing_rows(aws) -> None:
    from botocore.exceptions import ClientError

    # No version/HEAD rows for "nope": the conditional update fails closed.
    with pytest.raises(ClientError):
        service.set_produced_by_message_index(USER, "nope", 1, 1)
