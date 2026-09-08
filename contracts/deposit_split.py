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

    # ------------------------------------------------------------------
    # Assessment — the Intelligent Contract core
    # ------------------------------------------------------------------

    @gl.public.write
    def assess_case(self, case_id: int) -> None:
        """
        Run the evidence assessment through GenLayer consensus and derive
        the settlement deterministically.

        Takes ONLY the case id. The caller cannot influence the verdict.
        """
        if u256(case_id) not in self.cases:
            raise gl.vm.UserError("case does not exist")
        case = self.cases[u256(case_id)]
        if case.status != STATUS_OPEN:
            raise gl.vm.UserError("case is not open for assessment")

        # Copy plain values out of storage BEFORE the non-deterministic
        # block. Closures crossing the consensus boundary must not capture
        # storage handles — only picklable plain data.
        move_in_urls = [u for u in case.move_in_urls]
        move_out_urls = [u for u in case.move_out_urls]
        inventory_hash = case.inventory_hash

        def leader_fn() -> str:
            # ---------------------------------------------------------
            # Non-deterministic block — leader.
            # Fetches the evidence from the web and evaluates it.
            # Never raises: every failure normalizes to
            # INSUFFICIENT_EVIDENCE so ambiguity fails closed instead of
            # crashing consensus.
            # ---------------------------------------------------------

            def fetch_stage(urls: list) -> list:
                evidence = []
                for url in urls:
                    entry = {"url": url, "fetched": False, "content": ""}
                    try:
                        text = gl.nondet.web.render(url, mode="text")
                        entry["fetched"] = True
                        entry["content"] = (text or "")[:MAX_EVIDENCE_CHARS_PER_URL]
                    except Exception:
                        # Unreachable / invalid URL stays unfetched.
                        pass
                    evidence.append(entry)
                return evidence

            def evaluate() -> dict:
                move_in_evidence = fetch_stage(move_in_urls)
                move_out_evidence = fetch_stage(move_out_urls)

                # Mechanical evidence gate: every URL must have been
                # fetched and at least one stage must carry real content.
                all_fetched = all(e["fetched"] for e in move_in_evidence) and all(
                    e["fetched"] for e in move_out_evidence
                )
                has_content = any(
                    len(e["content"].strip()) > 0
                    for e in move_in_evidence + move_out_evidence
                )
                if not (all_fetched and has_content):
                    return {
                        "damage_class": DAMAGE_INSUFFICIENT,
                        "cost_band_bps": 0,
                        "evidence_ok": False,
                    }

                prompt = f"""You are an independent evidence assessor for a residential tenancy deposit case.

Inventory reference hash: {inventory_hash}

=== MOVE-IN EVIDENCE (condition at start of tenancy) ===
{json.dumps(move_in_evidence, sort_keys=True)}

=== MOVE-OUT EVIDENCE (condition at end of tenancy) ===
{json.dumps(move_out_evidence, sort_keys=True)}

Compare the move-out evidence against the move-in evidence and classify the property condition.

Return ONLY valid JSON with exactly these fields:
{{
  "damage_class": "NO_DAMAGE" | "NORMAL_WEAR" | "MINOR_DAMAGE" | "MAJOR_DAMAGE" | "INSUFFICIENT_EVIDENCE",
  "cost_band_bps": <integer 0..10000>,
  "evidence_ok": <true | false>
}}

Definitions:
- NO_DAMAGE: move-out condition matches move-in evidence.
- NORMAL_WEAR: ordinary deterioration consistent with normal use. NOT damage; cost_band_bps must be 0.
- MINOR_DAMAGE: small, clearly evidenced damage beyond normal wear.
- MAJOR_DAMAGE: substantial, clearly evidenced damage.
- INSUFFICIENT_EVIDENCE: evidence is empty, contradictory, unrelated to the property, or too unclear to classify.

Rules:
- Base every conclusion ONLY on the supplied evidence. Never invent facts.
- If you cannot reliably compare the two stages, set evidence_ok = false, damage_class = INSUFFICIENT_EVIDENCE, cost_band_bps = 0.
- cost_band_bps is the share of the deposit reasonably attributable to SUPPORTED damage, in basis points (10000 = 100%).
- Be conservative: never claim more damage than the evidence supports.
"""
                try:
                    result = gl.nondet.exec_prompt(prompt, response_format="json")
                    # Tolerate runners/mocks that hand back raw JSON text.
                    if isinstance(result, str):
                        result = json.loads(result)
                except Exception:
                    # Model failure -> fail closed.
                    return {
                        "damage_class": DAMAGE_INSUFFICIENT,
                        "cost_band_bps": 0,
                        "evidence_ok": False,
                    }
                try:
                    return _normalize_assessment(result)
                except Exception:
                    # Invalid model output -> fail closed.
                    return {
                        "damage_class": DAMAGE_INSUFFICIENT,
                        "cost_band_bps": 0,
                        "evidence_ok": False,
                    }

            return _canonical(evaluate())

        def validator_fn(leaders_res) -> bool:
            # ---------------------------------------------------------
            # Non-deterministic block — validator.
            # Independently re-fetches and re-evaluates the SAME evidence
            # and enforces the semantic equivalence principle against the
            # leader's result. This comparison is explicit contract code,
            # not an LLM judging itself.
            # ---------------------------------------------------------
            if not isinstance(leaders_res, gl.vm.Return):
                return False

            def fetch_stage(urls: list) -> list:
                evidence = []
                for url in urls:
                    entry = {"url": url, "fetched": False, "content": ""}
                    try:
                        text = gl.nondet.web.render(url, mode="text")
                        entry["fetched"] = True
                        entry["content"] = (text or "")[:MAX_EVIDENCE_CHARS_PER_URL]
                    except Exception:
                        pass
                    evidence.append(entry)
                return evidence

            def evaluate() -> dict:
                move_in_evidence = fetch_stage(move_in_urls)
                move_out_evidence = fetch_stage(move_out_urls)

                all_fetched = all(e["fetched"] for e in move_in_evidence) and all(
                    e["fetched"] for e in move_out_evidence
                )
                has_content = any(
                    len(e["content"].strip()) > 0
                    for e in move_in_evidence + move_out_evidence
                )
                if not (all_fetched and has_content):
                    return {
                        "damage_class": DAMAGE_INSUFFICIENT,
                        "cost_band_bps": 0,
                        "evidence_ok": False,
                    }

                prompt = f"""You are an independent evidence assessor for a residential tenancy deposit case.

Inventory reference hash: {inventory_hash}

=== MOVE-IN EVIDENCE (condition at start of tenancy) ===
{json.dumps(move_in_evidence, sort_keys=True)}

=== MOVE-OUT EVIDENCE (condition at end of tenancy) ===
{json.dumps(move_out_evidence, sort_keys=True)}

Compare the move-out evidence against the move-in evidence and classify the property condition.

Return ONLY valid JSON with exactly these fields:
{{
  "damage_class": "NO_DAMAGE" | "NORMAL_WEAR" | "MINOR_DAMAGE" | "MAJOR_DAMAGE" | "INSUFFICIENT_EVIDENCE",
  "cost_band_bps": <integer 0..10000>,
  "evidence_ok": <true | false>
}}

Definitions:
- NO_DAMAGE: move-out condition matches move-in evidence.
- NORMAL_WEAR: ordinary deterioration consistent with normal use. NOT damage; cost_band_bps must be 0.
- MINOR_DAMAGE: small, clearly evidenced damage beyond normal wear.
- MAJOR_DAMAGE: substantial, clearly evidenced damage.
- INSUFFICIENT_EVIDENCE: evidence is empty, contradictory, unrelated to the property, or too unclear to classify.

Rules:
- Base every conclusion ONLY on the supplied evidence. Never invent facts.
- If you cannot reliably compare the two stages, set evidence_ok = false, damage_class = INSUFFICIENT_EVIDENCE, cost_band_bps = 0.
- cost_band_bps is the share of the deposit reasonably attributable to SUPPORTED damage, in basis points (10000 = 100%).
- Be conservative: never claim more damage than the evidence supports.
"""
                try:
                    result = gl.nondet.exec_prompt(prompt, response_format="json")
                    # Tolerate runners/mocks that hand back raw JSON text.
                    if isinstance(result, str):
                        result = json.loads(result)
                except Exception:
                    return {
                        "damage_class": DAMAGE_INSUFFICIENT,
                        "cost_band_bps": 0,
                        "evidence_ok": False,
                    }
                try:
                    return _normalize_assessment(result)
                except Exception:
                    return {
                        "damage_class": DAMAGE_INSUFFICIENT,
                        "cost_band_bps": 0,
                        "evidence_ok": False,
                    }

            leader_assessment = _parse_assessment(leaders_res.calldata)
            if leader_assessment is None:
                return False
            try:
                leader_assessment = _normalize_assessment(leader_assessment)
            except Exception:
                return False

            own_assessment = evaluate()
            return _assessments_agree(leader_assessment, own_assessment)

        # Consensus: leader result is accepted only when independent
        # validators re-derive a semantically equivalent assessment.
        # On validator disagreement the transaction reverts and the case
        # stays OPEN — no settlement can be built on contested evidence.
        result = gl.vm.run_nondet(leader_fn, validator_fn)

        assessment = _parse_assessment(result)
        if assessment is None:
            self._enter_review(case, REVIEW_UNPARSABLE)
            return

        # Defensive re-validation of the consensus result before it is
        # allowed anywhere near the money math.
        try:
            assessment = _normalize_assessment(assessment)
        except Exception:
            self._enter_review(case, REVIEW_UNPARSABLE)
            return

        if not assessment["evidence_ok"] or (
            assessment["damage_class"] == DAMAGE_INSUFFICIENT
        ):
            # Fail closed: unresolved evidence is never a deduction.
            self._enter_review(case, REVIEW_INSUFFICIENT_EVIDENCE, assessment)
            return

        settlement = _derive_settlement(
            deposit_amount=int(case.deposit_amount),
            damage_class=assessment["damage_class"],
            cost_band_bps=assessment["cost_band_bps"],
            max_deduction_bps=int(case.max_deduction_bps),
        )

        case.status = STATUS_RESOLVED
        case.outcome = settlement["outcome"]
        case.damage_class = settlement["damage_class"]
        case.cost_band_bps = u256(settlement["cost_band_bps"])
        case.deduction = u256(settlement["deduction"])
        case.tenant_refund = u256(settlement["tenant_refund"])
        case.evidence_ok = True
        case.assessment = _canonical(assessment)

    def _enter_review(self, case, reason: str, assessment: dict | None = None) -> None:
        """
        Fail-closed terminal state. No deduction is authorized; the
        notional refund equals the full deposit pending human review.
        """
        case.status = STATUS_REVIEW
        case.outcome = OUTCOME_REVIEW
        if assessment is not None:
            case.damage_class = assessment.get("damage_class", "")
            case.evidence_ok = False
        else:
            case.damage_class = ""
            case.evidence_ok = False
        case.cost_band_bps = u256(0)
        case.deduction = u256(0)
        case.tenant_refund = case.deposit_amount
        case.assessment = reason

    # ------------------------------------------------------------------
    # Views (deterministic reads)
    # ------------------------------------------------------------------

    @gl.public.view
    def get_case(self, case_id: int) -> dict:
        if u256(case_id) not in self.cases:
            raise gl.vm.UserError("case does not exist")
        c = self.cases[u256(case_id)]
        return {
            "case_id": int(case_id),
            "tenant": c.tenant,
            "landlord": c.landlord,
            "inventory_hash": c.inventory_hash,
            "created_by": c.created_by,
            "move_in_urls": [u for u in c.move_in_urls],
            "move_out_urls": [u for u in c.move_out_urls],
            "deposit_amount": int(c.deposit_amount),
            "max_deduction_bps": int(c.max_deduction_bps),
            "status": c.status,
            "outcome": c.outcome,
            "damage_class": c.damage_class,
            "cost_band_bps": int(c.cost_band_bps),
            "deduction": int(c.deduction),
            "tenant_refund": int(c.tenant_refund),
            "evidence_ok": c.evidence_ok,
            "assessment": c.assessment,
        }

    @gl.public.view
    def get_settlement(self, case_id: int) -> dict:
        if u256(case_id) not in self.cases:
            raise gl.vm.UserError("case does not exist")
        c = self.cases[u256(case_id)]
        return {
            "case_id": int(case_id),
            "status": c.status,
            "outcome": c.outcome,
            "damage_class": c.damage_class,
            "cost_band_bps": int(c.cost_band_bps),
            "deposit_amount": int(c.deposit_amount),
            "max_deduction_bps": int(c.max_deduction_bps),
            "deduction": int(c.deduction),
            "tenant_refund": int(c.tenant_refund),
            "evidence_ok": c.evidence_ok,
        }

    @gl.public.view
    def get_case_count(self) -> int:
        return int(self.case_count)
