# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
DepositSplit — Tenancy Deposit Arbiter
======================================

A reusable GenLayer Intelligent Contract primitive for resolving tenancy
deposit disputes from independently verified evidence.

Architecture in one paragraph:
    The caller registers a case with evidence URL references (never a
    verdict). `assess_case()` accepts ONLY a case id. Inside
    `gl.vm.run_nondet`, the leader fetches the evidence from the web,
    evaluates it with an LLM, and produces a strict, normalized JSON
    assessment. Each validator independently re-fetches and re-evaluates
    the same evidence and compares its result with the leader's under an
    explicit semantic equivalence rule (same damage class, same
    evidence_ok, cost band within a fixed tolerance). Only after consensus
    does deterministic contract code derive the settlement. Anything
    ambiguous, unavailable, or malformed fails closed into REVIEW — it can
    never produce an arbitrary deduction.

Security boundaries:
    1. The caller cannot choose the outcome: no parameter of any method
       accepts a damage class, cost band, deduction, or outcome.
    2. External evidence is re-fetched by every validator during consensus
       (gl.nondet.web.render inside the run_nondet block).
    3. Semantic equivalence is enforced by explicit validator code, not by
       byte comparison and not by trusting the leader.
    4. The settlement calculation is deterministic and bounded by
       max_deduction_bps configured at case creation.
    5. Every failure mode (unreachable URL, empty evidence, invalid LLM
       output, out-of-range values, consensus disagreement) either lands
       the case in REVIEW or reverts the transaction — it never authorizes
       a deduction.
"""

import json
from dataclasses import dataclass

from genlayer import *


# --------------------------------------------------------------------------
# Constants (module-level, deterministic)
# --------------------------------------------------------------------------

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUS_REVIEW = "REVIEW"

OUTCOME_FULL_REFUND = "FULL_REFUND"
OUTCOME_DEDUCT = "DEDUCT"
OUTCOME_FORFEIT = "FORFEIT"
OUTCOME_REVIEW = "REVIEW"

DAMAGE_NO_DAMAGE = "NO_DAMAGE"
DAMAGE_NORMAL_WEAR = "NORMAL_WEAR"
DAMAGE_MINOR = "MINOR_DAMAGE"
DAMAGE_MAJOR = "MAJOR_DAMAGE"
DAMAGE_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

ALLOWED_DAMAGE_CLASSES = (
    DAMAGE_NO_DAMAGE,
    DAMAGE_NORMAL_WEAR,
    DAMAGE_MINOR,
    DAMAGE_MAJOR,
    DAMAGE_INSUFFICIENT,
)

# Classes that can justify a deduction at all.
DEDUCTIBLE_DAMAGE_CLASSES = (DAMAGE_MINOR, DAMAGE_MAJOR)

# Basis points: 10000 == 100%.
MAX_BPS = 10000

# How far the validator's cost band may deviate from the leader's while
# still counting as "materially consistent". Damage classification itself
# must match exactly.
COST_BAND_TOLERANCE_BPS = 500

# Bounded evidence input.
MAX_URLS_PER_STAGE = 8
MAX_EVIDENCE_CHARS_PER_URL = 6000

REVIEW_UNPARSABLE = "CONSENSUS_RESULT_UNPARSABLE"
REVIEW_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


# --------------------------------------------------------------------------
# Pure helpers — no gl.* / non-deterministic calls here.
# These are deterministic, unit-testable, and cloudpickle-safe.
# --------------------------------------------------------------------------


def _canonical(assessment: dict) -> str:
    """Canonical JSON serialization used for consensus comparison."""
    return json.dumps(assessment, sort_keys=True)


def _parse_assessment(raw) -> dict | None:
    """Parse a consensus result into a dict, or None if impossible."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _normalize_assessment(parsed: dict) -> dict:
    """
    Validate + normalize a raw LLM assessment into the strict schema.

    Raises ValueError for anything structurally invalid. This function is
    deliberately strict: callers treat ValueError as "insufficient
    evidence" so that malformed model output can never reach settlement.
    """
    damage = parsed.get("damage_class")
    cost_band = parsed.get("cost_band_bps")
    evidence_ok = parsed.get("evidence_ok")

    if not isinstance(damage, str) or damage not in ALLOWED_DAMAGE_CLASSES:
        raise ValueError("invalid damage_class")

    if isinstance(cost_band, bool) or not isinstance(cost_band, int):
        raise ValueError("invalid cost_band_bps")
    if cost_band < 0 or cost_band > MAX_BPS:
        raise ValueError("cost_band_bps out of range")

    if not isinstance(evidence_ok, bool):
        raise ValueError("invalid evidence_ok")

    if not evidence_ok:
        # Fail closed: no verified evidence => no classification, no band.
        return {
            "damage_class": DAMAGE_INSUFFICIENT,
            "cost_band_bps": 0,
            "evidence_ok": False,
        }

    if damage == DAMAGE_INSUFFICIENT:
        evidence_ok = False
        return {
            "damage_class": DAMAGE_INSUFFICIENT,
            "cost_band_bps": 0,
            "evidence_ok": False,
        }

    if damage in (DAMAGE_NO_DAMAGE, DAMAGE_NORMAL_WEAR):
        # Normal wear is never deductible — force the band to zero.
        cost_band = 0

    return {
        "damage_class": damage,
        "cost_band_bps": cost_band,
        "evidence_ok": True,
    }


