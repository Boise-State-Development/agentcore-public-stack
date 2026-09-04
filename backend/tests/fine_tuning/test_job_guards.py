"""Tests for the guards that stand between a request and a billed GPU.

Each of these prevents a job that would provision hardware and then fail, or
spend past a user's quota. They are the reason a bad submission returns a 400
in milliseconds rather than an opaque traceback several billed minutes later.
"""

import httpx
import pytest
from fastapi import HTTPException

from apis.app_api.fine_tuning import task_types
from apis.app_api.fine_tuning.routes import (
    MIN_BUDGETED_RUNTIME_SECONDS,
    _budgeted_runtime,
    _validate_dataset_format,
    _validate_instance_type,
    preflight_huggingface_model,
)

TEXT_SPEC = task_types.get_task_spec(task_types.TEXT_CLASSIFICATION)
IMAGE_SPEC = task_types.get_task_spec(task_types.IMAGE_CLASSIFICATION)
IMAGE_TEXT_SPEC = task_types.get_task_spec(task_types.IMAGE_TEXT_CLASSIFICATION)


class TestDatasetFormatIsTaskAware:

    def test_text_task_accepts_a_manifest(self):
        _validate_dataset_format("datasets/u/1/train.csv", TEXT_SPEC)
        _validate_dataset_format("datasets/u/1/train.jsonl", TEXT_SPEC)

    def test_text_task_rejects_an_archive(self):
        with pytest.raises(HTTPException) as excinfo:
            _validate_dataset_format("datasets/u/1/train.zip", TEXT_SPEC)
        assert excinfo.value.status_code == 400

    def test_image_task_requires_an_archive(self):
        """A bare CSV cannot carry the images it references."""
        with pytest.raises(HTTPException) as excinfo:
            _validate_dataset_format("datasets/u/1/train.csv", IMAGE_SPEC)
        assert "zip" in excinfo.value.detail.lower()

    def test_image_task_accepts_a_zip(self):
        _validate_dataset_format("datasets/u/1/train.zip", IMAGE_SPEC)

    def test_error_names_the_required_columns(self):
        with pytest.raises(HTTPException) as excinfo:
            _validate_dataset_format("bad.csv", IMAGE_TEXT_SPEC)
        for column in ("image", "text", "label"):
            assert f'"{column}"' in excinfo.value.detail


class TestInstanceValidation:

    def test_accepts_a_priced_training_instance(self):
        _validate_instance_type("ml.g6.xlarge")

    def test_rejects_an_unpriced_instance(self):
        """An unpriced instance runs real GPUs and records no spend."""
        with pytest.raises(HTTPException) as excinfo:
            _validate_instance_type("ml.p4d.24xlarge")
        assert excinfo.value.status_code == 400

    def test_transform_uses_its_own_table(self):
        _validate_instance_type("ml.g6.xlarge", transform=True)

    def test_error_names_the_operation(self):
        with pytest.raises(HTTPException) as excinfo:
            _validate_instance_type("ml.p3.2xlarge", transform=True)
        assert "Batch Transform" in excinfo.value.detail


class TestBudgetedRuntime:
    """The budget becomes the stopping condition, bounding spend exactly."""

    def test_leaves_an_affordable_request_alone(self):
        # $100 buys ~71h on ml.g5.xlarge, well over the 4h requested.
        assert _budgeted_runtime(14400, "ml.g5.xlarge", 100.0) == 14400

    def test_clamps_to_what_the_budget_affords(self):
        # $10 / $1.408 per hour = 7.10h = 25568s, under the 24h requested.
        effective = _budgeted_runtime(86400, "ml.g5.xlarge", 10.0)
        assert effective == int((10.0 / 1.408) * 3600)
        assert effective < 86400

    def test_a_full_day_is_admitted_rather_than_rejected(self):
        """Worst-case rejection would block every job at the 24h default.

        24h on the cheapest instance is ~$27, more than any ordinary monthly
        quota, even though such a job typically finishes in minutes.
        """
        assert _budgeted_runtime(86400, "ml.g6.xlarge", 15.0) > 0

    def test_rejects_a_budget_too_small_to_be_useful(self):
        with pytest.raises(HTTPException) as excinfo:
            _budgeted_runtime(86400, "ml.g5.xlarge", 0.10)
        assert "Insufficient quota" in excinfo.value.detail

    def test_the_floor_is_exactly_the_minimum_runtime(self):
        rate = 1.408
        just_enough = (MIN_BUDGETED_RUNTIME_SECONDS / 3600) * rate
        assert _budgeted_runtime(86400, "ml.g5.xlarge", just_enough * 1.01) > 0
        with pytest.raises(HTTPException):
            _budgeted_runtime(86400, "ml.g5.xlarge", just_enough * 0.5)

    def test_transform_prices_against_the_transform_table(self):
        assert _budgeted_runtime(3600, "ml.g6.xlarge", 50.0, transform=True) == 3600


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        if self._error:
            raise self._error
        return self._response


