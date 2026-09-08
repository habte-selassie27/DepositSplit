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
