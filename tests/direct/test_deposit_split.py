"""Direct-mode tests for the DepositSplit contract.

Covers the full acceptance matrix:
- FULL_REFUND (clean evidence, normal wear)
- DEDUCT (supported damage, capped by max_deduction_bps)
- FORFEIT (full-band supported damage)
- fail-closed: unreachable evidence, insufficient evidence, invalid model
  output, out-of-range values -> REVIEW, never a deduction
- replay protection: a case can only be assessed once
- consensus: validator agreement, conflict rejection, cost-band tolerance,
  evidence_ok mismatch rejection
- production safety: closures are picklable (cloudpickle check)
"""

import json

from tests.direct.conftest import (
    DEPOSIT_AMOUNT,
    MAX_DEDUCTION_BPS,
    MOVE_IN_URL,
    MOVE_OUT_URL,
    clean_assessment,
    create_case,
    register_case_mocks,
)


# ---------------------------------------------------------------------------
# Case creation
# ---------------------------------------------------------------------------


def test_create_case_stores_initial_state(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")

    case_id = create_case(contract)

    assert case_id == 0
    assert contract.get_case_count() == 1

    case = contract.get_case(case_id)
    assert case["status"] == "OPEN"
    assert case["tenant"] == "Tenant Alice"
    assert case["landlord"] == "Landlord Bob"
    assert case["deposit_amount"] == DEPOSIT_AMOUNT
    assert case["max_deduction_bps"] == MAX_DEDUCTION_BPS
    assert case["move_in_urls"] == [MOVE_IN_URL]
    assert case["move_out_urls"] == [MOVE_OUT_URL]

    settlement = contract.get_settlement(case_id)
    assert settlement["outcome"] == ""
    assert settlement["deduction"] == 0


def test_create_case_rejects_invalid_input(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")

    with direct_vm.expect_revert("deposit_amount must be positive"):
        contract.create_case(
            "T", "L", "hash", [MOVE_IN_URL], [MOVE_OUT_URL], 0, MAX_DEDUCTION_BPS
        )

    with direct_vm.expect_revert("max_deduction_bps must be between 0 and 10000"):
        contract.create_case(
            "T", "L", "hash", [MOVE_IN_URL], [MOVE_OUT_URL], DEPOSIT_AMOUNT, 15_000
        )

    with direct_vm.expect_revert("1..8 URLs are required"):
        contract.create_case(
            "T", "L", "hash", [MOVE_IN_URL], [], DEPOSIT_AMOUNT, MAX_DEDUCTION_BPS
        )

    with direct_vm.expect_revert("evidence URLs must be http(s) URLs"):
        contract.create_case(
            "T",
            "L",
            "hash",
            ["ftp://not-http.example/inv"],
            [MOVE_OUT_URL],
            DEPOSIT_AMOUNT,
            MAX_DEDUCTION_BPS,
        )

    with direct_vm.expect_revert("tenant and landlord are required"):
        contract.create_case(
            "", "L", "hash", [MOVE_IN_URL], [MOVE_OUT_URL], DEPOSIT_AMOUNT, MAX_DEDUCTION_BPS
        )

    assert contract.get_case_count() == 0


# ---------------------------------------------------------------------------
# Settlement outcomes — caller never picks the verdict
# ---------------------------------------------------------------------------


def test_full_refund_on_clean_evidence(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(direct_vm, clean_assessment())
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "RESOLVED"
    assert s["outcome"] == "FULL_REFUND"
    assert s["damage_class"] == "NO_DAMAGE"
    assert s["deduction"] == 0
    assert s["tenant_refund"] == DEPOSIT_AMOUNT
    assert s["evidence_ok"] is True


def test_normal_wear_forces_full_refund(direct_vm, direct_deploy, direct_owner):
    """Even if the model suggests a band for normal wear, the contract forces 0."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "NORMAL_WEAR", "cost_band_bps": 3000, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["outcome"] == "FULL_REFUND"
    assert s["damage_class"] == "NORMAL_WEAR"
    assert s["cost_band_bps"] == 0
    assert s["deduction"] == 0


def test_deduct_supported_minor_damage(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "RESOLVED"
    assert s["outcome"] == "DEDUCT"
    assert s["cost_band_bps"] == 1500
    assert s["deduction"] == 15_000
    assert s["tenant_refund"] == 85_000


def test_deduction_capped_by_max_deduction_bps(direct_vm, direct_deploy, direct_owner):
    """Consensus says 80% damage, but the case cap limits it to 20%."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = contract.create_case(
        tenant="T",
        landlord="L",
        inventory_hash="hash",
        move_in_urls=[MOVE_IN_URL],
        move_out_urls=[MOVE_OUT_URL],
        deposit_amount=DEPOSIT_AMOUNT,
        max_deduction_bps=2_000,
    )

    register_case_mocks(
        direct_vm,
        {"damage_class": "MAJOR_DAMAGE", "cost_band_bps": 8000, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["outcome"] == "DEDUCT"
    assert s["cost_band_bps"] == 2_000
    assert s["deduction"] == 20_000
    assert s["tenant_refund"] == 80_000


def test_forfeit_on_full_band_supported_damage(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MAJOR_DAMAGE", "cost_band_bps": 10_000, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "RESOLVED"
    assert s["outcome"] == "FORFEIT"
    assert s["deduction"] == DEPOSIT_AMOUNT
    assert s["tenant_refund"] == 0