def _assessments_agree(leader: dict, validator: dict) -> bool:
    """
    The explicit semantic equivalence principle for assessments.

    - damage_class must match exactly (semantic agreement on facts).
    - evidence_ok must match exactly.
    - cost_band_bps must be materially consistent (within tolerance).

    Returns True only when the two independent evaluations describe the
    same reality.
    """
    if leader.get("damage_class") != validator.get("damage_class"):
        return False
    if leader.get("evidence_ok") != validator.get("evidence_ok"):
        return False
    try:
        leader_band = int(leader.get("cost_band_bps"))
        validator_band = int(validator.get("cost_band_bps"))
    except Exception:
        return False
    return abs(leader_band - validator_band) <= COST_BAND_TOLERANCE_BPS


def _derive_settlement(
    deposit_amount: int,
    damage_class: str,
    cost_band_bps: int,
    max_deduction_bps: int,
) -> dict:
    """
    Deterministic settlement calculation. The ONLY place where money
    numbers are produced. Consensus decides the facts; this decides the
    money, strictly from those facts and the case's configured cap.
    """
    if damage_class not in DEDUCTIBLE_DAMAGE_CLASSES:
        cost_band_bps = 0

    # Never exceed the configured maximum deduction, never leave [0, 10000].
    cost_band_bps = max(0, min(int(cost_band_bps), int(max_deduction_bps), MAX_BPS))

    deduction = (int(deposit_amount) * cost_band_bps) // MAX_BPS

    if deduction <= 0:
        outcome = OUTCOME_FULL_REFUND
    elif deduction >= int(deposit_amount):
        outcome = OUTCOME_FORFEIT
        deduction = int(deposit_amount)
    else:
        outcome = OUTCOME_DEDUCT

    return {
        "outcome": outcome,
        "damage_class": damage_class,
        "cost_band_bps": cost_band_bps,
        "deduction": deduction,
        "tenant_refund": int(deposit_amount) - deduction,
    }


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


@allow_storage
@dataclass
class Case:
    tenant: str
    landlord: str
    inventory_hash: str
    created_by: str
    move_in_urls: DynArray[str]
    move_out_urls: DynArray[str]
    deposit_amount: u256
    max_deduction_bps: u256
    status: str
    outcome: str
    damage_class: str
    cost_band_bps: u256
    deduction: u256
    tenant_refund: u256
    evidence_ok: bool
    assessment: str


class DepositSplit(gl.Contract):
    """
    Tenancy deposit arbiter primitive.

    State machine per case:
        OPEN --assess_case() with consensus--> RESOLVED  (settlement derived)
        OPEN --assess_case() w/o evidence  --> REVIEW    (fail-closed)
        OPEN --consensus disagreement------> (tx reverts; stays OPEN)
    RESOLVED and REVIEW are terminal: the case can never be re-assessed.
    """

    cases: TreeMap[u256, Case]
    case_count: u256

    def __init__(self):
        self.case_count = 0

    # ------------------------------------------------------------------
    # Case creation (deterministic — no non-deterministic calls here)
    # ------------------------------------------------------------------

    @gl.public.write
    def create_case(
        self,
        tenant: str,
        landlord: str,
        inventory_hash: str,
        move_in_urls: list[str],
        move_out_urls: list[str],
        deposit_amount: int,
        max_deduction_bps: int,
    ) -> int:
        """
        Register a deposit case. Evidence is stored as URL *references*;
        the assessment is performed later, by validators, inside consensus.
        The caller has no way to hint the outcome.
        """
        if not tenant or not landlord:
            raise gl.vm.UserError("tenant and landlord are required")
        if not inventory_hash:
            raise gl.vm.UserError("inventory_hash is required")
        if deposit_amount <= 0:
            raise gl.vm.UserError("deposit_amount must be positive")
        if max_deduction_bps < 0 or max_deduction_bps > MAX_BPS:
            raise gl.vm.UserError("max_deduction_bps must be between 0 and 10000")
        if (
            len(move_in_urls) == 0
            or len(move_out_urls) == 0
            or len(move_in_urls) > MAX_URLS_PER_STAGE
            or len(move_out_urls) > MAX_URLS_PER_STAGE
        ):
            raise gl.vm.UserError(
                "1..8 URLs are required for each of move-in and move-out evidence"
            )
        for url in list(move_in_urls) + list(move_out_urls):
            if not isinstance(url, str) or not (
                url.startswith("http://") or url.startswith("https://")
            ):
                raise gl.vm.UserError("evidence URLs must be http(s) URLs")

        case_id = self.case_count
        self.case_count = self.case_count + 1

        # DynArray fields accept plain sequences from the storage layer.
        self.cases[case_id] = Case(
            tenant=tenant,
            landlord=landlord,
            inventory_hash=inventory_hash,
            created_by=gl.message.sender_address.as_hex,
            move_in_urls=list(move_in_urls),
            move_out_urls=list(move_out_urls),
            deposit_amount=u256(deposit_amount),
            max_deduction_bps=u256(max_deduction_bps),
            status=STATUS_OPEN,
            outcome="",
            damage_class="",
            cost_band_bps=u256(0),
            deduction=u256(0),
            tenant_refund=u256(0),
            evidence_ok=False,
            assessment="",
        )

        return int(case_id)
