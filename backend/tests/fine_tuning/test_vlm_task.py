"""Unit tests for the generative vision-language task module.

``task_image_text_to_text`` runs inside the SageMaker DLC, where torch,
transformers and peft exist.  They do not exist in the backend venv, so these
cover the parts that must hold *before* a GPU is billed: the module imports
cleanly, the chat rendering is faithful to the checkpoint's own template, the
label-masking arithmetic cannot produce an all-masked row, and the adapter
sidecar records what inference needs to rebuild the pair.
"""

import json

import pytest
from unittest.mock import MagicMock

from apis.app_api.fine_tuning import task_types
from apis.app_api.fine_tuning.sagemaker_scripts import task_image_text_to_text as vlm
from apis.app_api.fine_tuning.sagemaker_scripts import train as train_script

SPEC = task_types.get_task_spec(task_types.IMAGE_TEXT_TO_TEXT)


def _processor(chat_template="{{ messages }}"):
    """A processor stub exposing only what render_chat touches."""
    processor = MagicMock()
    processor.chat_template = chat_template
    processor.apply_chat_template.side_effect = (
        lambda messages, tokenize, add_generation_prompt: json.dumps(
            {"messages": messages, "generation_prompt": add_generation_prompt}
        )
    )
    return processor


class TestImportContract:
    """The module must load without the ML stack, like its siblings."""

    def test_imports_without_torch(self):
        import sys

        assert "torch" not in sys.modules
        assert vlm.LABEL_IGNORE_INDEX == -100

    def test_registered_in_both_dispatchers(self):
        from apis.app_api.fine_tuning.sagemaker_scripts import inference

        assert train_script.TASK_MODULES[task_types.IMAGE_TEXT_TO_TEXT] is vlm
        assert inference.TASK_MODULES[task_types.IMAGE_TEXT_TO_TEXT] is vlm


class TestBuildMessages:

    def test_prompt_only_has_no_assistant_turn(self):
        messages = vlm.build_messages("What is this?")
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    def test_image_precedes_text_in_the_user_turn(self):
        """Every catalog template expects the image first."""
        content = vlm.build_messages("caption it")[0]["content"]
        assert [part["type"] for part in content] == ["image", "text"]

    def test_response_becomes_the_assistant_turn(self):
        messages = vlm.build_messages("q", "a")
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"][0]["text"] == "a"

    def test_non_string_fields_are_coerced(self):
        """A CSV column of digits loads as int64 and would break the template."""
        messages = vlm.build_messages(42, 7)
        assert messages[0]["content"][1]["text"] == "42"
        assert messages[1]["content"][0]["text"] == "7"

    def test_empty_response_still_produces_a_turn(self):
        """An empty string is a real target; only None means prompt-only."""
        assert len(vlm.build_messages("q", "")) == 2


class TestRenderChat:

    def test_uses_the_processor_template(self):
        processor = _processor()
        rendered = vlm.render_chat(
            processor, vlm.build_messages("q", "a"), add_generation_prompt=False
        )
        assert json.loads(rendered)["generation_prompt"] is False
        processor.apply_chat_template.assert_called_once()

    def test_generation_prompt_flag_is_passed_through(self):
        rendered = vlm.render_chat(
            _processor(), vlm.build_messages("q"), add_generation_prompt=True
        )
        assert json.loads(rendered)["generation_prompt"] is True

    def test_template_on_the_tokenizer_is_accepted(self):
        """Some processors carry the template on their tokenizer instead."""
        processor = _processor(chat_template=None)
        processor.tokenizer.chat_template = "{{ messages }}"
        assert vlm.render_chat(processor, vlm.build_messages("q"), False)

    def test_missing_template_raises_rather_than_inventing_a_format(self):
        """Guessing trains the model on a dialogue shape it has never seen."""
        processor = _processor(chat_template=None)
        processor.tokenizer.chat_template = None
        with pytest.raises(ValueError, match="no chat template"):
            vlm.render_chat(processor, vlm.build_messages("q"), False)


class TestPromptMaskLimit:
    """Loss is computed on the response only — but never on nothing."""

    def test_masks_the_whole_prompt_when_the_response_survives(self):
        assert vlm.prompt_mask_limit(10, 40) == 10

    def test_never_masks_the_final_position(self):
        """An all-masked row yields NaN loss and poisons the batch average."""
        assert vlm.prompt_mask_limit(40, 40) == 39
        assert vlm.prompt_mask_limit(100, 40) == 39

    def test_zero_length_prompt_masks_nothing(self):
        assert vlm.prompt_mask_limit(0, 40) == 0

    def test_never_returns_a_negative_limit(self):
        """A degenerate single-token row must not produce a negative slice."""
        assert vlm.prompt_mask_limit(0, 1) == 0
        assert vlm.prompt_mask_limit(5, 1) == 0


