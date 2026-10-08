"""The maintenance verifier and op application (Shared Projects 2.6, §4.6 steps 4–5).

Pure functions, no AWS: what a model's plan must satisfy before a reviewer
sees it, and how approved ops land on a file that may have moved on.
"""

from __future__ import annotations

from datetime import date

import pytest

from apis.shared.memory.format import Item
from apis.shared.memory.maintenance.ops import apply_ops
from apis.shared.memory.maintenance.verify import PlannedChange, dates_in, verify_plan
from apis.shared.memory.models import ItemProvenance

TODAY = date(2026, 10, 7)

ITEMS = (
    Item("Batch Canvas enrollment calls in groups of 50.", "aaaaaaaa"),
    Item("Canvas enrollment calls go in batches of 50.", "bbbbbbbb"),
    Item("Term codes are YYYYTT because the SIS requires it (decided 2026-03).", "cccccccc"),
    Item("Kickoff meeting with Priya on 2026-09-14.", "dddddddd"),
    Item("Owner of the SIS sync: Marcus.", "eeeeeeee"),
    Item("Owner of the SIS sync: Priya.", "ffffffff"),
)


def verify(*changes, pinned=(), provenance=None):
    return verify_plan(list(changes), ITEMS, pinned=pinned, provenance=provenance, today=TODAY)


def merge(*ids, text):
    return PlannedChange(type="merge", ids=ids, text=text, why="Same fact")


def codes(report):
    return [d.code for d in report.dropped]


class TestMerge:
    def test_a_faithful_merge_keeps_the_first_items_anchor(self):
        ops, report = verify(merge(1, 2, text="Batch Canvas enrollment calls in groups of 50."))
        assert (report.planned, report.kept, codes(report)) == (1, 1, [])
        [op] = ops
        assert op.keep == "aaaaaaaa" and op.removed == ["bbbbbbbb"]
        assert [s.text for s in op.sources] == [ITEMS[0].text, ITEMS[1].text]

    @pytest.mark.parametrize(
        "text, code",
        [
            ("Batch Canvas enrollment calls in groups of 100.", "new_number"),
            ("Batch Canvas enrollment calls in small groups.", "lost_number"),
            ("Batch Canvas enrollment calls in groups of 50, per Marcus.", "new_name"),
            ("Batch enrollment calls in groups of 50.", "lost_name"),
            ("Batch Canvas enrollment calls in groups of 50; see [[canvas-limits]].", "new_link"),
            ("Batch Canvas enrollment calls in groups of 50 " + "and that is the rule " * 10, "grew"),
            ("- Batch Canvas enrollment calls in groups of 50. <!-- e:zzzzzzzz -->", "invalid_text"),
        ],
    )
    def test_a_merge_may_not_add_or_lose_facts(self, text, code):
        ops, report = verify(merge(1, 2, text=text))
        assert ops == [] and codes(report) == [code]

    def test_a_reason_kept_without_its_because_still_counts(self):
        items = (
            Item("Batch Canvas enrollment calls in groups of 50; larger batches hit the rate limit.", "aaaaaaaa"),
            Item("Canvas enrollment calls go in batches of 50, because larger batches hit the rate limit.", "bbbbbbbb"),
        )
        ops, report = verify_plan(
            [merge(1, 2, text=items[0].text)], items, today=TODAY,
        )
        assert len(ops) == 1 and codes(report) == []

    def test_a_merge_without_its_text_is_dropped(self):
        ops, report = verify(PlannedChange(type="merge", ids=(1, 2), text=None))
        assert ops == [] and codes(report) == ["missing_text"]

    @pytest.mark.parametrize("why, kept", [
        ("Item 6 updates item 4 with the new owner.", ""),
        ("Items [1] and 2 repeat each other.", ""),
        ("See #3.", ""),
        ("The owner changed from Marcus to Priya.", "The owner changed from Marcus to Priya."),
    ])
    def test_a_reason_citing_item_numbers_is_cleared(self, why, kept):
        [op], _ = verify(PlannedChange(type="supersede", ids=(5, 6), why=why))
        assert op.why == kept

    def test_a_reason_survives_a_merge(self):
        text = "Term codes are YYYYTT for the SIS (decided 2026-03), and Canvas enrollment calls batch in groups of 50."
        ops, report = verify(merge(3, 1, text=text))
        assert ops == [] and codes(report) == ["lost_rationale"]

    def test_two_pinned_items_cannot_merge_and_one_keeps_its_anchor(self):
        text = "Batch Canvas enrollment calls in groups of 50."
        assert codes(verify(merge(1, 2, text=text), pinned=["aaaaaaaa", "bbbbbbbb"])[1]) == ["pinned"]
        [op], _ = verify(merge(1, 2, text=text), pinned=["bbbbbbbb"])
        assert op.keep == "bbbbbbbb" and op.removed == ["aaaaaaaa"]


