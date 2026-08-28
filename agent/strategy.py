"""agent/strategy.py — discovery, delegation, caching, replica, and budget
POLICY. Where `agent/gateway.py` is the control plane (route / admit /
authorize / budget — the four JOBS a decision must do), this file is the
building blocks a real answer to those jobs is made of. Nothing here is
wired into `Gateway.decide` by default — see agent/gateway.py's own module
docstring and agent/README.md's table for where each piece is meant to
plug in. That wiring is the assignment, not a step you're missing.

THE ARITHMETIC THAT MAKES THIS FILE'S EXISTENCE THE LESSON
----------------------------------------------------------------------------
A duel gives EACH SIDE 100 credits, ONCE, for all 10 rounds combined
(CONTRACTS.md 4.2's `GatewayContext.credits`; FINAL-PLAN.md section 4). Two
ways to spend a single round, both real, both computed against
`kit.mcp.specs.TOOL_SPECS` (this file's `__main__` demo below recomputes
them live rather than just asserting the numbers, so they can never
silently drift from the real cost table):

    DISCIPLINED  slides.query(fields=[title,body])   base1 + (body2+title0) + 1row*1  =  4
                 slides.get_frame(default fields)     base2 + (body+title = 2)        =  4
                 registry.provenance(default fields)  base1 + (etag0)                 =  1
                 -------------------------------------------------------
                 = 9 credits this round — the CEILING of FINAL-PLAN.md 4.3's
                   "8-11" (a round that skips the provenance re-read, or
                   reuses a cached body via `ResultCache` below, lands
                   nearer the floor of that range instead).

    CARELESS     registry.list_servers(fields=[*])    ->            12
                 glossary.list_terms()  (default==full "punishment
                                          button", not a narrow mask) -> 10
                 slides.get_frame(fields=[*]) x3       ->  9 x 3   = 27
                 -------------------------------------------------------
                 = 49 credits — MORE THAN ONE THIRD OF THE WHOLE DUEL'S
                   BUDGET, spent in a single round.

Play at the DISCIPLINED CEILING every single round and 10 rounds cost 90 of
the 100-credit pool — 10 credits of margin for the whole duel, and not one
more. (That margin exists because `kit/mcp/specs.py`'s own D-10 FIX retuned
`slides.query` for exactly this reason: at the 11 cr this file used to
quote, ten disciplined rounds cost 110 and the pool was structurally
unplayable. This module's `__main__` demo recomputes the number live rather
than trusting this prose, which is how the retune was noticed at all.) Ten
credits is not slack you can spend twice: one careless catalog read (12 cr)
already exceeds it, and CONTRACTS.md 4.1 charges 2 cr for every malformed
`Decision` or blown deadline on top. "Disciplined" is therefore not a magic
number — it is not re-paying for the same provenance read or the same frame
body every round when you already have it (`ResultCache` below, and
`BudgetPacer.is_affordable`'s reserve floor, whose flat reserve
`reserve_for_round` below replaces with a round-aware one). Play CARELESS
even once and you are mathematically bankrupt by
round 3 (100 − 49 − 49 < 0) — not because the game is rigged against you,
but because `registry.list_servers` and `glossary.list_terms` were
deliberately built so their DEFAULT field mask is their full, expensive
dump (FINAL-PLAN.md section 4.1: "an audit showed `list_servers` and
`list_terms` each exceeded a whole round's sustainable allowance —
punishment buttons, not decisions"). Naming exactly the fields you plan to
actually CITE, every time, is not a minor optimisation here; it is the
difference between finishing the duel and not.

Stdlib only. No network, no randomness, no wall-clock reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# kit.mcp.specs is a collaborator's file (workspace hard rule 2). It is
# present and stable as of this writing, but this module must still degrade
# gracefully if a concurrent edit ever makes it briefly unimportable — the
# fallback table below covers exactly the tools this file's own functions
# and demo reference, nothing more.
try:
    from kit.mcp.specs import TOOL_SPECS, cost as _spec_cost
    _SPECS_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    TOOL_SPECS = {}
    _SPECS_AVAILABLE = False

    def _spec_cost(server: str, tool: str, fields: tuple[str, ...] = (), n_rows: int = 1) -> int:
        """Degraded fallback: a small, hand-copied anchor-price table
        (CONTRACTS.md 3.4's own named anchor prices) covering only the
        (server, tool) pairs this file's functions/demo touch. Real pricing
        always comes from kit.mcp.specs when it is importable — this table
        exists so the file still RUNS, not so it stays authoritative."""
        anchors = {
            ("slides", "query"): 4,  # fields=[title,body], n_rows=1
            ("slides", "get_frame"): 4,  # default fields
            ("registry", "provenance"): 1,
            ("registry", "list_servers"): 12,  # fields=[*]
            ("glossary", "list_terms"): 10,  # default == full dump
        }
        return anchors.get((server, tool), 5)  # 5: an honest "I don't know" default, not 0


__all__ = [
    "ROUNDS_PER_DUEL",
    "SAFE_STARTING_RESERVE",
    "MIN_VIABLE_ROUND_COST",
    "ROUND_ALLOWANCE",
    "CATALOG_TRAP_TOOLS",
    "DEPRECATED_SUCCESSORS",
    "NARROW_MASKS",
    "disciplined_round_cost",
    "careless_round_cost",
    "is_catalog_trap",
    "cheap_mask",
    "narrow_mask",
    "estimated_cost",
    "reserve_for_round",
    "successor_of",
    "BudgetPacer",
    "ReplicaChoice",
    "pick_replica",
    "ResultCache",
    "should_delegate",
]

ROUNDS_PER_DUEL = 10

# A pacing target, not a hard rule: if you have spent MORE than this
# fraction of your remaining budget by the time you decide whether THIS
# round's call is affordable, you are trending toward the careless curve
# above, not the disciplined one. `BudgetPacer.is_affordable` below uses it
# as its one, deliberately simple, heuristic.
SAFE_STARTING_RESERVE = 0.5  # keep at least half the ORIGINAL pool as a floor

# The cheapest round that can still GROUND an answer: one `slides.query` to
# locate (2 cr) plus one `registry.provenance` to pin (1 cr), with 2 cr of
# slack for the arena's own error penalties (CONTRACTS.md 4.1 charges 2 cr
# for a malformed Decision or a blown deadline). Below this a round cannot
# cite anything it has actually retrieved, which is `ungrounded` (weight 5)
# and `non_responsive` (weight 4) bought with the credits you saved. This is
# the floor `reserve_for_round` protects for every round STILL TO COME.
MIN_VIABLE_ROUND_COST = 5

# Per-round CREDIT allowance, and the reason it is NOT flat.
#
# Damage in a duel is scaled by the round: x1.0 for rounds 1-3, x1.25 for
# 4-7, x1.5 for 8-10 (spar.py's `round_scale`, mirroring the arena's). A
# credit that buys a grounded citation in round 9 therefore prevents 1.5x
# the damage the same credit prevents in round 2 — so spending the 100-credit
# pool EVENLY is a measurable mistake, not merely an aesthetic one. The table
# below back-loads the pool deliberately and sums to exactly 100:
#
#     rounds 1-3   9 cr each  = 27   (x1.0  — the cheap rounds, played lean)
#     rounds 4-7  10 cr each  = 40   (x1.25 — the disciplined round's price)
#     rounds 8-10 11 cr each  = 33   (x1.5  — where a credit is worth most)
#
# It is an allowance, not a quota: what a round does not spend stays in
# `GatewayContext.credits` and is available to every round after it, which
# is exactly what makes underspending early a real strategy rather than a
# missed opportunity.
ROUND_ALLOWANCE: Mapping[int, int] = {
    1: 9, 2: 9, 3: 9,
    4: 10, 5: 10, 6: 10, 7: 10,
    8: 11, 9: 11, 10: 11,
}

# The two named "punishment button" tools (FINAL-PLAN.md 4.1): their
# DEFAULT field mask is their full, most expensive dump, not a cheap
# starting point. Calling either with no `fields=` is never an accident
# worth repeating.
CATALOG_TRAP_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {("registry", "list_servers"), ("glossary", "list_terms")}
)

# CONTRACTS.md 4.2 mechanic 8: `slides.search` is deprecated in favour of
# `slides.query`. Kept here as data (mirroring kit/mcp/specs.py's own
# "the economy is data, not code" philosophy) so a `route`/`budget` job can
# look a tool up without re-deriving deprecation from `TOOL_SPECS` by hand.
DEPRECATED_SUCCESSORS: Mapping[tuple[str, str], tuple[str, str]] = {
    ("slides", "search"): ("slides", "query"),
}

# The mask this team actually sends when the model named none — one row per
# tool, chosen as "the fields an answer of this ask type would genuinely
# cite", not as "the fields that exist". Every entry below is validated
# against `kit.mcp.specs.TOOL_SPECS.all_fields` by this module's own
# `__main__` demo, so a retune of the economy (or a typo here) fails loudly
# in `python -m agent.strategy` rather than as a mid-duel `KeyError` inside
# `cost_of`.
#
# Two entries earn their place more than the rest, because they are the
# FINAL-PLAN.md 4.1 "punishment buttons" whose DEFAULT mask is their full
# dump (see `CATALOG_TRAP_TOOLS`):
#
#     registry.list_servers   default 12 cr -> ("name",)  2 cr
#     glossary.list_terms     default 10 cr -> ("term",)  2 cr
#
# `glossary.list_terms` narrows to bare `("term",)` on purpose: browsing a
# catalog is a question about WHICH TERMS EXIST. If you want a definition,
# `glossary.define` answers that for 1 credit — paying 10 to have every
# definition dumped so you can read one of them is the exact shape of the
# `wasteful` class.
NARROW_MASKS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("slides", "query"): ("title",),
    ("slides", "get_frame"): ("body", "title"),
    ("slides", "whatlinkshere"): ("targets",),
    ("slides", "search"): ("anchor", "title"),  # only reachable pre-rewrite
    ("glossary", "define"): ("definition",),
    ("glossary", "list_terms"): ("term",),
    ("registry", "provenance"): ("etag", "rev"),
    ("registry", "list_servers"): ("name",),
    ("research", "cite_source"): ("anchor", "url"),
    ("labs", "get_exercise"): ("summary",),
    ("progress", "record_mastery"): ("receipt_id",),
    ("content", "flag_stale_slide"): ("receipt_id",),
    ("curriculum-analyst", "which_days_cover"): ("anchor", "course_day", "track"),
    ("citation-checker", "verify_source"): ("anchor", "url"),
    ("roster", "lookup_learner"): ("act", "scopes"),
}


def disciplined_round_cost() -> int:
    """The module docstring's "DISCIPLINED" total, computed live against
    the real cost table (or the degraded fallback) rather than hard-coded —
    so if `kit/mcp/specs.py` is ever retuned, this number moves with it
    instead of silently lying."""
    return (
        _spec_cost("slides", "query", fields=("title", "body"), n_rows=1)
        + _spec_cost("slides", "get_frame")
        + _spec_cost("registry", "provenance")
    )


def careless_round_cost() -> int:
    """The module docstring's "CARELESS" total, same live-computation
    reasoning as `disciplined_round_cost` above."""
    return (
        _spec_cost("registry", "list_servers", fields=("*",))
        + _spec_cost("glossary", "list_terms")
        + 3 * _spec_cost("slides", "get_frame", fields=("*",))
    )


def is_catalog_trap(server: str, tool: str, fields: tuple[str, ...]) -> bool:
    """True iff `(server, tool)` is one of the two "punishment button"
    tools AND the caller passed no explicit mask (`fields` empty) or asked
    for everything (`("*",)`) — i.e. is about to pay the DEFAULT/full price
    rather than a deliberately chosen cheap one. A `route`/`budget` job can
    use this as the trigger for "rewrite this call's fields before letting
    it through"."""
    if (server, tool) not in CATALOG_TRAP_TOOLS:
        return False
    return fields in ((), ("*",))


def cheap_mask(server: str, tool: str, fields_you_will_actually_cite: tuple[str, ...]) -> tuple[str, ...]:
    """Given the fields your answer will actually cite, return exactly
    those, sorted — the discipline that keeps `slides.get_frame` at 4
    credits instead of 9, and `registry.list_servers` at 2 instead of 12
    (CONTRACTS.md 3.4's own named anchor prices). This function does not
    know what your answer needs; YOU do — pass the honest set. Passing an
    EMPTY set here is itself informative: it means you are about to make a
    call whose result you do not plan to cite, which is the `wasteful`
    class waiting to happen (CONTRACTS.md 6.4's detector: "credits spent >
    the round allowance").

    `server`/`tool` are accepted (and validated against `TOOL_SPECS` when
    available) purely so a caller gets an early, loud `KeyError` for a typo
    rather than a silently wrong mask two calls later."""
    if _SPECS_AVAILABLE and (server, tool) not in TOOL_SPECS:
        raise KeyError(f"{server}.{tool} is not a known tool in kit.mcp.specs.TOOL_SPECS")
    return tuple(sorted(set(fields_you_will_actually_cite)))


def successor_of(server: str, tool: str) -> tuple[str, str] | None:
    """`(server, tool)`'s non-deprecated replacement, or `None` if it is
    not deprecated at all. A `route` job's cheapest possible win: rewriting
    `slides.search` to `slides.query` before forwarding costs you nothing
    and removes the `wasteful` "used a deprecated tool" detector hit
    (CONTRACTS.md 6.4) entirely."""
    return DEPRECATED_SUCCESSORS.get((server, tool))


def _all_fields(server: str, tool: str) -> tuple[str, ...] | None:
    """Every field `(server, tool)` can legally be asked for, or `None` when
    the tool is unknown to `kit.mcp.specs` (or the specs module is degraded).
    Used to SANITISE a mask before pricing it — `kit.mcp.specs.cost_of`
    raises `KeyError` on a field the tool does not have, and `Gateway.decide`
    is forbidden from raising, so an unknown field must be dropped here
    rather than blown up on downstream."""
    if not _SPECS_AVAILABLE:
        return None
    spec = TOOL_SPECS.get((server, tool))
    return tuple(getattr(spec, "all_fields", ())) if spec is not None else None


def narrow_mask(server: str, tool: str, requested: tuple[str, ...]) -> tuple[str, ...]:
    """The mask a BUDGET job should actually forward for `(server, tool)`.

    Three cases, in order, and the middle one is the whole point:

      1. The caller named nothing (`()`) or asked for everything (`("*",)`)
         — that is not a mask, it is a decision to pay the tool's default,
         which for `registry.list_servers` and `glossary.list_terms` IS the
         full dump (`CATALOG_TRAP_TOOLS`). Substitute this team's own narrow
         default from `NARROW_MASKS`, falling back to the spec's
         `default_fields` for a tool nobody listed.
      2. The caller named real fields — RESPECT THEM. A mask the model chose
         is the model saying "these are the fields my answer will cite", and
         second-guessing it here is how a gateway ends up `ungrounded`
         (weight 5) for a field the answer needed and never received. Only
         fields the tool does not actually have are dropped, and only because
         pricing them would raise.
      3. Sanitising emptied the mask (every named field was bogus) — fall
         back to case 1's narrow default rather than sending `()`, which
         would silently re-select the tool's expensive default.

    Never raises. `cheap_mask` above is the version that DOES raise on an
    unknown tool, which is the right behaviour when a human is choosing a
    mask by hand and the wrong behaviour inside `Gateway.decide`."""
    fallback = NARROW_MASKS.get((server, tool))
    if fallback is None and _SPECS_AVAILABLE:
        spec = TOOL_SPECS.get((server, tool))
        fallback = tuple(getattr(spec, "default_fields", ())) if spec is not None else ()
    fallback = tuple(fallback or ())

    asked = tuple(requested or ())
    if not asked or asked == ("*",):
        return fallback

    legal = _all_fields(server, tool)
    if legal is None:
        # Specs degraded or unknown tool: we cannot tell a real field from a
        # typo, so pass the caller's mask through untouched rather than
        # inventing a narrowing we cannot justify.
        return tuple(sorted(set(asked)))
    kept = tuple(sorted({f for f in asked if f in legal}))
    return kept or fallback


#: What an unpriceable call is assumed to cost. Higher than the disciplined
#: round's ~3 cr per-call average on purpose: "I don't know" should make the
#: pacer more careful, never less.
_UNKNOWN_TOOL_COST = 6


def estimated_cost(
    server: str, tool: str, fields: tuple[str, ...] = (), n_rows: int = 1
) -> int:
    """What forwarding this call is expected to cost, in credits, priced off
    the real `kit.mcp.specs` table when it is importable and off this
    module's degraded anchor table when it is not.

    NEVER RAISES — that is the entire reason this wrapper exists rather than
    calling `kit.mcp.specs.cost` directly from `Gateway.decide`. `cost_of`
    raises `KeyError` for an unknown tool or an unknown field in the mask,
    and a raised exception inside `decide()` is charged as a denied command
    PLUS 2 credits PLUS a scored `integrity` event (CONTRACTS.md 4.1). An
    honest over-estimate is strictly cheaper than an exception: when pricing
    fails we return `_UNKNOWN_TOOL_COST`, which is deliberately above the
    disciplined round's per-call average so a mispriced call is held back by
    the pacer rather than waved through."""
    try:
        return int(_spec_cost(server, tool, tuple(fields or ()), n_rows))
    except Exception:
        # Retry once with the mask sanitised — the common failure is one
        # bogus field name, not an unknown tool.
        try:
            return int(_spec_cost(server, tool, narrow_mask(server, tool, tuple(fields or ())), n_rows))
        except Exception:
            return _UNKNOWN_TOOL_COST


def reserve_for_round(round_no: int, *, starting_pool: int = 100) -> float:
    """The fraction of the ORIGINAL pool that must survive this round —
    `BudgetPacer.is_affordable`'s `reserve` argument, made round-aware.

    `SAFE_STARTING_RESERVE`'s flat 0.5 is the thing `BudgetPacer.is_affordable`'s
    own docstring warns about: "by round 8-10 you know your real remaining
    need better than a flat reserve does... over-cautious late". A flat 50%
    floor means round 10 — the round whose credits are worth 1.5x — cannot
    spend the credits it saved, which converts good pacing into unspent
    budget at the exact moment budget is worth most.

    So the reserve DECAYS with the rounds still to come: keep
    `MIN_VIABLE_ROUND_COST` credits for each round after this one, and
    nothing more.

        round  1 -> 9 rounds left x 5 cr = 45 cr = 0.45 of the pool
        round  5 -> 5 rounds left x 5 cr = 25 cr = 0.25
        round 10 -> 0 rounds left        =  0 cr = 0.00  (spend it all)

    Clamped into [0.0, 1.0] and tolerant of a nonsense `round_no` (the arena
    owns that number; this function must not be the thing that breaks when
    it is surprising)."""
    if not isinstance(round_no, int) or isinstance(round_no, bool):
        round_no = 1
    bounded = max(1, min(round_no, ROUNDS_PER_DUEL))
    rounds_left = ROUNDS_PER_DUEL - bounded
    if starting_pool <= 0:
        return 0.0
    return max(0.0, min(1.0, (rounds_left * MIN_VIABLE_ROUND_COST) / float(starting_pool)))


@dataclass
class BudgetPacer:
    """Tracks YOUR OWN running spend across a duel and answers one
    question: "can I still afford this round the way I've been playing?"
    Deliberately simple — a flat per-round reserve check, not a model of
    the whole remaining duel — because the arithmetic in this file's module
    docstring is already the whole lesson: stay near the disciplined
    number every round and the pacing question barely matters; drift
    toward the careless number even once and no pacer saves you.

    This is YOUR bookkeeping, independent of `GatewayContext.credits` (the
    arena's authoritative figure) — the two SHOULD agree; if they ever
    disagree, trust `ctx.credits`, and treat the mismatch itself as
    something worth a `Telemetry.note(...)` (agent/telemetry.py)."""

    starting_pool: int = 100
    rounds_total: int = ROUNDS_PER_DUEL
    _spent: int = field(default=0, init=False)
    _spent_by_round: dict[int, int] = field(default_factory=dict, init=False)

    def record_spend(self, round_no: int, cost: int) -> None:
        if cost < 0:
            raise ValueError(f"cost must be non-negative, got {cost}")
        self._spent += cost
        self._spent_by_round[round_no] = self._spent_by_round.get(round_no, 0) + cost

    @property
    def credits_left(self) -> int:
        return self.starting_pool - self._spent

    @property
    def credits_spent(self) -> int:
        return self._spent

    def is_affordable(self, round_no: int, cost: int, *, reserve: float = SAFE_STARTING_RESERVE) -> bool:
        """`True` iff spending `cost` now leaves at least `reserve` of the
        ORIGINAL pool in hand — a simple, conservative floor. Reasonable
        for the FIRST half of a duel; by round 8-10 you know your real
        remaining need better than a flat reserve does, and a `budget` job
        that only ever consults this without ever revisiting the reserve
        as rounds run out will end up over-cautious late, not over-spent —
        the safer of the two failure directions, but still a real
        one-line simplification worth outgrowing."""
        floor = self.starting_pool * reserve
        return (self.credits_left - cost) >= floor

    def bankrupt_by(self) -> int | None:
        """The first round number (1-indexed) at which `credits_left`
        actually went negative, or `None` if it never did. Used by this
        file's own `__main__` demo to make the module docstring's "bankrupt
        by round 3" claim a live, checked fact instead of an assertion in
        prose."""
        running = self.starting_pool
        for round_no in sorted(self._spent_by_round):
            running -= self._spent_by_round[round_no]
            if running < 0:
                return round_no
        return None


@dataclass(frozen=True, slots=True)
class ReplicaChoice:
    replica: str  # "w" | "c"
    reason: str


def pick_replica(*, path_id: str | None, known_drifting: bool, prefers_fresh: bool = True) -> ReplicaChoice:
    """A starting heuristic for JOB 1 (ROUTE) in `agent/gateway.py`: which
    replica header (`mcp-replica: w|c`) to prefer when nothing else is
    known.

    `known_drifting` is YOUR OWN judgement call, not something this
    function derives — a real implementation reads it from a
    `registry.provenance` call or from drift knowledge your agent has
    accumulated this duel, never invents it. `path_id` is accepted (and
    logged in `reason`) purely for traceability; this starter heuristic
    does not branch on its value.

    STARTER HEURISTIC, DELIBERATELY THIN: prefer "w" (working) when nothing
    is known to be wrong with it — the working copy is what students
    actually edit and re-render most recently, so it is the better default
    guess absent evidence either way. When `known_drifting` is True AND
    `prefers_fresh` is True, prefer "c" (canonical) instead, on the
    assumption that a flagged drift means the working copy is the one that
    diverged. THIS ASSUMPTION IS OFTEN WRONG — CORPUS-FACTS.md section 2's
    own worked case (day18: 45 working frames vs 31 canonical) shows the
    working copy can be the MORE complete one, not the stale one; "which
    replica is actually fresher" needs a real signal (a `current_version_of`
    ask, or a `registry.provenance` timestamp), not a coin flip on which
    letter comes first. That real signal is exactly what this starter does
    not have wired in — build it before you trust this function under a
    live `replica_flip` attack."""
    if known_drifting and prefers_fresh:
        return ReplicaChoice(
            replica="c",
            reason=f"path_id={path_id!r} is known to drift this duel; preferring canonical as the naive fresher guess",
        )
    return ReplicaChoice(replica="w", reason=f"path_id={path_id!r}: no known drift; default to working")


@dataclass
class ResultCache:
    """A per-duel memory of `(anchor, fields)` you have ALREADY PAID FOR —
    `agent/gateway.py`'s `Gateway` lives for the whole duel (CONTRACTS.md
    4.3), so this cache can too, and a hit here is a call your `budget` job
    never needs to forward at all.

    THE CAVEAT THAT MATTERS MORE THAN THE CACHE: a cached body is a
    snapshot from whenever you first fetched it. Under an active
    `replica_flip` or `poisoned_result` mutation (CONTRACTS.md section 8),
    the SAME anchor can legitimately answer differently on a later round.
    Treat a cache hit as "I already have grounds to say this, and I paid
    for them once" — never as "this is still true right now" without
    re-confirming when a round's attack card gives you a specific reason to
    doubt it. A cache that is trusted blindly is exactly how a `stale_read`
    (CONTRACTS.md 6.4) happens for free.

    Keys are `(anchor, tuple(sorted(fields)))` — the SAME anchor requested
    with a NARROWER mask than what's cached is still a genuine cache miss
    (you never paid for the field you'd be citing), which is why the key
    includes the mask, not just the anchor."""

    _store: dict[tuple[str, tuple[str, ...]], Mapping[str, Any]] = field(default_factory=dict)

    @staticmethod
    def _key(anchor: str, fields: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        return (anchor, tuple(sorted(fields)))

    def get(self, anchor: str, fields: tuple[str, ...]) -> Mapping[str, Any] | None:
        return self._store.get(self._key(anchor, fields))

    def put(self, anchor: str, fields: tuple[str, ...], row: Mapping[str, Any]) -> None:
        self._store[self._key(anchor, fields)] = dict(row)

    def __len__(self) -> int:
        return len(self._store)


def should_delegate(
    *,
    own_confidence: float,
    calls_used_this_window: int,
    calls_allowed_this_window: int,
    credits_left: int,
    delegate_cost: int,
    min_confidence_to_skip: float = 0.85,
) -> bool:
    """JOB-neutral heuristic for "is an A2A verifier (e.g.
    `citation-checker.verify_source`, rate-limited 2-per-3-rounds per
    CONTRACTS.md section 4.2 mechanic 5) worth its cost RIGHT NOW". Three
    gates, all must pass:

      1. You are not already confident enough to skip it
         (`own_confidence < min_confidence_to_skip`) — delegating when you
         are already sure is the exact `wasteful` pattern the per-tool
         rate window (mechanic 5) exists to make you ration.
      2. The rate window has room left this duel
         (`calls_used_this_window < calls_allowed_this_window`) — a call
         that would just come back `rate_limited` is worse than skipping
         it: it still costs credits (CONTRACTS.md 3.3: `rate_limited` is
         "yes, no refund") for zero information.
      3. You can afford it without breaking your own reserve
         (`credits_left >= delegate_cost`, checked bare here — combine with
         `BudgetPacer.is_affordable` for the full picture including your
         reserve floor).

    STARTER HEURISTIC: `min_confidence_to_skip=0.85` is a placeholder
    threshold, not a tuned one — `own_confidence` itself is not computed by
    anything in this file; it is whatever your own reasoning (guided by
    `agent/prompt.md`'s citation contract) decides it is. Wire a real
    confidence signal in before you trust this gate under pressure."""
    if own_confidence >= min_confidence_to_skip:
        return False
    if calls_used_this_window >= calls_allowed_this_window:
        return False
    if credits_left < delegate_cost:
        return False
    return True


if __name__ == "__main__":
    print("=== agent.strategy: the round-cost arithmetic, computed live ===\n")
    disciplined = disciplined_round_cost()
    careless = careless_round_cost()
    print(f"  disciplined_round_cost() = {disciplined} cr  (FINAL-PLAN.md 4.3: '8-11')")
    print(f"  careless_round_cost()    = {careless} cr  (FINAL-PLAN.md 4.3: '~49')")
    assert 8 <= disciplined <= 11, disciplined
    assert careless >= 45, careless
    print(f"  kit.mcp.specs available: {_SPECS_AVAILABLE}")

    print("\n=== is_catalog_trap / cheap_mask / successor_of ===\n")
    assert is_catalog_trap("registry", "list_servers", ()) is True
    assert is_catalog_trap("registry", "list_servers", ("name",)) is False
    assert is_catalog_trap("slides", "get_frame", ()) is False
    print("  registry.list_servers with no mask -> catalog trap: True")
    print("  registry.list_servers with fields=(name,) -> catalog trap: False")

    mask = cheap_mask("slides", "get_frame", ("title", "title", "body"))
    print(f"  cheap_mask('slides','get_frame', ('title','title','body')) -> {mask}")
    assert mask == ("body", "title")

    succ = successor_of("slides", "search")
    print(f"  successor_of('slides','search') -> {succ}")
    assert succ == ("slides", "query")
    assert successor_of("slides", "query") is None

    print("\n=== NARROW_MASKS: every entry is a mask the real economy accepts ===\n")
    if _SPECS_AVAILABLE:
        for (srv, tl), msk in sorted(NARROW_MASKS.items()):
            spec = TOOL_SPECS.get((srv, tl))
            assert spec is not None, f"NARROW_MASKS names an unknown tool: {srv}.{tl}"
            bogus = [f for f in msk if f not in spec.all_fields]
            assert not bogus, f"{srv}.{tl}: {bogus} are not fields of this tool"
        print(f"  all {len(NARROW_MASKS)} narrow masks validate against TOOL_SPECS.all_fields")
        # The two punishment buttons, priced both ways — the single most
        # valuable number in this file.
        for srv, tl in sorted(CATALOG_TRAP_TOOLS):
            full = estimated_cost(srv, tl, ())
            narrow = estimated_cost(srv, tl, NARROW_MASKS[(srv, tl)])
            print(f"  {srv}.{tl}: default {full:>2} cr -> narrow {NARROW_MASKS[(srv, tl)]} {narrow:>2} cr")
            assert narrow < full, (srv, tl, narrow, full)
            assert narrow <= 3, (srv, tl, narrow)
    else:
        print("  kit.mcp.specs unavailable — mask validation skipped (degraded mode)")

    print("\n=== narrow_mask / estimated_cost: neither may ever raise ===\n")
    # 1. no mask named -> this team's narrow default, never the tool's own
    assert narrow_mask("registry", "list_servers", ()) == ("name",)
    assert narrow_mask("registry", "list_servers", ("*",)) == ("name",)
    # 2. a real mask the model chose is respected, not second-guessed
    assert narrow_mask("slides", "query", ("body", "title")) == ("body", "title")
    # 3. a bogus field is dropped rather than priced (cost_of would KeyError)
    assert narrow_mask("slides", "query", ("title", "anchor")) == ("title",)
    # 4. an ALL-bogus mask falls back rather than degrading to the tool default
    assert narrow_mask("slides", "query", ("nope",)) == ("title",)
    print(f"  narrow_mask('slides','query',('title','anchor')) -> {narrow_mask('slides', 'query', ('title', 'anchor'))}"
          f"   ('anchor' is not a slides.query field)")
    for bad in (("registry", "no_such_tool", ()), ("slides", "query", ("anchor",)), ("", "", ("*",))):
        got = estimated_cost(*bad)
        print(f"  estimated_cost{bad} -> {got} cr (no exception)")
        assert isinstance(got, int) and got >= 0

    print("\n=== reserve_for_round: the flat 0.5 reserve, made round-aware ===\n")
    for r in (1, 5, 8, 10):
        print(f"  round {r:>2} -> reserve {reserve_for_round(r):.2f} of the pool")
    assert reserve_for_round(1) == 0.45
    assert reserve_for_round(10) == 0.0
    assert reserve_for_round(1) > reserve_for_round(5) > reserve_for_round(10)
    # Nonsense round numbers are clamped, never raised on.
    assert 0.0 <= reserve_for_round(-3) <= 1.0 and 0.0 <= reserve_for_round(99) <= 1.0
    assert sum(ROUND_ALLOWANCE.values()) == 100, sum(ROUND_ALLOWANCE.values())
    assert ROUND_ALLOWANCE[10] > ROUND_ALLOWANCE[1], "late rounds are worth 1.5x — budget for them"

    print("\n=== BudgetPacer: disciplined-at-the-CEILING barely lasts the duel; careless does not ===\n")
    disciplined_pacer = BudgetPacer()
    for round_no in range(1, ROUNDS_PER_DUEL + 1):
        disciplined_pacer.record_spend(round_no, disciplined)
    print(
        f"  disciplined (ceiling, {disciplined}cr) x10 rounds -> spent={disciplined_pacer.credits_spent} "
        f"credits_left={disciplined_pacer.credits_left} bankrupt_by={disciplined_pacer.bankrupt_by()}"
    )
    # The CEILING of "disciplined" (paying full price for query + get_frame +
    # provenance, EVERY round, with no caching at all) finishes all ten rounds
    # with a single-digit margin -- a sharp contrast with careless play below,
    # and the honest reason ResultCache/pacing still exist: that margin does
    # not absorb one catalog read (12 cr), let alone two.
    assert disciplined_pacer.bankrupt_by() is None, disciplined_pacer.bankrupt_by()
    assert 0 < disciplined_pacer.credits_left <= 12, disciplined_pacer.credits_left
    one_catalog_read = BudgetPacer()
    for round_no in range(1, ROUNDS_PER_DUEL + 1):
        one_catalog_read.record_spend(round_no, disciplined)
    one_catalog_read.record_spend(ROUNDS_PER_DUEL, estimated_cost("registry", "list_servers", ()))
    print(f"  ...plus ONE default-mask registry.list_servers -> credits_left="
          f"{one_catalog_read.credits_left} bankrupt_by={one_catalog_read.bankrupt_by()}")
    assert one_catalog_read.credits_left < 0, "one catalog read must be enough to break the margin"

    careless_pacer = BudgetPacer()
    bankrupt_round = None
    for round_no in range(1, ROUNDS_PER_DUEL + 1):
        careless_pacer.record_spend(round_no, careless)
        if bankrupt_round is None and careless_pacer.credits_left < 0:
            bankrupt_round = round_no
    print(
        f"  careless per round -> spent={careless_pacer.credits_spent} "
        f"credits_left={careless_pacer.credits_left} bankrupt_by={careless_pacer.bankrupt_by()}"
    )
    assert careless_pacer.bankrupt_by() == bankrupt_round
    assert careless_pacer.bankrupt_by() <= 3, careless_pacer.bankrupt_by()

    print("\n=== BudgetPacer.is_affordable: the reserve floor ===\n")
    mid_pacer = BudgetPacer()
    mid_pacer.record_spend(1, 60)
    print(f"  after spending 60/100, credits_left={mid_pacer.credits_left}")
    assert mid_pacer.is_affordable(2, 5) is False  # would drop below the 50-credit reserve floor
    assert mid_pacer.is_affordable(2, -20) is True  # nonsense cost, but arithmetic still holds
    fresh_pacer = BudgetPacer()
    assert fresh_pacer.is_affordable(1, disciplined) is True

    print("\n=== BudgetPacer + reserve_for_round: pacing that stops being timid late ===\n")
    # The failure the flat reserve produces, shown side by side: 30 credits
    # left in round 9 is ample for an 11-credit round with only two rounds to
    # pay for, but a flat 0.5 floor refuses it — in exactly the rounds where a
    # credit prevents 1.5x the damage it would have prevented in round 2.
    late = BudgetPacer()
    late.record_spend(1, 70)
    flat = late.is_affordable(9, 11)
    aware = late.is_affordable(9, 11, reserve=reserve_for_round(9))
    print(f"  round 9, {late.credits_left} cr left, an 11 cr round:")
    print(f"    flat reserve  (0.50) -> affordable={flat}   <- over-cautious, and it costs 1.5x")
    print(f"    round-aware   ({reserve_for_round(9):.2f}) -> affordable={aware}")
    assert flat is False and aware is True
    # ...and it is still not a licence to be careless: the round-aware floor
    # refuses a CARELESS round even in round 9.
    assert late.is_affordable(9, careless, reserve=reserve_for_round(9)) is False

    print("\n=== pick_replica: the naive heuristic, and why it is naive ===\n")
    choice_clean = pick_replica(path_id="d8f95a7b", known_drifting=False)
    choice_drifting = pick_replica(path_id="d8f95a7b", known_drifting=True)
    print(f"  known_drifting=False -> {choice_clean}")
    print(f"  known_drifting=True  -> {choice_drifting}")
    assert choice_clean.replica == "w"
    assert choice_drifting.replica == "c"

    print("\n=== ResultCache: same (anchor, fields) is a hit; a wider mask is a genuine miss ===\n")
    cache = ResultCache()
    anchor = "Frame:3f2a9c11/w/041"
    assert cache.get(anchor, ("title", "body")) is None
    cache.put(anchor, ("title", "body"), {"title": "Streamable HTTP", "body": "..."})
    hit = cache.get(anchor, ("body", "title"))  # order-insensitive, same key
    print(f"  cache.get(anchor, ('body','title')) after put(('title','body')) -> {hit}")
    assert hit == {"title": "Streamable HTTP", "body": "..."}
    miss = cache.get(anchor, ("title", "body", "meta"))
    print(f"  cache.get(anchor, wider mask incl. 'meta') -> {miss}  (a real miss, not stale data)")
    assert miss is None
    assert len(cache) == 1

    print("\n=== should_delegate: three gates, all must pass ===\n")
    cases = [
        dict(own_confidence=0.4, calls_used_this_window=0, calls_allowed_this_window=2, credits_left=50, delegate_cost=6),
        dict(own_confidence=0.95, calls_used_this_window=0, calls_allowed_this_window=2, credits_left=50, delegate_cost=6),
        dict(own_confidence=0.4, calls_used_this_window=2, calls_allowed_this_window=2, credits_left=50, delegate_cost=6),
        dict(own_confidence=0.4, calls_used_this_window=0, calls_allowed_this_window=2, credits_left=3, delegate_cost=6),
    ]
    expected = [True, False, False, False]
    for case, want in zip(cases, expected):
        got = should_delegate(**case)
        print(f"  should_delegate({case}) -> {got}")
        assert got == want, (case, got, want)

    print("\nAll agent/strategy.py demos passed.")
