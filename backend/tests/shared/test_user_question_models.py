"""Normalization and answer parsing for `ask_user_question`.

Two asymmetric contracts meet in this module, and the asymmetry is the point.

**Questions come from the model**, so they are validated strictly: a payload
that cannot be drawn is rejected at the tool boundary, where the model still
has a turn left to fix it. Emitting an unrenderable prompt instead would pause
the turn behind a picker the SPA cannot draw — a hang with no affordance.

**Answers come from the client**, so they are parsed leniently: whatever shape
arrives, the turn resumes. A strict parser here would break the user's only
route out of a paused turn.
"""

from __future__ import annotations

import pytest

from apis.shared.user_questions.models import (
    MAX_OPTIONS,
    MAX_QUESTIONS,
    UserQuestion,
    UserQuestionError,
    decode_questions,
    encode_questions,
    format_answers,
    normalize_questions,
    parse_answers,
)


def _question(header: str = "Scope", n_options: int = 2) -> dict:
    return {
        "header": header,
        "question": f"What {header.lower()}?",
        "options": [{"label": f"opt{i}"} for i in range(n_options)],
    }


class TestNormalizeQuestions:
    """Req: the model's payload is untrusted; only renderable prompts pass."""

    def test_minimal_valid_question_survives(self):
        [question] = normalize_questions([_question()])
        assert question.header == "Scope"
        assert [o.label for o in question.options] == ["opt0", "opt1"]
        assert question.multi_select is False

    def test_multi_select_accepted_in_either_casing(self):
        camel = normalize_questions([{**_question(), "multiSelect": True}])
        snake = normalize_questions([{**_question(), "multi_select": True}])
        assert camel[0].multi_select is True
        assert snake[0].multi_select is True

    def test_bare_string_options_are_accepted(self):
        """Models reach for a plain list of strings; render it rather than fail."""
        [question] = normalize_questions(
            [{"header": "Fmt", "question": "Which?", "options": ["json", "yaml"]}]
        )
        assert [o.label for o in question.options] == ["json", "yaml"]

    def test_question_with_too_few_options_is_dropped(self):
        with pytest.raises(UserQuestionError):
            normalize_questions([{**_question(), "options": [{"label": "only"}]}])

    def test_duplicate_option_labels_are_dropped(self):
        """Answers correlate by label, so two identical choices cannot coexist."""
        with pytest.raises(UserQuestionError):
            normalize_questions(
                [
                    {
                        "header": "Fmt",
                        "question": "Which?",
                        "options": [{"label": "json"}, {"label": "JSON"}],
                    }
                ]
            )

    def test_counts_are_clamped_not_rejected(self):
        raw = [_question(header=f"H{i}", n_options=MAX_OPTIONS + 3) for i in range(9)]
        questions = normalize_questions(raw)
        assert len(questions) == MAX_QUESTIONS
        assert all(len(q.options) == MAX_OPTIONS for q in questions)

    def test_missing_header_gets_a_positional_fallback(self):
        [question] = normalize_questions(
            [{"question": "Which?", "options": ["a", "b"]}]
        )
        assert question.header == "Q1"

    def test_colliding_headers_are_disambiguated(self):
        """Header is the answer key — a collision would silently drop an answer."""
        questions = normalize_questions([_question("Scope"), _question("Scope")])
        headers = [q.header for q in questions]
        assert len(set(headers)) == 2

    @pytest.mark.parametrize("raw", [None, [], "questions", [{}], [{"question": ""}]])
    def test_unrenderable_payloads_raise(self, raw):
        with pytest.raises(UserQuestionError):
            normalize_questions(raw)


class TestParseAnswers:
    """Req: any client payload resumes the turn; nothing here may raise."""

    @pytest.fixture
    def questions(self) -> list[UserQuestion]:
        return normalize_questions([_question("Scope"), _question("Depth")])

    def test_canonical_keyed_payload(self, questions):
        answers = parse_answers(
            questions,
            {"answers": {"Scope": {"selected": ["opt0"]}, "Depth": {"selected": ["opt1"]}}},
        )
        assert [a.selected for a in answers] == [["opt0"], ["opt1"]]
        assert not any(a.skipped for a in answers)

    def test_header_matching_is_case_insensitive(self, questions):
        answers = parse_answers(questions, {"answers": {"scope": ["opt0"]}})
        assert answers[0].selected == ["opt0"]

    def test_positional_payload(self, questions):
        answers = parse_answers(questions, {"answers": [["opt0"], ["opt1"]]})
        assert [a.selected for a in answers] == [["opt0"], ["opt1"]]

    def test_free_text_other(self, questions):
        answers = parse_answers(
            questions, {"answers": {"Scope": {"text": "something else"}}}
        )
        assert answers[0].text == "something else"
        assert answers[0].skipped is False

    def test_explicit_skip(self, questions):
        answers = parse_answers(questions, {"skipped": True})
        assert all(a.skipped for a in answers)

    def test_unanswered_question_is_skipped_not_dropped(self, questions):
        answers = parse_answers(questions, {"answers": {"Scope": ["opt0"]}})
        assert len(answers) == 2
        assert answers[1].skipped is True

    def test_bare_string_is_attributed_to_the_first_question(self, questions):
        answers = parse_answers(questions, "just do the simple one")
        assert answers[0].selected == ["just do the simple one"]
        assert answers[1].skipped is True

    @pytest.mark.parametrize(
        "response", [None, 42, [], {}, {"answers": 7}, {"answers": None}, object()]
    )
    def test_junk_degrades_to_skipped_and_never_raises(self, questions, response):
        answers = parse_answers(questions, response)
        assert len(answers) == 2
        assert all(a.skipped for a in answers)


