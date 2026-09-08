"""Shared helpers for DepositSplit direct-mode tests.

Direct mode (genlayer-test) runs the contract in-memory with mocked web
requests and LLM responses, so every scenario — clean evidence, damage,
unreachable URLs, malformed model output, validator disagreement — is
reproducible in milliseconds without a running Studio.
"""

import json

# Evidence URLs used across tests. The contract re-fetches these inside the
# non-deterministic block; mocks below make them resolvable in direct mode.
MOVE_IN_URL = "https://evidence.example/case-1/move-in.html"
MOVE_OUT_URL = "https://evidence.example/case-1/move-out.html"

MOVE_IN_BODY = "<html><body>Move-in report: unit in excellent condition. All fixtures intact. Walls clean, no marks. Flooring undamaged.</body></html>"
MOVE_OUT_BODY = "<html><body>Move-out report: unit matches the move-in inventory. No marks, stains, or breakages observed.</body></html>"

# LLM prompt pattern: the contract's assessor prompt always starts with this
# sentence, so the regex reliably matches only the assessment call.
ASSESSOR_PROMPT_PATTERN = r"You are an independent evidence assessor.*"

DEPOSIT_AMOUNT = 100_000
MAX_DEDUCTION_BPS = 10_000


def clean_assessment() -> dict:
    return {"damage_class": "NO_DAMAGE", "cost_band_bps": 0, "evidence_ok": True}


def register_case_mocks(vm, assessment: dict) -> None:
    """Register web + LLM mocks for a fully resolvable evidence case."""
    vm.mock_web(
        r".*move-in\.html$",
        {"status": 200, "body": MOVE_IN_BODY},
    )
    vm.mock_web(
        r".*move-out\.html$",
        {"status": 200, "body": MOVE_OUT_BODY},
    )
    vm.mock_llm(ASSESSOR_PROMPT_PATTERN, json.dumps(assessment))


def create_case(contract) -> int:
    """Create a standard 100,000-unit deposit case and return its id.

    Set direct_vm.sender before calling to control who creates the case.
    """
    return contract.create_case(
        tenant="Tenant Alice",
        landlord="Landlord Bob",
        inventory_hash="0xinv123",
        move_in_urls=[MOVE_IN_URL],
        move_out_urls=[MOVE_OUT_URL],
        deposit_amount=DEPOSIT_AMOUNT,
        max_deduction_bps=MAX_DEDUCTION_BPS,
    )
