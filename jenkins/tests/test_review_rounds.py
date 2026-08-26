"""Tests for review_rounds — the settled-issue ledger for round-N incremental
review (rage DESIGN §1.4.8). These pure functions were under-covered (21%);
this brings carry_verification / split_flagged_for_verification / is_settled /
indices / _by_index to full coverage.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "rage-review"))
import review_rounds as rr


def test_is_settled():
    assert rr.is_settled({"verification": "addressed"}) is True
    assert rr.is_settled({"verification": "obsolete"}) is True
    assert rr.is_settled({"verification": "not_addressed"}) is False
    assert rr.is_settled({"verification": "unclear"}) is False
    assert rr.is_settled({}) is False
    assert rr.is_settled(None) is False
    assert rr.is_settled("not-a-dict") is False


def test_carry_verification_copies_settled():
    prev = [
        {"index": 1, "verification": "addressed", "verification_rationale": "改好了",
         "verified_at_sha": "sha1", "marked_resolved_at": 1},
    ]
    new = [{"index": 1}, {"index": 2, "verification": "not_addressed"}]
    out = rr.carry_verification(new, prev)
    # #1 settled -> carried over all verdict fields
    assert out[0]["verification"] == "addressed"
    assert out[0]["verification_rationale"] == "改好了"
    assert out[0]["verified_at_sha"] == "sha1"
    assert out[0]["marked_resolved_at"] == 1
    # #2 unsettled -> NOT carried (stays as-is, no overwrite)
    assert out[1]["verification"] == "not_addressed"


def test_carry_verification_ignores_unsettled_prev():
    prev = [{"index": 1, "verification": "unclear"}]   # unsettled -> do not carry
    new = [{"index": 1}]
    out = rr.carry_verification(new, prev)
    assert "verification" not in out[0]


def test_carry_verification_empty_and_non_dict():
    assert rr.carry_verification([], []) == []
    assert rr.carry_verification(["junk", {"index": 1}], []) == ["junk", {"index": 1}]


def test_split_flagged_for_verification():
    flagged = [
        {"index": 3, "verification": "addressed"},     # settled -> carried
        {"index": 1},                                   # no verdict -> to_verify
        {"index": 2, "verification": "not_addressed"},  # unsettled -> to_verify
    ]
    to_verify, carried = rr.split_flagged_for_verification(flagged)
    assert rr.indices(to_verify) == [1, 2]
    assert rr.indices(carried) == [3]


def test_split_skips_non_dict_and_orders_by_index():
    to_verify, carried = rr.split_flagged_for_verification(
        ["junk", {"index": 9, "verification": "obsolete"}, {"index": 2}])
    assert rr.indices(to_verify) == [2]
    assert rr.indices(carried) == [9]


def test_indices_skips_missing_index():
    assert rr.indices([{"index": 1}, {}, {"index": 3}, "x"]) == [1, 3]
    assert rr.indices(None) == []
