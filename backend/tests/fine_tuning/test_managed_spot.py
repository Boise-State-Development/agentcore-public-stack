"""Managed spot training.

Spot trades a large discount for a longer queue and the risk of being
interrupted. It is only safe here because checkpointing landed first: without
it an interrupted job restarts from zero, and a run longer than the mean time
between interruptions almost never completes while being billed for every
attempt.
"""

import pytest
from unittest.mock import MagicMock

from apis.app_api.fine_tuning import pricing, task_types
from apis.app_api.fine_tuning import sagemaker_service as sm_service
from apis.app_api.fine_tuning.sagemaker_scripts import task_common


def _params(**overrides):
    service = sm_service.SageMakerService(
        sagemaker_client=MagicMock(), logs_client=MagicMock()
    )
    kwargs = dict(
        job_name="j", hyperparameters={"a": "b"},
        input_s3_uri="s3://b/in", output_s3_uri="s3://b/out",
        instance_type="ml.g6e.xlarge", max_runtime=3600,
        source_dir_s3_uri="s3://b/src.tar.gz",
        task_type=task_types.IMAGE_TEXT_TO_TEXT,
        checkpoint_s3_uri="s3://b/checkpoints/u/j",
    )
    kwargs.update(overrides)
    service.create_training_job(**kwargs)
    return service._sagemaker.create_training_job.call_args[1]


class TestSpotStoppingCondition:

    def test_off_by_default(self):
        """Opt-in: a researcher who needs a result today pays for certainty."""
        params = _params()
        assert "EnableManagedSpotTraining" not in params
        assert "MaxWaitTimeInSeconds" not in params["StoppingCondition"]

    def test_enables_the_flag(self):
        assert _params(use_spot=True)["EnableManagedSpotTraining"] is True

    def test_max_wait_strictly_exceeds_max_runtime(self):
        """The API rejects MaxWaitTime <= MaxRuntime."""
        params = _params(use_spot=True, max_runtime=3600)
        assert params["StoppingCondition"]["MaxWaitTimeInSeconds"] > 3600

    def test_max_wait_leaves_room_for_the_capacity_queue(self):
        """MaxWaitTime covers waiting AND training.

        Measured on-demand waits for these GPU families ran 28-58 minutes;
        spot draws from the surplus of the same constrained pools, so a wait
        allowance shorter than that fails the job with MaxWaitTimeExceeded
        after it has already queued.
        """
        params = _params(use_spot=True, max_runtime=3600)
        slack = params["StoppingCondition"]["MaxWaitTimeInSeconds"] - 3600
        assert slack >= 3600

    def test_max_runtime_is_unchanged_by_spot(self):
        """Spot must not quietly extend the budget-clamped stopping condition."""
        params = _params(use_spot=True, max_runtime=1234)
        assert params["StoppingCondition"]["MaxRuntimeInSeconds"] == 1234

    def test_spot_still_carries_checkpoint_config(self):
        """The pairing that makes spot survivable."""
        params = _params(use_spot=True)
        assert params["CheckpointConfig"]["LocalPath"] == task_common.CHECKPOINT_DIR


class TestSpotCostAccounting:
    """AWS shrinks BillableTimeInSeconds rather than discounting the rate."""

    def test_on_demand_rate_is_correct_for_spot(self):
        """The documented savings formula is
        (1 - BillableTimeInSeconds / TrainingTimeInSeconds) * 100 — i.e. the
        discount is already inside billable_seconds, so multiplying it by the
        on-demand rate is right and needs no spot branch."""
        full = pricing.calculate_cost("ml.g6e.xlarge", 3600)
        discounted = pricing.calculate_cost("ml.g6e.xlarge", 1200)
        assert discounted == pytest.approx(full / 3)

    def test_multiplies_by_instance_count(self):
        """AWS documents BillableTimeInSeconds as per-instance."""
        one = pricing.calculate_cost("ml.g6e.xlarge", 3600, instance_count=1)
        four = pricing.calculate_cost("ml.g6e.xlarge", 3600, instance_count=4)
        assert four == pytest.approx(one * 4)

    def test_default_instance_count_is_one(self):
        assert pricing.calculate_cost("ml.g6e.xlarge", 3600) == pytest.approx(
            pricing.calculate_cost("ml.g6e.xlarge", 3600, instance_count=1)
        )

    def test_a_zero_count_does_not_zero_the_bill(self):
        """Defensive: a bad count must not silently make GPU time free."""
        assert pricing.calculate_cost("ml.g6e.xlarge", 3600, instance_count=0) > 0


class TestSharedBooleanParsing:
    """app-api and the container must agree, or app-api admits a job the
    trainer then runs with different settings."""

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", True])
    def test_truthy(self, value):
        assert task_types.str2bool(value) is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "", False])
    def test_falsy(self, value):
        assert task_types.str2bool(value) is False

    def test_the_string_False_is_not_truthy(self):
        """SageMaker JSON-parses "false" and passes back Python-style "False"."""
        assert bool("False") is True
        assert task_types.str2bool("False") is False

    def test_train_script_reexports_the_same_function(self):
        from apis.app_api.fine_tuning.sagemaker_scripts import train

        assert train.str2bool is task_types.str2bool
