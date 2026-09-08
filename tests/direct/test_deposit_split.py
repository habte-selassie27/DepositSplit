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


# ---------------------------------------------------------------------------
# Fail-closed behavior
# ---------------------------------------------------------------------------


def test_unreachable_evidence_fails_closed(direct_vm, direct_deploy, direct_owner):
    """Move-out URL is unreachable -> REVIEW, no deduction, full notional refund."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    # Only the move-in URL is mockable; the move-out fetch will fail.
    direct_vm.mock_web(r".*move-in\.html$", {"status": 200, "body": "move-in ok"})

    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "REVIEW"
    assert s["outcome"] == "REVIEW"
    assert s["deduction"] == 0
    assert s["tenant_refund"] == DEPOSIT_AMOUNT
    assert s["evidence_ok"] is False


def test_insufficient_evidence_verdict_fails_closed(direct_vm, direct_deploy, direct_owner):
    """Evidence reachable but the assessor cannot support a classification."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {
            "damage_class": "INSUFFICIENT_EVIDENCE",
            "cost_band_bps": 0,
            "evidence_ok": False,
        },
    )
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "REVIEW"
    assert s["outcome"] == "REVIEW"
    assert s["damage_class"] == "INSUFFICIENT_EVIDENCE"
    assert s["deduction"] == 0
    assert s["tenant_refund"] == DEPOSIT_AMOUNT


def test_empty_evidence_fails_closed(direct_vm, direct_deploy, direct_owner):
    """URLs resolve but carry no content -> REVIEW via the mechanical gate."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    direct_vm.mock_web(r".*move-in\.html$", {"status": 200, "body": ""})
    direct_vm.mock_web(r".*move-out\.html$", {"status": 200, "body": "   "})

    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "REVIEW"
    assert s["deduction"] == 0
    assert s["evidence_ok"] is False


def test_invalid_model_output_fails_closed(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(direct_vm, {"damage_class": "NO_DAMAGE", "cost_band_bps": 0, "evidence_ok": True})
    direct_vm.clear_mocks()
    direct_vm.mock_web(r".*move-in\.html$", {"status": 200, "body": "ok"})
    direct_vm.mock_web(r".*move-out\.html$", {"status": 200, "body": "ok"})
    direct_vm.mock_llm(r"You are an independent evidence assessor.*", "this is not json")

    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "REVIEW"
    assert s["deduction"] == 0


def test_unsupported_damage_class_fails_closed(direct_vm, direct_deploy, direct_owner):
    """An invented damage class can never reach the settlement math."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "TOTAL_DESTRUCTION", "cost_band_bps": 5000, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "REVIEW"
    assert s["deduction"] == 0
    assert s["evidence_ok"] is False


