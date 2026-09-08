"""Unit tests for DepositSplit's pure deterministic logic.

These functions are the deterministic half of the contract: normalization
of model output, the semantic equivalence principle, and the settlement
calculation. They are tested directly (no mocks, no consensus) because
money math and fail-closed rules must be correct in isolation.

NOTE: the contract module imports `genlayer`, and the GenLayer SDK only
permits one contract class per process. So we deploy via direct_deploy
(which loads the contract into sys.modules['_contract_deposit_split']) and
grab the helpers from that already-loaded module instead of re-importing.
"""

import sys

import pytest

from tests.direct.conftest import DEPOSIT_AMOUNT, MAX_DEDUCTION_BPS

_contract_module = None


def load_helpers(direct_deploy):
    """Load the contract module after the GenLayer SDK is available."""
    global _contract_module
    if _contract_module is None:
        # Ensure the SDK is loaded by deploying the contract in-memory first.
        direct_deploy("contracts/deposit_split.py")
        _contract_module = sys.modules["_contract_deposit_split"]
    return _contract_module


# ---------------------------------------------------------------------------
# _normalize_assessment — strict schema validation + fail-closed mapping
# ---------------------------------------------------------------------------


def test_normalize_keeps_valid_minor_damage(direct_deploy):
    h = load_helpers(direct_deploy)
    result = h._normalize_assessment(
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True}
    )
    assert result == {
        "damage_class": "MINOR_DAMAGE",
        "cost_band_bps": 1500,
        "evidence_ok": True,
    }


def test_normalize_forces_insufficient_when_evidence_not_ok(direct_deploy):
    h = load_helpers(direct_deploy)
    result = h._normalize_assessment(
        {"damage_class": "MAJOR_DAMAGE", "cost_band_bps": 9000, "evidence_ok": False}
    )
    assert result == {
        "damage_class": "INSUFFICIENT_EVIDENCE",
        "cost_band_bps": 0,
        "evidence_ok": False,
    }


def test_normalize_rejects_unknown_damage_class(direct_deploy):
    h = load_helpers(direct_deploy)
    with pytest.raises(ValueError):
        h._normalize_assessment(
            {"damage_class": "TOTAL_DESTRUCTION", "cost_band_bps": 5000, "evidence_ok": True}
        )


def test_normalize_rejects_out_of_range_band(direct_deploy):
    h = load_helpers(direct_deploy)
    with pytest.raises(ValueError):
        h._normalize_assessment(
            {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 25000, "evidence_ok": True}
        )
    with pytest.raises(ValueError):
        h._normalize_assessment(
            {"damage_class": "MINOR_DAMAGE", "cost_band_bps": -1, "evidence_ok": True}
        )


def test_normalize_rejects_non_bool_evidence_ok(direct_deploy):
    h = load_helpers(direct_deploy)
    with pytest.raises(ValueError):
        h._normalize_assessment(
            {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 100, "evidence_ok": "yes"}
        )


def test_normalize_zeroes_band_for_normal_wear(direct_deploy):
    """Normal wear is never deductible, even if the model suggests a band."""
    h = load_helpers(direct_deploy)
    result = h._normalize_assessment(
        {"damage_class": "NORMAL_WEAR", "cost_band_bps": 3000, "evidence_ok": True}
    )
    assert result["cost_band_bps"] == 0
    assert result["evidence_ok"] is True


def test_normalize_rejects_non_int_band(direct_deploy):
    h = load_helpers(direct_deploy)
    with pytest.raises(ValueError):
        h._normalize_assessment(
            {"damage_class": "MINOR_DAMAGE", "cost_band_bps": "1500", "evidence_ok": True}
        )


# ---------------------------------------------------------------------------
# _assessments_agree — the explicit semantic equivalence principle
# ---------------------------------------------------------------------------


def test_agree_on_identical_assessments(direct_deploy):
    h = load_helpers(direct_deploy)
    a = {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True}
    assert h._assessments_agree(a, dict(a)) is True


def test_agree_within_cost_band_tolerance(direct_deploy):
    h = load_helpers(direct_deploy)
    leader = {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True}
    validator = {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1800, "evidence_ok": True}
    assert h._assessments_agree(leader, validator) is True