class TestResolveImageTokenIds:

    def test_reads_the_config_attribute(self):
        config = MagicMock(spec=["image_token_index"])
        config.image_token_index = 32000
        assert 32000 in vlm.resolve_image_token_ids(MagicMock(), config)

    def test_reads_the_renamed_attribute(self):
        """transformers renamed this between the versions we run."""
        config = MagicMock(spec=["image_token_id"])
        config.image_token_id = 151655
        assert 151655 in vlm.resolve_image_token_ids(MagicMock(), config)

    def test_resolves_through_the_tokenizer(self):
        processor = MagicMock()
        processor.image_token = "<image>"
        processor.tokenizer.convert_tokens_to_ids.return_value = 128256
        assert 128256 in vlm.resolve_image_token_ids(processor, MagicMock(spec=[]))

    def test_unknown_placeholder_is_not_an_error(self):
        """Pad and prompt masking still apply; a few live placeholders only
        nudge the loss, so an unrecognised architecture must not fail here."""
        processor = MagicMock()
        processor.image_token = None
        assert vlm.resolve_image_token_ids(processor, MagicMock(spec=[])) == set()

    def test_ignores_a_non_integer_attribute(self):
        config = MagicMock(spec=["image_token_index"])
        config.image_token_index = "<image>"
        processor = MagicMock()
        processor.image_token = None
        assert vlm.resolve_image_token_ids(processor, config) == set()


class TestQuantizationConfig:

    def test_disabled_returns_none_without_importing_bitsandbytes(self):
        """bf16 training must not require the quantisation stack at all."""
        assert vlm.build_quantization_config(False) is None


class TestAdapterConfig:
    """Only the adapter is saved; this sidecar names the base it belongs to."""

    def _args(self, **overrides):
        defaults = {
            "model_name_or_path": "llava-hf/llava-v1.6-34b-hf",
            "load_in_4bit": True,
            "max_new_tokens": 256,
        }
        defaults.update(overrides)
        return MagicMock(**defaults)

    def test_records_the_base_model(self, tmp_path):
        vlm.save_adapter_config(str(tmp_path), self._args())
        payload = json.loads(
            (tmp_path / vlm.ADAPTER_CONFIG_FILENAME).read_text()
        )
        assert payload["base_model_id"] == "llava-hf/llava-v1.6-34b-hf"

    def test_records_the_quantisation_mode(self, tmp_path):
        """Loading a bf16-trained adapter over a 4-bit base changes the model."""
        vlm.save_adapter_config(str(tmp_path), self._args(load_in_4bit=False))
        payload = json.loads(
            (tmp_path / vlm.ADAPTER_CONFIG_FILENAME).read_text()
        )
        assert payload["load_in_4bit"] is False

    def test_creates_a_missing_directory(self, tmp_path):
        target = tmp_path / "model"
        vlm.save_adapter_config(str(target), self._args())
        assert (target / vlm.ADAPTER_CONFIG_FILENAME).is_file()


class TestBooleanHyperparameters:
    """SageMaker passes every hyperparameter as a string."""

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", True])
    def test_truthy(self, value):
        assert train_script.str2bool(value) is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", "", False])
    def test_falsy(self, value):
        assert train_script.str2bool(value) is False

    def test_the_string_false_is_not_truthy(self):
        """bool("false") is True — the whole reason this parser exists."""
        assert bool("false") is True
        assert train_script.str2bool("false") is False

    def test_parser_applies_it_to_load_in_4bit(self):
        args = train_script.parse_args(
            ["--model_name_or_path", "x", "--load_in_4bit", "false"]
        )
        assert args.load_in_4bit is False


class TestGenerativeHyperparameterPlumbing:
    """Every default in the registry must reach a real parser argument."""

    @pytest.mark.parametrize("key", sorted(SPEC.default_hyperparameters))
    def test_default_is_a_known_argument(self, key):
        args = train_script.parse_args(["--model_name_or_path", "x"])
        # split_ratio/seed/epochs etc. are shared; the LoRA ones are new.
        assert hasattr(args, key), f"{key} has no --{key} argument"

    def test_defaults_parse_as_their_declared_types(self):
        argv = ["--model_name_or_path", "x", "--task_type", SPEC.task_type]
        for key, value in SPEC.default_hyperparameters.items():
            argv += [f"--{key}", value]
        args = train_script.parse_args(argv)

        assert args.lora_r == 16
        assert args.lora_alpha == 32
        assert args.lora_dropout == pytest.approx(0.05)
        assert args.load_in_4bit is True
        assert args.gradient_accumulation_steps == 8
        assert args.max_new_tokens == 256
        assert args.learning_rate == pytest.approx(1e-4)


class TestCollationCheck:
    """A canary batch, so a bad pairing fails before the GPU bill starts."""

    def test_passes_a_working_collator(self):
        vlm.check_collation(lambda batch: {"ok": True}, [{"a": 1}, {"a": 2}])

    def test_reraises_with_actionable_guidance(self):
        def collator(_batch):
            raise RuntimeError("Image features and image tokens do not match")

        with pytest.raises(ValueError, match="context_length"):
            vlm.check_collation(collator, [{"a": 1}])

    def test_preserves_the_original_error(self):
        def collator(_batch):
            raise RuntimeError("tokens do not match: 2928 vs 1024")

        with pytest.raises(ValueError, match="2928 vs 1024"):
            vlm.check_collation(collator, [{"a": 1}])

    def test_samples_at_most_the_requested_records(self):
        seen = []

        def collator(batch):
            seen.append(len(batch))
            return {}

        vlm.check_collation(collator, [{"a": i} for i in range(50)])
        assert seen == [2]

    def test_shorter_dataset_than_the_sample_size(self):
        """A one-record dataset must not index past the end."""
        vlm.check_collation(lambda batch: {}, [{"a": 1}])

    def test_empty_dataset_is_a_no_op(self):
        vlm.check_collation(lambda batch: 1 / 0, [])