class TestFormatAnswers:
    """Req: the only part of this feature that re-enters the prompt stays small."""

    def test_selections_render_one_line_each(self):
        questions = normalize_questions([_question("Scope"), _question("Depth")])
        text = format_answers(
            parse_answers(
                questions,
                {"answers": {"Scope": ["opt0", "opt1"], "Depth": {"text": "brief"}}},
            )
        )
        assert "- Scope: opt0, opt1" in text
        assert '- Depth: "brief"' in text
        # The option catalog the model already wrote is not echoed back — this
        # string is paid for on every subsequent turn of the conversation.
        assert text.count("\n") == 2

    def test_all_skipped_tells_the_model_to_proceed(self):
        questions = normalize_questions([_question("Scope")])
        text = format_answers(parse_answers(questions, {"skipped": True}))
        assert "skipped" in text
        assert "do not ask again" in text


class TestPersistenceRoundTrip:
    def test_encode_decode_preserves_the_rendered_shape(self):
        questions = normalize_questions(
            [{**_question("Scope"), "multiSelect": True}]
        )
        decoded = decode_questions(encode_questions(questions))
        assert decoded[0]["header"] == "Scope"
        assert decoded[0]["multiSelect"] is True
        # Round-trips back through the model the SSE event uses.
        assert UserQuestion.model_validate(decoded[0]).multi_select is True

    @pytest.mark.parametrize("encoded", [None, "", "not json", "{}"])
    def test_undecodable_payload_yields_no_questions(self, encoded):
        assert decode_questions(encoded) == []


class TestEscapeHatchOptionsAreStripped:
    """Req: the picker owns Other/Skip; a model-supplied lookalike is dropped.

    Measured against real models, the tool description alone does not hold —
    Haiku 4.5 added an "Other" option on roughly half of sampled turns. A
    supplied "Other" is worse than a duplicate: selecting it records the bare
    string with no free text, so the model learns nothing from the answer.
    """

    @pytest.mark.parametrize(
        "label",
        ["Other", "other", "Other (please specify)", "Skip", "None of the above",
         "Something else", "No preference", "Not sure", "You decide"],
    )
    def test_escape_hatch_labels_are_dropped(self, label):
        [question] = normalize_questions(
            [
                {
                    "header": "Fmt",
                    "question": "Which format?",
                    "options": ["json", "yaml", "toml", label],
                }
            ]
        )
        assert [o.label for o in question.options] == ["json", "yaml", "toml"]

    def test_real_options_named_like_a_hatch_are_kept(self):
        """Only exact matches are dropped — "No preference shown" is a real
        answer to a real question and must survive."""
        [question] = normalize_questions(
            [
                {
                    "header": "Sort",
                    "question": "Which order?",
                    "options": ["Other repositories first", "No preference shown"],
                }
            ]
        )
        assert len(question.options) == 2

    def test_a_question_padded_down_below_the_minimum_is_dropped(self):
        """The model turned a binary choice into "A or Other". The picker
        already expresses that, so the question is not worth a round trip."""
        with pytest.raises(UserQuestionError):
            normalize_questions(
                [{"header": "Go", "question": "Proceed?", "options": ["Yes", "Other"]}]
            )

    def test_stripping_does_not_starve_a_full_question(self):
        [question] = normalize_questions(
            [
                {
                    "header": "Scope",
                    "question": "How much?",
                    "options": ["Just the API", "Everything", "Skip"],
                }
            ]
        )
        assert [o.label for o in question.options] == ["Just the API", "Everything"]


class TestHeaderTrimming:
    """Req: the chip is a label the user reads — never a severed word.

    Found in the browser: the model wrote "Dashboard Purpose" and a hard
    12-char slice rendered "DASHBOARD PU", which reads as a rendering bug.
    """

    def test_long_header_trims_on_a_word_boundary(self):
        [q] = normalize_questions(
            [{"header": "Dashboard Purpose", "question": "Which?", "options": ["a", "b"]}]
        )
        assert q.header == "Dashboard"

    def test_header_within_budget_is_untouched(self):
        [q] = normalize_questions(
            [{"header": "Scope", "question": "Which?", "options": ["a", "b"]}]
        )
        assert q.header == "Scope"

    def test_single_overlong_word_still_falls_back_to_a_hard_cut(self):
        [q] = normalize_questions(
            [{"header": "Internationalization", "question": "Which?", "options": ["a", "b"]}]
        )
        assert q.header == "Internationa"
        assert len(q.header) == 12

    def test_multi_word_header_keeps_as_many_whole_words_as_fit(self):
        [q] = normalize_questions(
            [{"header": "Key Data Points", "question": "Which?", "options": ["a", "b"]}]
        )
        assert q.header == "Key Data"
