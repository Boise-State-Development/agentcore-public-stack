"""Tests for the task-type registry and the invariants that hang off it."""

import pytest

from apis.app_api.fine_tuning import pricing, task_types
from apis.app_api.fine_tuning.job_models import (
    AVAILABLE_MODELS,
    MODEL_CATALOG,
    models_for_task,
)


class TestRegistry:

    def test_every_listed_task_resolves(self):
        for task_type in task_types.TASK_TYPES:
            assert task_types.get_task_spec(task_type).task_type == task_type

    def test_unknown_task_raises(self):
        with pytest.raises(ValueError, match="Unknown task type"):
            task_types.get_task_spec("interpretive-dance")

    def test_none_resolves_to_the_legacy_default(self):
        """Job rows written before task types existed have no attribute.

        They are all text classifiers and must keep resolving.
        """
        assert task_types.get_task_spec(None).task_type == task_types.TEXT_CLASSIFICATION
        assert task_types.get_task_spec("").task_type == task_types.TEXT_CLASSIFICATION

    def test_default_task_is_text_classification(self):
        assert task_types.DEFAULT_TASK_TYPE == task_types.TEXT_CLASSIFICATION

    def test_task_order_is_deterministic(self):
        """User-facing lists must not reorder between calls."""
        assert task_types.TASK_TYPES == tuple(task_types.TASK_TYPES)
        assert list(task_types.TASK_SPECS) == list(task_types.TASK_SPECS)

    @pytest.mark.parametrize("task_type", task_types.TASK_TYPES)
    def test_label_column_is_required(self, task_type):
        spec = task_types.get_task_spec(task_type)
        assert spec.label_column in spec.required_columns

    @pytest.mark.parametrize("task_type", task_types.TASK_TYPES)
    def test_image_tasks_require_an_archive(self, task_type):
        """A task referencing image files needs them bundled with the manifest."""
        spec = task_types.get_task_spec(task_type)
        if spec.image_column is not None:
            assert spec.requires_archive
            assert spec.image_column in spec.required_columns
            assert spec.upload_extensions == (".zip",)
        else:
            assert not spec.requires_archive

    def test_requires_images_predicate(self):
        assert not task_types.requires_images(task_types.TEXT_CLASSIFICATION)
        assert task_types.requires_images(task_types.IMAGE_CLASSIFICATION)
        assert task_types.requires_images(task_types.IMAGE_TEXT_CLASSIFICATION)

    def test_archive_task_list_matches_the_specs(self):
        assert task_types.ARCHIVE_TASK_TYPES == (
            task_types.IMAGE_CLASSIFICATION,
            task_types.IMAGE_TEXT_CLASSIFICATION,
        )

    @pytest.mark.parametrize("task_type", task_types.TASK_TYPES)
    def test_payload_size_is_within_batch_transform_limits(self, task_type):
        """Batch Transform caps MaxPayloadInMB at 100."""
        spec = task_types.get_task_spec(task_type)
        assert 0 < spec.inference_max_payload_mb <= 100

    @pytest.mark.parametrize("task_type", task_types.TASK_TYPES)
    def test_default_instance_is_priced(self, task_type):
        """A task defaulting to an unpriced instance is unusable on arrival."""
        spec = task_types.get_task_spec(task_type)
        assert pricing.training_rate(spec.default_instance_type) is not None
        assert pricing.transform_rate(spec.default_instance_type) is not None


class TestCatalog:

    @pytest.mark.parametrize("model", AVAILABLE_MODELS, ids=lambda m: m.model_id)
    def test_every_model_declares_a_known_task(self, model):
        assert model.task_type in task_types.TASK_TYPES

    @pytest.mark.parametrize("model", AVAILABLE_MODELS, ids=lambda m: m.model_id)
    def test_every_model_default_instance_is_priced(self, model):
        assert pricing.training_rate(model.default_instance_type) is not None

    def test_model_ids_are_unique(self):
        ids = [m.model_id for m in AVAILABLE_MODELS]
        assert len(ids) == len(set(ids))

    def test_every_task_has_at_least_one_model(self):
        for task_type in task_types.TASK_TYPES:
            assert models_for_task(task_type), f"no catalog models for {task_type}"

    def test_models_for_task_filters(self):
        image_models = models_for_task(task_types.IMAGE_CLASSIFICATION)
        assert all(m.task_type == task_types.IMAGE_CLASSIFICATION for m in image_models)
        assert "bert-base-uncased" not in {m.model_id for m in image_models}

    def test_text_model_defaults_are_unchanged(self):
        """The catalog refactor must not silently retune existing models."""
        assert MODEL_CATALOG["gpt2-medium"].default_hyperparameters == {
            "epochs": "3",
            "learning_rate": "2e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "per_device_train_batch_size": "8",
            "context_length": "512",
        }
        assert (
            MODEL_CATALOG["electra-tiny"].default_hyperparameters[
                "per_device_train_batch_size"
            ]
            == "32"
        )
        assert (
            MODEL_CATALOG["eurollm-1.7b-instruct"].default_hyperparameters[
                "learning_rate"
            ]
            == "2e-5"
        )


