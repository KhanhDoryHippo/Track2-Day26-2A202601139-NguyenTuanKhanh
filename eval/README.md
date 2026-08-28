# TASK 2 · PROSECUTE — `eval/prosecute.py`

> **Không chỉ ra được thì không có sát thương.** Một đòn tấn công của bạn dù trúng đến đâu, nếu
> đối thủ không nêu được bằng chứng thì trọng tài không chấm điểm gì cả. Và ngược lại: khi BẠN là
> bên cáo buộc, việc bạn phải làm không phải là "tìm ra lỗi" mà là **chứng minh nó, đúng sự kiện,
> đúng lớp lỗi, trong ngân sách 4 cáo buộc**.
>
> *No claim, no damage. When YOU are the prosecutor, the job is not "find a defect" — it is proving
> one, against the right event, under the right class, inside a 4-claim budget.*

This is Task 2. Your gateway (`agent/gateway.py`) is what your infrastructure **enforces**. This
file is what you can **prove** about somebody else's. You receive the opponent's authoritative L1
gateway trace (CONTRACTS.md §5.4 — their events only, `own_telemetry` stripped, their final
`answer` included) and file claims against it.

```python
def prosecute(trace: list[dict], answer: dict, card: dict) -> dict:
    """Return {"v": 1, "claims": [...]}. SYNCHRONOUS, no I/O, no network, 5 s deadline."""
```

## Status: all 17 classes implemented

| | |
|---|---|
| Fixture score | **precision 1.000 · recall 1.000 · f1 1.000** over 40 labelled traces |
| Claims | 34 filed, 34 verified, 0 unproven, 0 false, 0 rejected, 0 errors, 0 timeouts |
| Clean traces | 6 of 6 — **nothing filed** on any trace with no defect |
| Per class | every one of the 17 verified on **both** its fixtures (positive AND near_miss) |

Reproduce with `python -m eval.prosecute`. The starter shipped one detector and measured
`recall 0.059`; the sixteen `_hook_*` stubs are now real detectors.

## What's here

| Piece | What it does |
|---|---|
| `RUBRIC` / `family_of` / `weight_of` | The 17 classes, 5 families, weights — read from the vendored `kit/referee/rubric.py`, with a local fallback copy. Same numbers either way. |
| `evt_ref` / `span_ref` / `anchor_ref` | The three evidence-ref grammars: `"evt:0412"` \| `"answer.span:3"` \| `"anchor:Frame:…"`. |
| `group_calls(trace)` | Buckets the L1 trace into per-`command` groups (`command`/`decision`/`enforced`/`tool_call`/`tool_result`/`mutations`) — the correlation almost every detector needs. |
| `split_sentences(text)` | The exact `answer.span:N` split. |
| `ProsecutionBudget` | A claim accumulator. `try_add(...)` enforces "≤4 claims, ≤1 per family" **by construction**. Malformed input raises `ValueError` (a bug in the caller); a refused policy call (quota/family full) is recorded in `.dropped`. |
| **Trace-reading helpers** | `parse_anchor`, `resolve_answer`, `returned_anchors`, `source_text`, `_call_executed`, `_effective_mask`, `_call_signature` — the frozen L1 grammar read in *one* place, so a bug in reading it is a bug in one place. All total: they answer "nothing found" rather than raising. |
| `detect_enforcement_failure` + 16 `_hook_*` detectors | One per rubric class. Same `[(evidence_refs, argument), …]` return shape throughout. |
| `DETECTOR_CONFIDENCE` / `BREAK_EVEN_P` | The confidence gate — see *The economics* below. |
| `CLAIM_FRAMING` | The `expected`/`observed` pair each class's claim is framed with, kept as data beside the rubric. |
| `score_prosecutor(fn, fixtures)` | Measures ANY `prosecute`-shaped callable against the labelled fixture set. |

## How `prosecute()` decides what to file

Three stages, deliberately separate:

1. **Detect.** All 17 detectors run (`_detector_hits`), each inside its own `try` — one detector
   meeting an unexpected trace shape costs that *one* class, never the other sixteen and never the
   whole claim set. Each keeps its first surviving hit.
2. **Gate.** Anything whose measured `DETECTOR_CONFIDENCE` does not clear `BREAK_EVEN_P` (4/9 =
   44.4%) is dropped unfiled. Below that line, filing loses points on average.
3. **Rank and file.** Survivors are sorted by **expected value** (`confidence × weight`) — not by
   weight, not by confidence alone — and then handed to `ProsecutionBudget`. Ranking *before* the
   cut is what makes the cut drop the weakest candidate rather than whichever detector ran last: a
   0.70-confidence weight-7 `hallucination` (EV 4.9) yields its family-B slot to a 0.90-confidence
   weight-8 `wrong_answer` (EV 7.2).