class TestShapeAndIdentity:
    @pytest.mark.parametrize(
        "change, code",
        [
            (PlannedChange(type="rewrite", ids=(1,)), "unknown_type"),
            (merge(1, text="x"), "bad_shape"),
            (merge(1, 1, text="x"), "bad_shape"),
            (PlannedChange(type="supersede", ids=(5,)), "bad_shape"),
            (PlannedChange(type="prune", ids=(4, 5), reason="expired"), "bad_shape"),
            (merge(1, 99, text="x"), "unknown_item"),
            (merge(0, 1, text="x"), "unknown_item"),
        ],
    )
    def test_malformed_changes_are_dropped(self, change, code):
        ops, report = verify(change)
        assert ops == [] and codes(report) == [code]

    def test_an_item_is_used_by_one_change_at_most(self):
        ops, report = verify(
            merge(1, 2, text="Batch Canvas enrollment calls in groups of 50."),
            PlannedChange(type="supersede", ids=(2, 1)),
        )
        assert len(ops) == 1 and codes(report) == ["overlap"]


class TestSupersede:
    def test_the_newer_item_replaces_the_older(self):
        prov = {
            "eeeeeeee": ItemProvenance(added_at="2026-01-01T00:00:00Z"),
            "ffffffff": ItemProvenance(added_at="2026-06-01T00:00:00Z"),
        }
        [op], _ = verify(PlannedChange(type="supersede", ids=(5, 6)), provenance=prov)
        assert op.removed == ["eeeeeeee"]
        ops, report = verify(PlannedChange(type="supersede", ids=(6, 5)), provenance=prov)
        assert ops == [] and codes(report) == ["not_newer"]

    def test_without_provenance_it_is_allowed_and_a_pin_still_protects(self):
        assert len(verify(PlannedChange(type="supersede", ids=(5, 6)))[0]) == 1
        assert codes(verify(PlannedChange(type="supersede", ids=(5, 6)), pinned=["eeeeeeee"])[1]) == ["pinned"]


class TestPrune:
    def test_only_an_item_whose_dates_have_passed(self):
        assert len(verify(PlannedChange(type="prune", ids=(4,), reason="expired"))[0]) == 1
        assert codes(verify(PlannedChange(type="prune", ids=(1,), reason="expired"))[1]) == ["not_expired"]
        assert codes(verify(PlannedChange(type="prune", ids=(4,), reason="stale"))[1]) == ["bad_reason"]
        assert codes(verify(PlannedChange(type="prune", ids=(4,), reason="expired"), pinned=["dddddddd"])[1]) == ["pinned"]

    def test_dates_in_text(self):
        assert dates_in("on 2026-09-14 and March 3, 2027") == [date(2026, 9, 14), date(2027, 3, 3)]
        assert dates_in("decided 2026-03") == [date(2026, 3, 31)]
        assert dates_in("the 5 October 2026 review, due Nov 2026") == [date(2026, 10, 5), date(2026, 11, 30)]
        assert dates_in("batches of 50") == []


class TestApply:
    def ops(self):
        ops, _ = verify(
            merge(1, 2, text="Batch Canvas enrollment calls in groups of 50."),
            PlannedChange(type="prune", ids=(4,), reason="expired"),
            PlannedChange(type="supersede", ids=(5, 6)),
        )
        return ops

    def test_all_ops_in_place_with_reasons_for_the_archive(self):
        result = apply_ops(ITEMS, self.ops())
        assert [i.anchor for i in result.items] == ["aaaaaaaa", "cccccccc", "ffffffff"]
        assert result.applied == [0, 1, 2] and result.skipped == []
        assert result.archive == {
            "bbbbbbbb": ("merged", "aaaaaaaa"),
            "dddddddd": ("pruned", None),
            "eeeeeeee": ("superseded", "ffffffff"),
        }

    def test_selected_ops_only(self):
        result = apply_ops(ITEMS, self.ops(), selected=[1])
        assert result.applied == [1] and [i.anchor for i in result.items] == [
            "aaaaaaaa", "bbbbbbbb", "cccccccc", "eeeeeeee", "ffffffff",
        ]
        with pytest.raises(IndexError):
            apply_ops(ITEMS, self.ops(), selected=[3])

    def test_an_op_whose_items_changed_or_got_pinned_is_skipped(self):
        edited = tuple(Item("Batch calls in groups of 25.", i.anchor) if i.anchor == "bbbbbbbb" else i for i in ITEMS)
        result = apply_ops(edited, self.ops())
        assert result.applied == [1, 2] and result.skipped == [0]
        assert "bbbbbbbb" in [i.anchor for i in result.items]

        pinned = apply_ops(ITEMS, self.ops(), pinned=["dddddddd"])
        assert pinned.skipped == [1] and "dddddddd" in [i.anchor for i in pinned.items]