def test_out_of_range_band_fails_closed(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 25_000, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "REVIEW"
    assert s["deduction"] == 0


# ---------------------------------------------------------------------------
# Replay / state protection
# ---------------------------------------------------------------------------


def test_case_cannot_be_assessed_twice(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(direct_vm, clean_assessment())
    contract.assess_case(case_id)

    with direct_vm.expect_revert("case is not open for assessment"):
        contract.assess_case(case_id)


def test_review_case_cannot_be_reassessed(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    # Unreachable move-out evidence -> REVIEW (terminal).
    direct_vm.mock_web(r".*move-in\.html$", {"status": 200, "body": "move-in ok"})
    contract.assess_case(case_id)
    assert contract.get_settlement(case_id)["status"] == "REVIEW"

    with direct_vm.expect_revert("case is not open for assessment"):
        contract.assess_case(case_id)


def test_assess_unknown_case_reverts(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")

    with direct_vm.expect_revert("case does not exist"):
        contract.assess_case(999)


# ---------------------------------------------------------------------------
# Consensus: validator agreement and conflict handling
# ---------------------------------------------------------------------------


def test_validator_agrees_with_same_evidence(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    assessment = {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True}
    register_case_mocks(direct_vm, assessment)
    contract.assess_case(case_id)

    # Same mocks still active -> an independent validator re-derives the
    # same assessment and agrees.
    assert direct_vm.run_validator() is True


def test_validator_rejects_conflicting_classification(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    # Swap the world: a validator now sees substantial damage instead.
    direct_vm.clear_mocks()
    register_case_mocks(
        direct_vm,
        {"damage_class": "MAJOR_DAMAGE", "cost_band_bps": 8000, "evidence_ok": True},
    )
    assert direct_vm.run_validator() is False


def test_validator_accepts_materially_consistent_band(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    # 300 bps apart — within the 500 bps tolerance, same classification.
    direct_vm.clear_mocks()
    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1800, "evidence_ok": True},
    )
    assert direct_vm.run_validator() is True


def test_validator_rejects_materially_different_band(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    # 1000 bps apart — beyond tolerance even with the same classification.
    direct_vm.clear_mocks()
    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 2500, "evidence_ok": True},
    )
    assert direct_vm.run_validator() is False


def test_validator_rejects_evidence_ok_mismatch(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    # Leader saw usable evidence; validator could not verify anything.
    direct_vm.clear_mocks()
    register_case_mocks(
        direct_vm,
        {
            "damage_class": "INSUFFICIENT_EVIDENCE",
            "cost_band_bps": 0,
            "evidence_ok": False,
        },
    )
    assert direct_vm.run_validator() is False


# ---------------------------------------------------------------------------
# Production-readiness checks
# ---------------------------------------------------------------------------


def test_consensus_closures_are_picklable(direct_vm, direct_deploy, direct_owner):
    """In production the run_nondet closures cross the WASM boundary via
    cloudpickle. Enable pickle checking and run a full happy path."""
    direct_vm.check_pickling = True
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(direct_vm, clean_assessment())
    contract.assess_case(case_id)
    assert contract.get_settlement(case_id)["outcome"] == "FULL_REFUND"


def test_independent_cases_resolve_independently(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")

    case_0 = create_case(contract)
    case_1 = create_case(contract)
    assert contract.get_case_count() == 2

    register_case_mocks(direct_vm, clean_assessment())
    contract.assess_case(case_0)

    direct_vm.clear_mocks()
    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True},
    )
    contract.assess_case(case_1)

    assert contract.get_settlement(case_0)["outcome"] == "FULL_REFUND"
    assert contract.get_settlement(case_1)["outcome"] == "DEDUCT"


# ---------------------------------------------------------------------------
# Additional assessment scenarios
# ---------------------------------------------------------------------------


def test_major_damage_moderate_band_deducts(direct_vm, direct_deploy, direct_owner):
    """Major damage at 60% band with 100% cap → DEDUCT at 60%."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MAJOR_DAMAGE", "cost_band_bps": 6000, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "RESOLVED"
    assert s["outcome"] == "DEDUCT"
    assert s["cost_band_bps"] == 6000
    assert s["deduction"] == 60_000
    assert s["tenant_refund"] == 40_000


def test_no_damage_forces_zero_band(direct_vm, direct_deploy, direct_owner):
    """NO_DAMAGE assessment always results in FULL_REFUND regardless of band."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "NO_DAMAGE", "cost_band_bps": 5000, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["outcome"] == "FULL_REFUND"
    assert s["cost_band_bps"] == 0
    assert s["deduction"] == 0


def test_multiple_urls_evidence_aggregation(direct_vm, direct_deploy, direct_owner):
    """Case with multiple evidence URLs per stage — all must resolve."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")

    move_in_urls = [
        "https://evidence.example/case-multi/move-in-1.html",
        "https://evidence.example/case-multi/move-in-2.html",
    ]
    move_out_urls = [
        "https://evidence.example/case-multi/move-out-1.html",
        "https://evidence.example/case-multi/move-out-2.html",
    ]

    case_id = contract.create_case(
        tenant="Tenant Alice",
        landlord="Landlord Bob",
        inventory_hash="0xinv-multi",
        move_in_urls=move_in_urls,
        move_out_urls=move_out_urls,
        deposit_amount=DEPOSIT_AMOUNT,
        max_deduction_bps=MAX_DEDUCTION_BPS,
    )

    for url in move_in_urls + move_out_urls:
        direct_vm.mock_web(
            rf".*{url.split('/')[-1]}$",
            {"status": 200, "body": "Property in good condition."},
        )
    direct_vm.mock_llm(
        r"You are an independent evidence assessor.*",
        json.dumps({"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1200, "evidence_ok": True}),
    )

    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "RESOLVED"
    assert s["outcome"] == "DEDUCT"
    assert s["deduction"] == 12_000


def test_http_error_status_fails_closed(direct_vm, direct_deploy, direct_owner):
    """Evidence URL returns HTTP 500 → unfetched → REVIEW."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    direct_vm.mock_web(r".*move-in\.html$", {"status": 200, "body": "ok"})
    direct_vm.mock_web(r".*move-out\.html$", {"status": 500, "body": ""})

    contract.assess_case(case_id)

    s = contract.get_settlement(case_id)
    assert s["status"] == "REVIEW"
    assert s["outcome"] == "REVIEW"
    assert s["deduction"] == 0
    assert s["evidence_ok"] is False


def test_boundary_band_exactly_at_tolerance(direct_vm, direct_deploy, direct_owner):
    """Validator band exactly 500 bps from leader → within tolerance, passes."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    # Exactly at tolerance boundary.
    direct_vm.clear_mocks()
    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 2000, "evidence_ok": True},
    )
    assert direct_vm.run_validator() is True


def test_boundary_band_beyond_tolerance(direct_vm, direct_deploy, direct_owner):
    """Validator band 501 bps from leader → just outside tolerance, rejected."""
    direct_vm.sender = direct_owner
    contract = direct_deploy("contracts/deposit_split.py")
    case_id = create_case(contract)

    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 1500, "evidence_ok": True},
    )
    contract.assess_case(case_id)

    direct_vm.clear_mocks()
    register_case_mocks(
        direct_vm,
        {"damage_class": "MINOR_DAMAGE", "cost_band_bps": 2001, "evidence_ok": True},
    )
    assert direct_vm.run_validator() is False