def test_disagree_beyond_cost_band_tolerance(direct_deploy):
    h = load_helpers(direct_deploy)
    leader = {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True}
    validator = {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 2500, "evidence_ok": True}
    assert h._assessments_agree(leader, validator) is False


def test_disagree_on_damage_class(direct_deploy):
    h = load_helpers(direct_deploy)
    leader = {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True}
    validator = {"damage_class": "MAJOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True}
    assert h._assessments_agree(leader, validator) is False


def test_disagree_on_evidence_ok(direct_deploy):
    h = load_helpers(direct_deploy)
    leader = {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True}
    validator = {
        "damage_class": "INSUFFICIENT_EVIDENCE",
        "cost_band_bps": 0,
        "evidence_ok": False,
    }
    assert h._assessments_agree(leader, validator) is False


# ---------------------------------------------------------------------------
# _derive_settlement — deterministic money math
# ---------------------------------------------------------------------------


def test_settlement_full_refund_on_no_damage(direct_deploy):
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(DEPOSIT_AMOUNT, "NO_DAMAGE", 0, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "FULL_REFUND"
    assert s["deduction"] == 0
    assert s["tenant_refund"] == DEPOSIT_AMOUNT


def test_settlement_deduct_minor_damage(direct_deploy):
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(DEPOSIT_AMOUNT, "MINOR_DAMAGE", 1500, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "DEDUCT"
    assert s["deduction"] == 15_000
    assert s["tenant_refund"] == 85_000


def test_settlement_forfeit_at_full_band(direct_deploy):
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(DEPOSIT_AMOUNT, "MAJOR_DAMAGE", 10_000, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "FORFEIT"
    assert s["deduction"] == DEPOSIT_AMOUNT
    assert s["tenant_refund"] == 0


def test_settlement_respects_max_deduction_cap(direct_deploy):
    """The case cap bounds the deduction no matter what consensus said."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(DEPOSIT_AMOUNT, "MAJOR_DAMAGE", 8_000, 2_000)
    assert s["outcome"] == "DEDUCT"
    assert s["cost_band_bps"] == 2_000
    assert s["deduction"] == 20_000
    assert s["tenant_refund"] == 80_000


def test_settlement_zero_band_is_full_refund(direct_deploy):
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(DEPOSIT_AMOUNT, "MINOR_DAMAGE", 0, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "FULL_REFUND"


def test_settlement_zero_cap_never_deducts(direct_deploy):
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(DEPOSIT_AMOUNT, "MAJOR_DAMAGE", 10_000, 0)
    assert s["outcome"] == "FULL_REFUND"
    assert s["deduction"] == 0


# ---------------------------------------------------------------------------
# _parse_assessment — defensive parsing of the consensus result
# ---------------------------------------------------------------------------


def test_parse_accepts_json_string_and_dict(direct_deploy):
    h = load_helpers(direct_deploy)
    raw = '{"damage_class": "NO_DAMAGE", "cost_band_bps": 0, "evidence_ok": true}'
    parsed = h._parse_assessment(raw)
    assert parsed["damage_class"] == "NO_DAMAGE"
    assert h._parse_assessment(parsed) == parsed


def test_parse_rejects_garbage(direct_deploy):
    h = load_helpers(direct_deploy)
    assert h._parse_assessment("not json at all") is None
    assert h._parse_assessment(42) is None
    assert h._parse_assessment(None) is None


# ---------------------------------------------------------------------------
# _derive_settlement — property and edge-case tests
# ---------------------------------------------------------------------------


def test_settlement_band_exactly_one_bps(direct_deploy):
    """Minimum non-zero band: 1 bps on 100_000 deposit → 10 deduction."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(100_000, "MINOR_DAMAGE", 1, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "DEDUCT"
    assert s["deduction"] == 10
    assert s["tenant_refund"] == 99_990


def test_settlement_band_at_max_bps_forfeits(direct_deploy):
    """10000 bps = 100% → FORFEIT."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(100_000, "MAJOR_DAMAGE", 10_000, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "FORFEIT"
    assert s["deduction"] == 100_000
    assert s["tenant_refund"] == 0


def test_settlement_small_deposit(direct_deploy):
    """Small deposit (1 unit) with 50% band → 0 deduction (integer division)."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(1, "MINOR_DAMAGE", 5000, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "FULL_REFUND"
    assert s["deduction"] == 0


def test_settlement_large_deposit(direct_deploy):
    """Large deposit scales correctly."""
    h = load_helpers(direct_deploy)
    deposit = 10**18  # 1 ETH-scale
    s = h._derive_settlement(deposit, "MAJOR_DAMAGE", 3000, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "DEDUCT"
    assert s["deduction"] == deposit * 3000 // 10_000
    assert s["tenant_refund"] == deposit - s["deduction"]


def test_settlement_cap_at_exactly_one_bps(direct_deploy):
    """Cap of 1 bps: band is 5000 but capped to 1 → minimal deduction."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(100_000, "MAJOR_DAMAGE", 5000, 1)
    assert s["outcome"] == "DEDUCT"
    assert s["cost_band_bps"] == 1
    assert s["deduction"] == 10


def test_settlement_cap_zero_always_full_refund(direct_deploy):
    """Cap of 0: even MAXOR damage at full band → FULL_REFUND."""
    h = load_helpers(direct_deploy)
    for cls in ("MINOR_DAMAGE", "MAJOR_DAMAGE"):
        s = h._derive_settlement(100_000, cls, 10_000, 0)
        assert s["outcome"] == "FULL_REFUND"
        assert s["deduction"] == 0


def test_settlement_no_damage_ignores_band(direct_deploy):
    """NO_DAMAGE: band forced to 0, always FULL_REFUND."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(100_000, "NO_DAMAGE", 9999, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "FULL_REFUND"
    assert s["cost_band_bps"] == 0
    assert s["deduction"] == 0


def test_settlement_normal_wear_ignores_band(direct_deploy):
    """NORMAL_WEAR: band forced to 0, always FULL_REFUND."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(100_000, "NORMAL_WEAR", 8000, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "FULL_REFUND"
    assert s["cost_band_bps"] == 0
    assert s["deduction"] == 0


def test_settlement_insufficient_evidence_never_deducts(direct_deploy):
    """INSUFFICIENT_EVIDENCE: band forced to 0, always FULL_REFUND."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(100_000, "INSUFFICIENT_EVIDENCE", 5000, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "FULL_REFUND"
    assert s["cost_band_bps"] == 0
    assert s["deduction"] == 0


def test_settlement_band_clamped_to_cap(direct_deploy):
    """Band (8000) exceeds cap (3000) → cap wins, DEDUCT at 30%."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(100_000, "MINOR_DAMAGE", 8000, 3000)
    assert s["outcome"] == "DEDUCT"
    assert s["cost_band_bps"] == 3000
    assert s["deduction"] == 30_000
    assert s["tenant_refund"] == 70_000


def test_settlement_negative_band_clamped_to_zero(direct_deploy):
    """Negative band (shouldn't happen but defensive) → clamped to 0."""
    h = load_helpers(direct_deploy)
    s = h._derive_settlement(100_000, "MINOR_DAMAGE", -500, MAX_DEDUCTION_BPS)
    assert s["outcome"] == "FULL_REFUND"
    assert s["deduction"] == 0


def test_settlement_refund_plus_duction_equals_deposit(direct_deploy):
    """Invariant: tenant_refund + deduction == deposit_amount for all combos."""
    h = load_helpers(direct_deploy)
    deposits = [1, 100, 99_999, 100_000, 10**18]
    bands = [0, 1, 1500, 5000, 9999, 10_000]
    caps = [0, 1, 2000, 5000, MAX_DEDUCTION_BPS]
    classes = ["NO_DAMAGE", "NORMAL_WEAR", "MINOR_DAMAGE", "MAJOR_DAMAGE"]

    for deposit in deposits:
        for band in bands:
            for cap in caps:
                for cls in classes:
                    s = h._derive_settlement(deposit, cls, band, cap)
                    assert s["tenant_refund"] + s["deduction"] == deposit, (
                        f"INVARIANT BROKEN: deposit={deposit}, cls={cls}, "
                        f"band={band}, cap={cap} → refund={s['tenant_refund']}, "
                        f"deduction={s['deduction']}"
                    )
