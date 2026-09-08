# DepositSplit Architecture

## Trust boundary

```text
Caller
  |
  | create_case(tenant, landlord, inventory_hash,
  |             move_in_urls, move_out_urls,
  |             deposit_amount, max_deduction_bps)
  |        <-- no verdict, no classification, no band accepted
  v
On-chain case state (OPEN)
  |
  | assess_case(case_id)          <-- only the case id
  v
GenLayer non-deterministic block (gl.vm.run_nondet)
  |
  +--> LEADER
  |      gl.nondet.web.render(url, mode="text")   for every evidence URL
  |      mechanical gate: all fetched + non-empty content
  |      gl.nondet.exec_prompt(prompt, response_format="json")
  |      normalize -> strict JSON assessment
  |
  +--> VALIDATORS (independently)
  |      re-fetch the same URLs
  |      re-run the same evaluation
  |      enforce equivalence vs. leader result (explicit Python code)
  |
  v
Consensus agreed?
  |
  +-- no --> transaction reverts, case stays OPEN (nothing settles)
  |
  +-- yes
       v
Deterministic contract code
  +--> re-validate the consensus result (defensive)
  +--> fail-closed check: evidence_ok / INSUFFICIENT_EVIDENCE -> REVIEW
  +--> apply max_deduction_bps cap
  +--> deduction = deposit * band / 10000
  +--> outcome: FULL_REFUND | DEDUCT | FORFEIT
       v
RESOLVED (or REVIEW)
```

## Why the caller cannot choose the result

`assess_case()` takes only `case_id`. There is no method parameter
anywhere that accepts an outcome, damage class, cost band, or deduction.
Those values exist only as outputs of (a) the consensus assessment and
(b) the deterministic settlement function applied to it. The settlement
function is additionally bounded by `max_deduction_bps`, which is fixed at
case creation and is part of the signed case parameters.

## Why evidence is re-fetched

Evidence is stored as URL references, not trusted descriptions. The
non-deterministic block fetches those URLs with `gl.nondet.web.render`, so
the consensus process — not the caller — observes the external evidence.
Every validator fetches independently; the leader's copy is never accepted
on faith.

## The semantic equivalence principle

Implemented explicitly in `validator_fn` (contract code, not an LLM judge):

| Dimension      | Rule                                   | Rationale                                  |
| -------------- | -------------------------------------- | ------------------------------------------ |
| `damage_class` | must match **exactly**                 | the factual finding must be identical       |
| `evidence_ok`  | must match **exactly**                 | evidence verifiability is binary            |
| `cost_band_bps`| within **500 bps** tolerance           | subjective cost estimates vary slightly; materially consistent is enough |

If any rule fails, the validator votes against the leader and consensus
disagrees. The final band used is the leader's (already consensus-bounded
by the cap and by the "normal wear ⇒ 0" rule in normalization).

## Deterministic settlement

```text
effective_band = min(consensus_band, max_deduction_bps)   # and clamped to [0, 10000]
NO_DAMAGE / NORMAL_WEAR  =>  effective_band = 0
deduction      = deposit_amount * effective_band / 10000
tenant_refund  = deposit_amount - deduction
outcome        = FULL_REFUND (deduction == 0)
               | DEDUCT      (0 < deduction < deposit)
               | FORFEIT     (deduction == deposit)
```

## Fail-closed matrix

| Failure                                       | Detected by            | Result                          |
| --------------------------------------------- | ---------------------- | ------------------------------- |
| URL unreachable / fetch error                 | mechanical gate        | `INSUFFICIENT_EVIDENCE` → REVIEW |
| Empty or whitespace-only evidence content     | mechanical gate        | `INSUFFICIENT_EVIDENCE` → REVIEW |
| Model returns non-JSON / malformed JSON       | normalization try/except | `INSUFFICIENT_EVIDENCE` → REVIEW |
| Unknown damage class (invented category)      | normalization          | `INSUFFICIENT_EVIDENCE` → REVIEW |
| `cost_band_bps` out of [0, 10000]             | normalization          | `INSUFFICIENT_EVIDENCE` → REVIEW |
| `evidence_ok = false` from the assessor       | semantic layer         | REVIEW                          |
| `INSUFFICIENT_EVIDENCE` classification        | semantic layer         | REVIEW                          |
| Consensus result unparsable at settlement time | defensive re-validation | REVIEW (`CONSENSUS_RESULT_UNPARSABLE`) |
| Validators disagree                           | consensus              | transaction reverts; case stays OPEN |
| Caller retries assessment on a closed case    | state machine          | transaction reverts             |

In every row, no deduction is authorized. `REVIEW` records
`tenant_refund = deposit_amount` notionally and waits for a human/off-chain
process.

## State machine

```text
        create_case             assess_case (consensus OK, evidence OK)
OPEN ────────────────► OPEN ─────────────────────────────────► RESOLVED (terminal)
  ▲                      │
  │ consensus disagreement│ assess_case (insufficient/unavailable evidence)
  └──────────────────────┴─────────────────────────────────► REVIEW (terminal)
   (transaction reverts, nothing changes)
```

## Input bounds

- 1–8 evidence URLs per stage, each `http(s)`.
- `deposit_amount > 0`; `max_deduction_bps ∈ [0, 10000]`.
- Each fetched page contributes at most 6000 characters to the assessor
  prompt, so the prompt size is bounded regardless of the target page.

## Production-safety notes

- Closures passed to `gl.vm.run_nondet` capture only plain strings/lists —
  never storage handles — and the test suite verifies cloudpickle-ability
  (`direct_vm.check_pickling = True`).
- Normalization is strict: anything structurally invalid becomes
  `INSUFFICIENT_EVIDENCE`, so a malicious or broken model can only push the
  case **toward** safety (REVIEW), never toward a larger deduction.
- User-facing validation failures raise `gl.vm.UserError`, so reverts are
  distinguishable from internal VM errors (result code 1 vs 2) and the
  GenVM linter stays clean.

## Extension points

- **Escrow integration** — pair with a token/escrow contract; the settlement
  figures (`deduction`, `tenant_refund`) become payout instructions.
- **Appeal window** — add a timed `appeal()` that re-opens a `RESOLVED`
  case for a fresh consensus round with a higher quorum.
- **Evidence hash registry** — anchor evidence content hashes at creation
  so tampering with hosted pages is detectable.
- **Photo-native assessment** — replace text rendering with
  `gl.nondet.web.render(url, mode="screenshot")` +
  `gl.nondet.exec_prompt(prompt, images=[...])` for image evidence.
- **Jurisdiction rulesets** — parameterize wear-and-tear definitions and
  cap defaults by ruleset id.
- **Events** — emit state-transition events once the target SDK version
  exposes them, for indexer-friendly tracking.