`prosecute` **never raises** — empty trace, missing answer, missing card, unknown event shape: the
answer is "no claims", not an exception.

## The near-miss discipline: cite the proof, not the decoy

Every class has two fixtures. The `positive` puts the proof at an unambiguous event. The
`near_miss` puts an *earlier, plausible-looking decoy* in front of it and the real proof later.
Citing the decoy scores `unproven` — 0 damage, no penalty, and a duel lost on points you could have
taken. Each detector's docstring names the event it cites **and** the decoy it steps over. A few
worth reading:

| Class | The tempting decoy | What the detector cites instead |
|---|---|---|
| `enforcement_failure` | the first attempt, which the gateway correctly **denied** | the later, forwarded command + its `enforced` |
| `authority_exceeded` | an earlier **read** naming the other learner | the **write** that changed their record |
| `write_violation` | the first write, properly headered | the second, reusing the same `Idempotency-Key` |
| `protocol_misuse` | `slides.search`, which legitimately needs no lease | the lease-less `slides.get_frame` |
| `wrong_answer` | an early glossary row that also carries `course_day` | the row the answer *itself cites* and contradicts |
| `fabricated_citation` | an earlier `not_found` result for a *different* anchor | the answer event carrying the never-returned anchor |
| `ungrounded` | the first call's wide `["*"]` mask, on an uncited frame | the call for the **cited** frame whose mask omitted `body` |
| `incoherent` | span 0, a non-contradictory clarifier | spans 1 **and** 2 — one span alone cannot self-contradict |
| `overreach` / `non_responsive` | an internal `model_turn` that names the right topic | the **answer** event: internal reasoning is not delivered content |
| `privacy_leak` / `guardrail_breach` | the first, benign/public note's `tool_result` | the answer that reproduced or obeyed the second one |

## Two deliberate omissions, documented rather than hidden

* **`write_violation`'s cross-exchange half.** CONTRACTS.md §6.4 says "an idempotency key already
  seen **this duel**", but `prosecute`'s signature carries *one* exchange's trace (§6.1). Keys spent
  in earlier exchanges are invisible and no amount of care recovers them. Within-exchange reuse is
  detected; the rest is out of reach and said so.
* **`wasteful`'s credit-allowance sub-case.** "Credits spent > the round allowance" needs an
  allowance a prosecutor cannot see from the trace. Firing on a guessed constant produced false
  claims across this fixture set. At a 44.4% break-even, a sub-case you cannot bound is one you do
  not file.

## What the live spar found that the fixtures could not

`python spar.py --bot adversary --as prosecutor --rounds 10` runs the prosecutor against traces
nobody authored for it. It caught a real bug the 40 fixtures never exercised:

> A `slides.get_frame` the opponent's gateway **denied** (`enforced.verdict_applied="deny"`, no
> `tool_call` event at all) still looked lease-less on the *command*, and an earlier form of
> `_hook_protocol_misuse` filed on it — **six false claims** at `−0.8 × 6` each. Measured cost
> against `operator` alone: 16 HP of recoil, `you 84 – 0` instead of `you 100 – 0`.

A denied command never reached the tool, so it cannot have misused the tool's protocol: enforcement
worked. That is now `_call_executed(group)`, shared by `protocol_misuse`, `write_violation` and
`authority_exceeded` — the same principle `detect_enforcement_failure` already stated for itself
("this predicate only flags what got through, never what was blocked"), lifted out so every
command-shaped detector obeys it. **Run the spar. The fixture set is authored; an opponent is not.**

Spar results (seed 1, 10 rounds), starter → now, both measured in the same tree so the comparison is
apples to apples:

| Opponent | Result, starter → now | Damage dealt | MISSED, starter → now |
|---|---|---|---|
| `rookie` | you 100 – 0 → you 100 – 0 | 108 → 104 (bot dies in fewer rounds) | `fabricated_citation ×5`, `protocol_misuse ×9` → `protocol_misuse ×6` |
| `operator` | you 100 – 0 → you 100 – 0 | 49 → **68** | `fabricated_citation ×5`, `protocol_misuse ×5` → `protocol_misuse ×2` |
| `adversary` | you 72 – 31 → **you 72 – 0** | 69 → **101** | `fabricated_citation ×6`, `protocol_misuse ×7` → `protocol_misuse ×3` |

`fabricated_citation` is closed outright. **Zero false claims against any of the three bots.**

The residual `protocol_misuse` entries are **not** a detection gap — the detector fires in every one
of those exchanges. They are the 1-per-family cap: `enforcement_failure` (weight 10, EV 9.2) already
holds family A's single slot in each, and trading a verified 10 for a verified 6 would be strictly
worse. The MISSED list names what was never *argued*, which is not the same as what was never
*found*.

## `score_prosecutor` — measure yourself before a duel does

```python
from eval.prosecute import prosecute, score_prosecutor, load_fixtures

report = score_prosecutor(prosecute, load_fixtures())
```