def _patch_client(monkeypatch, *, response=None, error=None):
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: _FakeClient(response, error)
    )


@pytest.mark.asyncio
class TestHuggingFacePreflight:
    """Ask the Hub whether a model can serve the task before billing a GPU."""

    async def test_accepts_a_trainable_model(self, monkeypatch):
        _patch_client(
            monkeypatch,
            response=_FakeResponse(
                200,
                {
                    "pipeline_tag": "fill-mask",
                    "siblings": [{"rfilename": "model.safetensors"}],
                },
            ),
        )
        await preflight_huggingface_model("bert-base-uncased", TEXT_SPEC)

    async def test_rejects_a_gguf_only_repository(self, monkeypatch):
        """GGUF is a llama.cpp inference format and cannot be fine-tuned.

        Without this the job provisions a GPU and dies in from_pretrained.
        """
        _patch_client(
            monkeypatch,
            response=_FakeResponse(
                200,
                {
                    "pipeline_tag": "image-text-to-text",
                    "siblings": [
                        {"rfilename": "model-Q4_K_M.gguf"},
                        {"rfilename": "README.md"},
                    ],
                },
            ),
        )
        with pytest.raises(HTTPException) as excinfo:
            await preflight_huggingface_model("someone/model-GGUF", IMAGE_TEXT_SPEC)
        assert "GGUF" in excinfo.value.detail

    async def test_rejects_a_repository_with_no_weights(self, monkeypatch):
        _patch_client(
            monkeypatch,
            response=_FakeResponse(
                200, {"pipeline_tag": "fill-mask", "siblings": [{"rfilename": "README.md"}]}
            ),
        )
        with pytest.raises(HTTPException) as excinfo:
            await preflight_huggingface_model("someone/docs-only", TEXT_SPEC)
        assert "no loadable model weights" in excinfo.value.detail

    async def test_rejects_a_modality_mismatch(self, monkeypatch):
        """An image model cannot be fine-tuned for text classification."""
        _patch_client(
            monkeypatch,
            response=_FakeResponse(
                200,
                {
                    "pipeline_tag": "image-classification",
                    "siblings": [{"rfilename": "model.safetensors"}],
                },
            ),
        )
        with pytest.raises(HTTPException) as excinfo:
            await preflight_huggingface_model("google/vit-base-patch16-224", TEXT_SPEC)
        assert "not compatible" in excinfo.value.detail

    async def test_accepts_a_matching_modality(self, monkeypatch):
        _patch_client(
            monkeypatch,
            response=_FakeResponse(
                200,
                {
                    "pipeline_tag": "image-classification",
                    "siblings": [{"rfilename": "model.safetensors"}],
                },
            ),
        )
        await preflight_huggingface_model("google/vit-base-patch16-224", IMAGE_SPEC)

    async def test_reports_a_missing_model(self, monkeypatch):
        _patch_client(monkeypatch, response=_FakeResponse(404))
        with pytest.raises(HTTPException) as excinfo:
            await preflight_huggingface_model("nobody/nothing", TEXT_SPEC)
        assert "was not found" in excinfo.value.detail

    async def test_an_unreachable_hub_does_not_block_submission(self, monkeypatch):
        """The Hub being down is our problem, not the researcher's."""
        _patch_client(monkeypatch, error=httpx.ConnectError("boom"))
        await preflight_huggingface_model("bert-base-uncased", TEXT_SPEC)

    async def test_a_hub_error_response_does_not_block_submission(self, monkeypatch):
        _patch_client(monkeypatch, response=_FakeResponse(503))
        await preflight_huggingface_model("bert-base-uncased", TEXT_SPEC)

    async def test_an_untagged_model_is_allowed_through(self, monkeypatch):
        """Many valid checkpoints simply carry no pipeline_tag."""
        _patch_client(
            monkeypatch,
            response=_FakeResponse(
                200, {"pipeline_tag": None, "siblings": [{"rfilename": "model.safetensors"}]}
            ),
        )
        await preflight_huggingface_model("someone/untagged", TEXT_SPEC)
