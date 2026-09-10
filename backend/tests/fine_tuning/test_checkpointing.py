"""Checkpointing: what makes a restarted training job resume rather than restart.

Two things restart a job: a spot interruption, and SageMaker killing it at
MaxRuntimeInSeconds — which the dollar-quota clamp makes routine for long runs.
Before this existed, `save_strategy="no"` meant the adapter was only written
after `trainer.train()` returned, so either restart produced *nothing* for the
money already spent.
"""

import os

import pytest
from unittest.mock import MagicMock

from apis.app_api.fine_tuning import task_types
from apis.app_api.fine_tuning.sagemaker_scripts import task_common
from apis.app_api.fine_tuning.sagemaker_scripts import train as train_script
from apis.app_api.fine_tuning import sagemaker_service as sm_service


class TestResolveSaveSteps:
    """A fixed save_steps cannot serve both ends of this catalog."""

    def test_vlm_shaped_run_still_checkpoints(self):
        """The trap this function exists for.

        A 34B VLM trains at batch 1 with 16-step accumulation, so a
        90-sample epoch is ~6 optimizer steps. Against a hardcoded
        save_steps=50 the longest, most interruption-exposed job in the
        catalog would never checkpoint at all.
        """
        steps = task_common.resolve_save_steps(
            num_examples=90, batch_size=1, gradient_accumulation_steps=16, epochs=1
        )
        assert steps >= 1
        assert steps < 50

    def test_scales_with_a_long_run(self):
        """A text classifier with thousands of steps must not checkpoint constantly."""
        steps = task_common.resolve_save_steps(
            num_examples=50_000, batch_size=16, gradient_accumulation_steps=1, epochs=3
        )
        # ~9375 optimizer steps / 10 targets
        assert steps > 100

    def test_targets_roughly_ten_checkpoints(self):
        total_steps = 1000
        steps = task_common.resolve_save_steps(
            num_examples=total_steps, batch_size=1, gradient_accumulation_steps=1, epochs=1
        )
        assert 1 <= total_steps // steps <= 20

    def test_never_returns_zero(self):
        """save_steps=0 is rejected by TrainingArguments."""
        assert task_common.resolve_save_steps(1, 64, 64, 1) >= 1

    def test_tolerates_degenerate_inputs(self):
        """Guards against a division-by-zero on an unset hyperparameter."""
        assert task_common.resolve_save_steps(10, 0, 0, 0) >= 1

    def test_accumulation_reduces_the_step_count(self):
        """Accumulation means fewer optimizer steps for the same data."""
        without = task_common.resolve_save_steps(1000, 1, 1, 1)
        with_accum = task_common.resolve_save_steps(1000, 1, 16, 1)
        assert with_accum < without


class TestCheckpointArguments:

    def test_enabled_produces_step_strategy(self):
        args = task_common.checkpoint_arguments(25)
        assert args["save_strategy"] == "steps"
        assert args["save_steps"] == 25

    def test_keeps_only_the_newest(self):
        """Resume needs the latest checkpoint; older ones only grow the mirror."""
        assert task_common.checkpoint_arguments(25)["save_total_limit"] == 1

    def test_kill_switch_restores_the_old_behaviour(self):
        args = task_common.checkpoint_arguments(25, enabled=False)
        assert args == {"save_strategy": "no"}

    def test_zero_steps_disables(self):
        assert task_common.checkpoint_arguments(0)["save_strategy"] == "no"


class TestLatestCheckpoint:

    def test_missing_directory_is_a_fresh_start(self, tmp_path):
        assert task_common.latest_checkpoint(str(tmp_path / "nope")) is None

    def test_empty_directory_is_a_fresh_start(self, tmp_path):
        assert task_common.latest_checkpoint(str(tmp_path)) is None

    def test_an_unreadable_checkpoint_does_not_fail_the_job(self, tmp_path):
        """Losing progress is survivable; failing the whole run is not.

        In the backend venv transformers is absent by design, so this
        exercises the real import-failure path: a directory that *looks* like
        it holds a checkpoint must still yield a quiet fresh start rather than
        an ImportError escaping into the trainer.
        """
        (tmp_path / "checkpoint-10").mkdir()
        assert task_common.latest_checkpoint(str(tmp_path)) is None


class TestCheckpointConfigWiring:
    """The container writing checkpoints is useless if S3 never mirrors them."""

    def _service(self):
        return sm_service.SageMakerService(
            sagemaker_client=MagicMock(), logs_client=MagicMock()
        )

    def _create(self, **overrides):
        service = self._service()
        kwargs = dict(
            job_name="j", hyperparameters={"a": "b"},
            input_s3_uri="s3://b/in", output_s3_uri="s3://b/out",
            instance_type="ml.g6e.xlarge", max_runtime=3600,
            source_dir_s3_uri="s3://b/src.tar.gz",
            task_type=task_types.IMAGE_TEXT_TO_TEXT,
        )
        kwargs.update(overrides)
        service.create_training_job(**kwargs)
        return service._sagemaker.create_training_job.call_args[1]

    def test_checkpoint_config_is_sent(self):
        params = self._create(checkpoint_s3_uri="s3://bucket/checkpoints/u/j")
        assert params["CheckpointConfig"]["S3Uri"] == "s3://bucket/checkpoints/u/j"

    def test_local_path_matches_what_the_trainer_writes(self):
        """SageMaker only mirrors the directory it is told about."""
        params = self._create(checkpoint_s3_uri="s3://bucket/checkpoints/u/j")
        assert params["CheckpointConfig"]["LocalPath"] == task_common.CHECKPOINT_DIR

    def test_omitted_when_no_uri_is_given(self):
        """Absent config must not become an empty one — SageMaker rejects that."""
        assert "CheckpointConfig" not in self._create()


class TestCheckpointS3Uri:

    def _s3(self):
        from apis.app_api.fine_tuning.s3_service import FineTuningS3Service

        return FineTuningS3Service(s3_client=MagicMock(), bucket_name="ft-bucket")

    def test_is_scoped_per_user_and_job(self):
        uri = self._s3().get_checkpoint_s3_uri("user-1", "job-9")
        assert uri == "s3://ft-bucket/checkpoints/user-1/job-9"

    def test_does_not_collide_with_the_output_prefix(self):
        """model.tar.gz lands under output/; a live mirror there is ambiguous."""
        s3 = self._s3()
        assert not s3.get_checkpoint_s3_uri("u", "j").startswith(
            s3.get_output_s3_uri("u", "j")
        )


class TestCheckpointingHyperparameter:

    def test_defaults_on(self):
        args = train_script.parse_args(["--model_name_or_path", "x"])
        assert args.checkpointing is True

    def test_can_be_disabled(self):
        args = train_script.parse_args(
            ["--model_name_or_path", "x", "--checkpointing", "false"]
        )
        assert args.checkpointing is False