class TestPricing:

    def test_training_and_transform_tables_are_populated(self):
        assert pricing.TRAINING_COST_PER_HOUR
        assert pricing.TRANSFORM_COST_PER_HOUR

    def test_retired_p3_family_is_absent(self):
        """The Price List API returns no on-demand SageMaker rate for ml.p3.*.

        Pricing them let a caller pick an instance no job could provision.
        """
        assert not [i for i in pricing.TRAINING_COST_PER_HOUR if i.startswith("ml.p3.")]

    def test_g5_16xlarge_uses_the_real_rate(self):
        """This was 6.10 against an actual 5.12 — a ~19% overcharge."""
        assert pricing.training_rate("ml.g5.16xlarge") == 5.12

    def test_unpriced_instance_returns_none(self):
        assert pricing.training_rate("ml.nonexistent.xlarge") is None
        assert pricing.transform_rate("ml.nonexistent.xlarge") is None

    def test_cost_is_prorated_by_seconds(self):
        assert pricing.calculate_cost("ml.g6.xlarge", 3600) == pytest.approx(1.127)
        assert pricing.calculate_cost("ml.g6.xlarge", 1800) == pytest.approx(0.5635)

    def test_cost_of_an_unpriced_instance_is_zero(self):
        assert pricing.calculate_cost("ml.nope.xlarge", 3600) == 0.0

    def test_transform_rate_can_differ_from_training(self):
        """One shared map would silently misprice these."""
        assert pricing.training_rate("ml.g6e.24xlarge") != pricing.transform_rate(
            "ml.g6e.24xlarge"
        )

    def test_estimate_max_cost_uses_full_runtime(self):
        assert pricing.estimate_max_cost("ml.g5.xlarge", 86400) == pytest.approx(33.792)

    def test_supported_lists_are_cheapest_first(self):
        instances = pricing.supported_training_instances()
        rates = [pricing.training_rate(i) for i in instances]
        assert rates == sorted(rates)


class TestDlcFamilies:
    """Text and vision must resolve to different containers."""

    def _service(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.delenv("FINE_TUNING_TRAINING_IMAGE_VISION", raising=False)
        monkeypatch.delenv("FINE_TUNING_TRAINING_IMAGE_TEXT", raising=False)
        from apis.app_api.fine_tuning.sagemaker_service import SageMakerService

        return SageMakerService(sagemaker_client=object(), logs_client=object())

    def test_text_keeps_the_validated_container(self, monkeypatch):
        """Every existing model was trained on transformers 4.36.

        Bumping this image would re-baseline all of them at once.
        """
        service = self._service(monkeypatch)
        uri = service.get_huggingface_image_uri(task_types.TEXT_CLASSIFICATION)
        assert "transformers4.36" in uri

    def test_vision_gets_a_modern_container(self, monkeypatch):
        service = self._service(monkeypatch)
        for task_type in task_types.ARCHIVE_TASK_TYPES:
            uri = service.get_huggingface_image_uri(task_type)
            assert "transformers4.56" in uri

    def test_legacy_call_without_a_task_uses_the_text_image(self, monkeypatch):
        service = self._service(monkeypatch)
        assert service.get_huggingface_image_uri() == service.get_huggingface_image_uri(
            task_types.TEXT_CLASSIFICATION
        )

    def test_environment_override_wins(self, monkeypatch):
        """Escape hatch for a retired or region-lagging DLC tag."""
        service = self._service(monkeypatch)
        monkeypatch.setenv(
            "FINE_TUNING_TRAINING_IMAGE_VISION", "1.dkr.ecr.us-west-2.amazonaws.com/x:y"
        )
        assert (
            service.get_huggingface_image_uri(task_types.IMAGE_CLASSIFICATION)
            == "1.dkr.ecr.us-west-2.amazonaws.com/x:y"
        )

    def test_unsupported_region_raises(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "ap-south-1")
        from apis.app_api.fine_tuning.sagemaker_service import SageMakerService

        service = SageMakerService(sagemaker_client=object(), logs_client=object())
        with pytest.raises(ValueError, match="No HuggingFace DLC image"):
            service.get_huggingface_image_uri(task_types.TEXT_CLASSIFICATION)
