"""The excerpt an Outputs card shows (Shared Projects 3.3).

``excerpt`` is pure. The listing tests reuse the moto world of
``test_project_outputs`` and add the artifacts bucket, so the first listing
reads real S3 content and later ones read the pointer.
"""

from __future__ import annotations

import base64

import boto3
import pytest

from apis.app_api.artifacts import preview, service as artifact_service
from apis.app_api.artifacts.preview import excerpt

from tests.apis.app_api.artifacts.test_project_outputs import (  # noqa: F401 - fixtures
    ARTIFACTS,
    AUTHOR,
    REGION,
    VIEWER,
    _outputs,
    _reset_caches,
    _share,
    world,
)

BUCKET = "test-artifacts-bucket"


class TestExcerpt:
    def test_markdown_keeps_its_opening_and_stops_on_a_line(self):
        doc = "# Week 7 digest\n\n" + "\n".join(f"- point {i} " + "x" * 40 for i in range(40))
        out = excerpt(doc, "text/markdown; charset=utf-8")
        assert out.startswith("# Week 7 digest")
        assert len(out) <= preview.MARKDOWN_CHARS
        assert doc.startswith(out)
        assert out.endswith("x")

    def test_csv_keeps_the_header_and_first_rows_bounded(self):
        rows = ["section,seats,enrolled"] + [f"00{i},24,{20 + i}" for i in range(20)]
        rows[1] += "," + "y" * 500
        out = excerpt("\n".join(rows), "text/csv").splitlines()
        assert len(out) == preview.CSV_ROWS
        assert out[0] == "section,seats,enrolled"
        assert all(len(r) <= preview.CSV_ROW_CHARS for r in out)

    def test_a_page_reads_as_its_visible_text(self):
        page = (
            "<!doctype html><html><head><title>T</title><style>body{color:red}</style></head>"
            "<body><!-- note --><h1>Mitosis quiz</h1><script>alert(1)</script>"
            "<p>Which phase &amp; why?</p></body></html>"
        )
        assert excerpt(page, "text/html; charset=utf-8") == "Mitosis quiz Which phase & why?"

    def test_a_small_svg_is_kept_whole_and_a_large_one_dropped(self):
        small = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'
        assert excerpt(small, "image/svg+xml") == small
        large = "<svg>" + "<rect/>" * preview.SVG_BYTES + "</svg>"
        assert excerpt(large, "image/svg+xml") == ""

    def test_anything_else_is_bounded_text(self):
        out = excerpt("word " * 500, "text/plain")
        assert 0 < len(out) <= preview.TEXT_CHARS
        assert not out.endswith(" ")


def _md_wrapper(markdown: str) -> str:
    """The writer's Markdown HTML wrapper, as ``ArtifactContentService`` unwraps it."""
    b64 = base64.b64encode(markdown.encode()).decode()
    return f'<html><body><script type="application/x-markdown-base64" id="md-src">{b64}</script></body></html>'


@pytest.fixture()
def bucket(world, monkeypatch):
    monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    return s3


def _markdown_artifact(s3, artifact: str = "art-1", version: int = 1, text: str = "# Lab 6 rubric\n\nSlide prep: 6 pts") -> None:
    table = boto3.resource("dynamodb", region_name=REGION).Table(ARTIFACTS)
    table.put_item(Item={"PK": f"USER#{AUTHOR.user_id}", "SK": f"ARTIFACT#{artifact}#HEAD",
                         "artifact_id": artifact, "session_id": "s-proj", "title": "Rubric"})
    key = f"k/{artifact}/{version}"
    table.put_item(Item={
        "PK": f"USER#{AUTHOR.user_id}", "SK": f"ARTIFACT#{artifact}#V#{version:05d}", "artifact_id": artifact,
        "user_id": AUTHOR.user_id, "version": version, "storage": "s3", "content_key": key,
        "content_type": "text/markdown; charset=utf-8", "title": "Rubric", "session_id": "s-proj",
    })
    s3.put_object(Bucket=BUCKET, Key=key, Body=_md_wrapper(text).encode())


class TestListing:
    def test_the_first_listing_computes_it_and_later_ones_read_the_pointer(self, world, bucket, monkeypatch):
        _markdown_artifact(bucket)
        assert _share(world).status_code == 201

        [out] = _outputs(world, VIEWER)
        assert out["preview"] == "# Lab 6 rubric\n\nSlide prep: 6 pts"
        stored = world["projects"].repository.get_output(world["project"].project_id, "art-1")
        assert stored.preview == out["preview"]

        def unreachable(*_a, **_k):
            raise AssertionError("a listing with a stored preview must not read the content")

        monkeypatch.setattr(artifact_service.ArtifactContentService, "get", unreachable)
        assert _outputs(world, AUTHOR)[0]["preview"] == out["preview"]

    def test_missing_content_is_remembered_as_none(self, world, bucket):
        _markdown_artifact(bucket)
        bucket.delete_object(Bucket=BUCKET, Key="k/art-1/1")
        _share(world)

        assert _outputs(world)[0]["preview"] is None
        assert world["projects"].repository.get_output(world["project"].project_id, "art-1").preview == ""

    def test_an_unreadable_store_lists_without_one_and_tries_again(self, world, bucket, monkeypatch):
        _markdown_artifact(bucket)
        _share(world)
        monkeypatch.delenv("S3_ARTIFACTS_BUCKET_NAME")
        artifact_service._reset_caches_for_tests()

        assert _outputs(world)[0]["preview"] is None
        assert world["projects"].repository.get_output(world["project"].project_id, "art-1").preview is None

    def test_a_newer_version_shared_since_is_not_given_the_old_excerpt(self, world, bucket):
        _markdown_artifact(bucket)
        _share(world)
        repo = world["projects"].repository
        pid = world["project"].project_id

        repo.set_output_preview(pid, "art-1", 2, "stale")
        assert repo.get_output(pid, "art-1").preview is None
        repo.set_output_preview(pid, "gone", 1, "stale")
        assert repo.get_output(pid, "gone") is None