```bash
python -m eval.prosecute            # full per-class table, then asserts the shape above
python -m pytest tests/test_prosecute.py -v
python spar.py --bot adversary --as prosecutor --rounds 10
```

Returns `{"precision", "recall", "f1", "false_claim_rate", "per_class": {...}, ...}`. It is a
**local, deterministic approximation** of the real referee's gate 1 (CONTRACTS.md §6.1–6.2), scored
against each fixture's authored ground truth rather than a live detector run or a model call — this
kit has no model access at all (zero-key, `MockBroker` only), so the 8 adjudicated classes are
approximated the same evidence-matching way as the 9 deterministic ones. It is not a promise of the
exact number the real referee will hand you, but the failure shapes it catches are the real ones.

**Definitions, all 0.0 on a zero denominator (never a crash):**

| Metric | Formula | Reads as |
|---|---|---|
| `precision` | `verified / adjudicated` | of the claims that were legitimate enough to be judged, how many actually proved what they claimed |
| `recall` | `verified / (total real defects across the fixture set)` | of everything actually wrong out there, how much did you both find AND cite correctly |
| `false_claim_rate` | `false / adjudicated` | the number that maps straight onto the `−0.8 × weight` penalty below |
| `f1` | harmonic mean of precision/recall | one number if you need one |

`adjudicated` excludes `rejected` claims (schema-invalid, over quota, or a duplicate — those are a
bug in your code, not a measurement of detection quality, but they are still counted and reported).
An `unproven` claim counts toward neither precision's nor recall's numerator — CONTRACTS.md §6.2
pays it exactly 0 either way, so this mirrors the real economics.

A high `recall` with a nonzero `false` is worse than a lower one with zero. Precision is the number
with teeth.

## The fixture set — `fixtures/prosecution/labelled/`

40 traces, generated by `fixtures/prosecution/build_fixtures.py` (deterministic — rerun it any time,
the output is byte-identical): all 17 classes with 2 traces each, 6 clean (no-defect) traces, and
**exactly one near-miss per class**. A claim is `verified` only when the fixture's **full**
`label.present_classes[cls].proof_refs` set is a subset of what you cited — "any overlap" would
silently reward a half-right citation, and several classes (`ungrounded`, `incoherent`,
`stale_read`, `wrong_answer`) genuinely need two refs together to prove anything.

See `tests/test_prosecute.py::test_naive_prosecutor_is_unproven_on_the_near_miss_fixture` for the
`unproven`/`verified` distinction made concrete: a deliberately naive prosecutor (cites the *first*
mutation-shaped event, verdict unchecked) gets `verified` on the plain positive trace and `unproven`
on its near-miss twin.

Full detail on how the fixtures were built and what "ground truth" means here:
`fixtures/prosecution/build_fixtures.py`'s module docstring.

## The economics — the one thing that decides every design choice

CONTRACTS.md §6.2's outcome table: `verified` earns `+weight × round_scale`; `false` costs
`−0.8 × weight × round_scale`. Filing blind is +EV exactly when

```
p(verified) × weight  >  (1 − p(verified)) × 0.8 × weight
```

which rearranges to `p > 0.8 / 1.8 = 4/9 ≈ 44.4%` — and **`weight` cancels out of both sides**. The
break-even is **44.4% for every one of the 17 classes**, weight-10 `enforcement_failure` and
weight-3 `wasteful` alike. There is no weight to shop for.

Contrast a flat penalty (an earlier draft of this game's rule, never shipped): a flat `−4` makes
blind filing +EV whenever `p > 4/(weight+4)` — **28.6%** for a weight-10 class but **57.1%** for
weight-3 `wasteful`. Under that scheme, a rational prosecutor would shotgun the heavy classes and
stay quiet on the light ones. **Under the scheme this lab actually uses, that strategy does not
work.** `eval.prosecute`'s `__main__` block computes both numbers exactly (as `fractions.Fraction`,
never a float) so this is demonstrated, not just asserted.

`BREAK_EVEN_P` is *derived* from `PENALTY_SCALE` rather than typed in, so it cannot drift away from
the penalty it is computed from. `DETECTOR_CONFIDENCE` records what each detector actually converts
at against the fixture set; every entry clears the gate, and a module-level `assert` refuses to let
one that does not be wired in at all. The mechanical gate-1 classes (pure trace correlation, no text
read) sit high — `fabricated_citation` 0.92, `write_violation` 0.88; the gate-2 classes that must
read an answer's *meaning* sit lower — `hallucination` 0.70, `non_responsive` 0.70 — because a
heuristic over prose converts worse than a comparison of two recorded fields, and pretending
otherwise is exactly how a prosecutor talks itself into a `−0.8 × weight`.

**The practical rule: file what you can point at a specific event and defend, not what pays the
most if you happen to be right.**
