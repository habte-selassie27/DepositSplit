# GenLayer Portal Submission — DepositSplit

## Title

DepositSplit — Tenancy Deposit Arbiter

## Short description

DepositSplit is a reusable GenLayer Intelligent Contract primitive that
resolves tenancy deposit cases from independently verified move-in and
move-out evidence. Validators re-fetch the evidence during consensus,
agree on a normalized damage classification and cost band under an explicit
semantic equivalence principle, and deterministic contract code derives the
settlement. Unavailable, contradictory, or malformed evidence fails closed
into a REVIEW state — it can never produce an arbitrary deduction.

## Why this is an Intelligent Contract (not an "AI decides X" demo)

The contract depends on external, real-world evidence that deterministic
blockchain code cannot inspect: it must open hosted evidence pages, compare
property condition across time, and judge whether claimed damage is
supported. GenLayer validators perform exactly that during consensus.
The architecture to emphasize:

> Validators independently verify real-world evidence → consensus
> establishes the factual damage classification and cost band →
> deterministic contract code calculates the settlement → ambiguous
> evidence fails closed.

The caller never chooses the verdict: `assess_case()` takes only a case id,
and every settlement number is derived by bounded deterministic code from
the consensus result.

## Core consensus design (what to point reviewers at)

1. Cases store evidence **URL references** and an inventory hash — never
   caller-authored verdicts.
2. `assess_case(case_id)` accepts only the case id.
3. Inside `gl.vm.run_nondet`, the leader fetches all evidence with
   `gl.nondet.web.render(url, mode="text")` and evaluates it with
   `gl.nondet.exec_prompt(prompt, response_format="json")`.
4. Validators re-fetch and re-evaluate independently; `validator_fn`
   enforces semantic equivalence in explicit Python: exact
   `damage_class` match, exact `evidence_ok` match, cost band within
   500 bps (`_assessments_agree`).
5. Normalization (`_normalize_assessment`) is strict — unknown classes,
   out-of-range bands, or malformed output become
   `INSUFFICIENT_EVIDENCE` (fail closed), never a deduction.
6. Settlement (`_derive_settlement`) is deterministic and bounded by the
   case's `max_deduction_bps`.
7. Consensus disagreement reverts the transaction; the case stays OPEN
   and nothing settles on contested evidence.

## Example outcomes (demo plan)

| Case | Evidence setup | Expected result |
| ---- | -------------- | --------------- |
| 1. Clean tenancy | move-in and move-out pages describe identical condition | `FULL_REFUND`, deduction 0 |
| 2. Normal wear | move-out shows ordinary deterioration | `FULL_REFUND` (band forced to 0) |
| 3. Minor damage | move-out documents a small hole / stained carpet | `DEDUCT` with band ≤ cap |
| 4. Capped damage | consensus says 80% damage but cap is 20% | `DEDUCT` at 20% only |
| 5. Severe damage | move-out documents destruction; cap allows 100% | `FORFEIT` |
| 6. Dead URL | move-out URL unreachable | `REVIEW`, no deduction |
| 7. Empty/contradictory evidence | pages have no usable content | `REVIEW`, no deduction |

Cases 1–7 are all covered by the automated test suite in direct mode;
cases 1, 3, 4/5, and 6/7 should also be demonstrated on the deployed
testnet contract.

## Evidence to attach

- [ ] GitHub repository URL (this project)
- [ ] Deployed contract address on Bradbury testnet (via GenLayer Studio)
- [ ] At least one successful `create_case` → `assess_case` transaction
      (explorer links), ideally one from each outcome family
- [ ] Local verification output: `genvm-lint check` + `pytest tests/direct/ -v`
- [ ] README / ARCHITECTURE sections explaining the consensus design
- [ ] `scripts/demo_testnet.py` output showing the demo transactions

## Framing guardrails

- Do **not** describe it as "AI decides how much deposit the landlord
  gets."
- Do **not** present the testnet contract as a regulated tenancy
  arbitration service or a custodial escrow.
- Do describe: validators verify external evidence under an explicit
  equivalence principle; the contract — not the caller, not a single
  model — derives the settlement deterministically; failure is safe.
