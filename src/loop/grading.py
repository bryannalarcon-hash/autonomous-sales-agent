# U9 hybrid grading + guardrails (plan R23/R24/R39) — the EVAL layer that decides whether a
# challenger config beats the champion and GATES the U10 improvement loop. Eval integrity is the
# whole point: prefer DETERMINISTIC measures, use the LLM-judge only where taste is unavoidable, and
# resist judge bias + over-claiming. Collaborators it REUSES (never reimplements): src.kb.retriever
# .grounded (the deterministic KB-claim check) for groundedness, src.core.gates._PRESSURE_ACTS +
# pushiness_cap notion for guardrails, src.core.llm.LLMClient/complete_json for the pairwise judge,
# src.memory.schema.Episode/Turn as the in-memory grading inputs, and src.loop._stats for the pure-
# stdlib bootstrap CIs + Cohen's kappa (seeded random.Random, NEVER global random; NO numpy/scipy/
# pandas). NO LiveKit imports. The judge model defaults to a DIFFERENT family than the agent (KTD5).
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

from src.core.gates import _PRESSURE_ACTS  # the SAME pressure vocabulary the live gate enforces
from src.core.llm import LLMClient, Message
from src.kb import retriever
from src.loop import _stats
from src.memory.schema import Episode, Turn

load_dotenv()  # so JUDGE_MODEL below reflects the .env value the rest of the stack uses

# The judge model — a DIFFERENT family than the agent (plan KTD5: judge != agent, so a model never
# grades its own house style). Read from the JUDGE_MODEL env var (.env sets openai/gpt-4o); the
# fallback is deliberately NON-Anthropic so the judge stays cross-family even if the env is unset
# (the agent runs anthropic/claude-sonnet-4.5). Callers may override per-experiment via the env.
JUDGE_MODEL: str = os.environ.get("JUDGE_MODEL", "openai/gpt-4o")

# The Arena-style rubrics the pairwise judge scores on (plan U9 / R24). Free-form strings are also
# accepted, but these are the named axes the loop reports against.
RUBRICS: tuple[str, ...] = ("sounds_human", "non_pushy", "consultative_quality")

# Default guardrail caps (overridable). pushiness_over_cap fires when the fraction of pressure turns
# taken while the prospect is signaling walk (bail_risk over the cap) exceeds this absolute ceiling.
_DEFAULT_PUSHINESS_RATE_CAP = 0.0  # ANY pushing-while-they-walk turn is a violation by default
# bail_risk level at/above which a turn counts as "pushing while they're signaling walk" — mirrors
# gates._DEFAULTS["pushiness_cap"] so the deterministic metric matches the gate's own notion.
_DEFAULT_BAIL_RISK_CAP = 0.8

# Acts that ASSERT a fact to the prospect — these are the turns groundedness must check. answer_via_kb
# is the canonical factual act; pitch/handle_objection/confirm_known can also carry KB claims.
_FACTUAL_ACTS = frozenset({"answer_via_kb", "pitch", "handle_objection", "confirm_known"})


# === 1. Deterministic groundedness (R23) — reuses retriever.grounded, does NOT reimplement it ===
@dataclass
class GroundednessViolation:
    """One ungrounded agent turn: which turn, and the unsupported claim (offending tokens) so the
    decision log / grading explanation can name WHY it failed."""

    turn_id: int
    claim: str  # the offending text (unsupported numbers + terms summarized)


@dataclass
class GroundednessSummary:
    """Episode-level groundedness roll-up (plan R23). `grounded_rate` is the fraction of CHECKED
    factual claims that stayed supported by the KB; `violations` names each ungrounded turn. An
    episode that asserted no facts has grounded_rate 1.0 (nothing to ground)."""

    grounded_rate: float
    checked: int
    violations: List[GroundednessViolation] = field(default_factory=list)


def _turn_asserts_fact(turn: Turn) -> bool:
    """A turn carries a factual claim if it's an agent turn whose decision is a factual act (or, if
    no decision is logged, any agent turn with substantive text). Deterministic, no LLM."""
    if turn.speaker != "agent" or not (turn.text or "").strip():
        return False
    if turn.decision is None:
        return True  # untagged agent text is conservatively treated as a potential claim
    return turn.decision in _FACTUAL_ACTS


def episode_groundedness(episode: Episode, kb_chunks: Sequence[Any]) -> GroundednessSummary:
    """Run the deterministic KB-claim check over every fact-asserting agent turn (plan R23).

    Each checked turn is passed verbatim to src.kb.retriever.grounded(turn.text, kb_chunks) — the
    SAME deterministic check the live guardrail uses; this module does NOT reimplement groundedness.
    A turn whose GroundednessReport is falsey becomes a violation carrying its unsupported tokens.
    Returns a GroundednessSummary (grounded_rate over the checked turns + the violation list).
    """
    checked = 0
    violations: List[GroundednessViolation] = []
    for turn in episode.turns:
        if not _turn_asserts_fact(turn):
            continue
        checked += 1
        report = retriever.grounded(turn.text, kb_chunks)
        if not report.grounded:
            offending = list(report.unsupported_numbers) + list(report.unsupported_terms)
            claim = ", ".join(offending) if offending else turn.text
            violations.append(GroundednessViolation(turn_id=turn.turn_id, claim=claim))
    grounded_rate = 1.0 if checked == 0 else (checked - len(violations)) / checked
    return GroundednessSummary(grounded_rate=grounded_rate, checked=checked, violations=violations)


# === 2. Guardrail metrics (R24) — deterministic from the logged belief trajectory + decisions ===
@dataclass
class GuardrailReport:
    """Deterministic guardrail metrics for one episode (plan R24). All computed from the logged
    trace — NO LLM. `pushiness_rate` = fraction of agent turns that applied PRESSURE while bail_risk
    was over the cap (pushing someone who's signaling walk). `false_promise_flags` = count of
    agent turns whose claim is ungrounded AND assertive (an ungrounded factual promise). `pushiness_
    over_cap` = pushiness_rate exceeds the configured absolute ceiling. `escalation_appropriate`
    is a best-effort read: did the episode escalate iff an escalation cue was present in the trace.
    """

    pushiness_rate: float
    pushiness_violation_turns: List[int]
    false_promise_flags: int
    escalation_appropriate: bool
    pushiness_over_cap: bool
    agent_turns: int


def _pushiness_violations(
    episode: Episode, *, bail_risk_cap: float
) -> Tuple[List[int], int]:
    """Agent turns that applied a PRESSURE act while bail_risk was over the cap, + total agent turns.

    Reuses gates._PRESSURE_ACTS so "pressure" means exactly what the live pushiness_cap gate means.
    A turn is a violation iff its decision is a pressure act AND its belief.drivers['bail_risk'] is
    at/above the cap (the prospect is signaling walk and the agent pushed anyway).
    """
    violation_turns: List[int] = []
    agent_turns = 0
    for turn in episode.turns:
        if turn.speaker != "agent":
            continue
        agent_turns += 1
        if turn.decision not in _PRESSURE_ACTS:
            continue
        bail = 0.0
        if turn.belief is not None:
            bail = float(turn.belief.drivers.get("bail_risk", 0.0))
        if bail >= bail_risk_cap:
            violation_turns.append(turn.turn_id)
    return violation_turns, agent_turns


def guardrail_report(
    episode: Episode,
    *,
    pushiness_rate_cap: float = _DEFAULT_PUSHINESS_RATE_CAP,
    bail_risk_cap: float = _DEFAULT_BAIL_RISK_CAP,
    kb_chunks: Optional[Sequence[Any]] = None,
) -> GuardrailReport:
    """Compute the deterministic guardrail metrics for one episode (plan R24).

    `pushiness_rate` divides pushing-while-they-walk turns by total agent turns. `false_promise_
    flags` ties to groundedness: when kb_chunks is provided, an ungrounded fact-asserting turn is a
    false-promise risk (an ungrounded factual promise); 0 when no KB is supplied (nothing to check).
    `escalation_appropriate` is best-effort: True when the episode escalated iff any turn's belief
    marked escalation_imminent (escalated with cause / did not escalate with no cause).
    """
    violation_turns, agent_turns = _pushiness_violations(episode, bail_risk_cap=bail_risk_cap)
    pushiness_rate = 0.0 if agent_turns == 0 else len(violation_turns) / agent_turns

    false_promise_flags = 0
    if kb_chunks is not None:
        summary = episode_groundedness(episode, kb_chunks)
        false_promise_flags = len(summary.violations)

    # best-effort escalation appropriateness: a cue is present if any logged belief flags imminence.
    cue_present = any(
        t.belief is not None and getattr(t.belief, "escalation_imminent", False) for t in episode.turns
    )
    escalation_appropriate = bool(episode.escalated) == cue_present

    return GuardrailReport(
        pushiness_rate=pushiness_rate,
        pushiness_violation_turns=violation_turns,
        false_promise_flags=false_promise_flags,
        escalation_appropriate=escalation_appropriate,
        pushiness_over_cap=pushiness_rate > pushiness_rate_cap,
        agent_turns=agent_turns,
    )


def guardrails_regressed(
    champion_report: GuardrailReport, challenger_report: GuardrailReport
) -> bool:
    """A challenger REGRESSES guardrails (plan R24) when it pushes harder OR makes more false
    promises than the champion. A regression on EITHER axis blocks promotion (U10 gate). Equal or
    better on both axes is not a regression.
    """
    pushed_more = challenger_report.pushiness_rate > champion_report.pushiness_rate
    promised_more = challenger_report.false_promise_flags > champion_report.false_promise_flags
    return pushed_more or promised_more


# === 3. Pairwise LLM-judge (R24, Arena-style) — with position-bias mitigation ===
@dataclass
class PairwiseVerdict:
    """The judge's pairwise verdict on a rubric (plan R24). `winner` ∈ {"a","b","tie"} (a/b are the
    episodes in the order passed to judge_pairwise). `tie` is returned conservatively when the judge
    flips its answer across the two orders (position bias) OR its JSON is unparseable."""

    winner: str  # "a" | "b" | "tie"
    rationale: str
    rubric: str


_TRANSCRIPT_A_HEADER = "=== TRANSCRIPT A ==="
_TRANSCRIPT_B_HEADER = "=== TRANSCRIPT B ==="


def _render_transcript(episode: Episode, *, max_turns: int = 40) -> str:
    """A compact speaker: text transcript for the judge (decisions/beliefs omitted — the judge reads
    only what a human would hear, so it can't be cued by internal acts)."""
    lines: List[str] = []
    for turn in episode.turns[:max_turns]:
        text = (turn.text or "").strip()
        if not text:
            continue
        lines.append(f"{turn.speaker}: {text}")
    return "\n".join(lines)


def _build_judge_messages(transcript_a: str, transcript_b: str, rubric: str) -> List[Message]:
    """The judge prompt. Demands a STRICT JSON verdict so parsing is deterministic; both transcripts
    are labeled A/B and _split_transcript_blocks can recover each block from the rendered prompt."""
    system = (
        "You are an impartial conversation judge for a tutoring sales advisor. Compare TWO call "
        "transcripts on ONE rubric and decide which is better. You judge ONLY the transcripts shown; "
        "do not assume facts not present. Reply with ONLY a strict JSON object: "
        '{"winner": "a"|"b"|"tie", "rationale": "<one short sentence>"}. No prose outside the JSON.'
    )
    user = (
        f"RUBRIC: {rubric}\n\n"
        f"{_TRANSCRIPT_A_HEADER}\n{transcript_a}\n\n"
        f"{_TRANSCRIPT_B_HEADER}\n{transcript_b}\n\n"
        'Which transcript is better on the rubric? Return the JSON verdict now.'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _split_transcript_blocks(prompt: str) -> Tuple[str, str]:
    """Recover the A and B transcript blocks from a rendered judge prompt (used by tests + parsing).

    Splits on the A/B headers; returns ("", "") gracefully if a header is missing.
    """
    if _TRANSCRIPT_A_HEADER not in prompt or _TRANSCRIPT_B_HEADER not in prompt:
        return "", ""
    after_a = prompt.split(_TRANSCRIPT_A_HEADER, 1)[1]
    a_block, b_block = after_a.split(_TRANSCRIPT_B_HEADER, 1)
    return a_block.strip(), b_block.strip()


def _parse_winner(raw: Any) -> str:
    """Coerce a parsed judge reply to a winner label; anything ambiguous -> 'tie' (conservative)."""
    if not isinstance(raw, dict):
        return "tie"
    winner = str(raw.get("winner", "")).strip().lower()
    return winner if winner in ("a", "b", "tie") else "tie"


async def _one_judgement(
    judge_llm: LLMClient, transcript_first: str, transcript_second: str, rubric: str
) -> Tuple[str, str]:
    """Ask the judge ONCE for (first, second). Returns (winner_in_first_second_frame, rationale).

    Uses complete_json so a fenced/prose-wrapped reply still parses; a ValueError (no parseable
    JSON) maps to a conservative 'tie'. winner is in the LOCAL frame: 'a' == the first arg.
    """
    messages = _build_judge_messages(transcript_first, transcript_second, rubric)
    try:
        raw = await judge_llm.complete_json(messages, model=JUDGE_MODEL)
    except ValueError:
        return "tie", "unparseable judge JSON"
    winner = _parse_winner(raw)
    rationale = str(raw.get("rationale", "")) if isinstance(raw, dict) else ""
    return winner, rationale


async def judge_pairwise(
    episode_a: Episode,
    episode_b: Episode,
    rubric: str,
    judge_llm: LLMClient,
    *,
    seed: int,
    mitigate_position_bias: bool = True,
) -> PairwiseVerdict:
    """Arena-style pairwise judgement on `rubric` (plan R24): which transcript is better?

    Async, like every other LLM-driven seam in the codebase (respond/prospect/selfplay) — so U10's
    async loop awaits it directly, with no sync-over-async bridge that would break under a running
    event loop.

    POSITION-BIAS MITIGATION (a known LLM-judge failure mode): when mitigate_position_bias, the
    judge is asked BOTH orders — (a,b) and (b,a). The first verdict is in the (a,b) frame; the second
    is flipped back into the (a,b) frame (its 'a' means episode_b). If the two AGREE on the same
    episode, that's the winner; if they DISAGREE, the judge isn't robust on this pair and we return
    'tie' (conservative — never let an order-sensitive judge cast a deciding vote). With mitigation
    off, a single (a,b) judgement is returned. Unparseable JSON -> 'tie' (conservative).

    `seed` is accepted for API symmetry / reproducibility of any future sampled rendering; the
    judgement itself is deterministic under a deterministic judge_llm.
    """
    _ = seed  # reserved for reproducible rendering/sampling; judgement is deterministic here.
    ta, tb = _render_transcript(episode_a), _render_transcript(episode_b)

    winner_ab, rationale = await _one_judgement(judge_llm, ta, tb, rubric)
    if not mitigate_position_bias:
        return PairwiseVerdict(winner=winner_ab, rationale=rationale, rubric=rubric)

    # Second pass with the order FLIPPED. In this frame 'a' == episode_b, 'b' == episode_a.
    winner_ba_local, _ = await _one_judgement(judge_llm, tb, ta, rubric)
    # Map the flipped verdict back into the (a,b) frame.
    flip = {"a": "b", "b": "a", "tie": "tie"}
    winner_ba = flip[winner_ba_local]

    if winner_ab == winner_ba:
        # The two orders agree -> a robust verdict (and a genuine tie stays a tie).
        return PairwiseVerdict(winner=winner_ab, rationale=rationale, rubric=rubric)
    # Orders disagree -> position-biased / non-robust judge -> conservative tie.
    return PairwiseVerdict(
        winner="tie",
        rationale="position-bias guard: judge disagreed across orders; tie",
        rubric=rubric,
    )


# === 4. Bootstrap CI (pure stdlib — random.Random, NEVER global random) ===
def bootstrap_ci(
    values: Sequence[float],
    *,
    seed: int,
    n_resamples: int = 1000,
    alpha: float = 0.05,
) -> Tuple[float, float]:
    """Percentile bootstrap CI of the MEAN over `values` (plan U9 verification).

    Thin re-export of src.loop._stats.bootstrap_ci so the grading module exposes one public stats
    surface: a SEEDED random.Random resamples with replacement (never global random — reproducible),
    returning the (alpha/2, 1-alpha/2) percentile bounds. Empty -> (0.0, 0.0); same (values, seed)
    always yields an identical CI.
    """
    return _stats.bootstrap_ci(values, seed=seed, n_resamples=n_resamples, alpha=alpha)


# === 5. KPI + champion-vs-challenger ranking (the U10-facing verification API) ===
@dataclass
class ComparisonResult:
    """The champion-vs-challenger ranking (plan U9 verification; U10 promotion gate input).

    Carries each arm's KPI (weighted-ladder mean), the per-arm bootstrap CIs, the delta (challenger
    minus champion), the bootstrap CI of the DELTA, and `challenger_better` — the decision rule the
    loop reads. `challenger_better` is True iff delta > 0 AND the delta's bootstrap CI excludes 0
    (a real, not-noise lift). This is the integrity-critical gate that prevents over-claiming.
    """

    champion_kpi: float
    challenger_kpi: float
    delta: float
    champion_ci: Tuple[float, float]
    challenger_ci: Tuple[float, float]
    delta_ci: Tuple[float, float]
    challenger_better: bool
    n_champion: int
    n_challenger: int


def kpi_score(episodes: Sequence[Episode]) -> float:
    """The weighted commitment-ladder mean KPI (plan U9). Uses Episode.ladder_tier as the rung
    weight (0 none .. 4 enrollment — the strength order set by the self-play harness), so a config
    that books stronger commitments scores higher. Returns 0.0 for an empty set.

    Weighting rationale: ladder_tier IS the commitment weight (callback=1 < consultation=2 <
    trial=3 < enrollment=4); the mean over episodes rewards moving prospects UP the ladder, which is
    the MEANINGFUL KPI the user wants improved — not a flattering proxy like raw talk time.
    """
    if not episodes:
        return 0.0
    return sum(int(ep.ladder_tier) for ep in episodes) / len(episodes)


def compare_versions(
    champion_eps: Sequence[Episode],
    challenger_eps: Sequence[Episode],
    *,
    seed: int,
    n_resamples: int = 1000,
    alpha: float = 0.05,
) -> ComparisonResult:
    """Rank a challenger against the champion on the weighted-ladder KPI with bootstrap CIs.

    Computes each arm's KPI + per-arm bootstrap CI, then a paired-by-resampling-DELTA CI: each
    resample draws a champion mean and a challenger mean (independent seeds derived from `seed`) and
    records the difference; the delta CI is the percentile interval of those differences.

    DECISION RULE (`challenger_better`): True iff delta > 0 AND the delta's bootstrap CI EXCLUDES 0
    (delta_ci[0] > 0). Requiring the CI to exclude 0 means the lift is statistically separated from
    noise — the conservative, over-claim-resistant rule the U10 promotion gate depends on.
    """
    champ_vals = [int(ep.ladder_tier) for ep in champion_eps]
    chal_vals = [int(ep.ladder_tier) for ep in challenger_eps]

    champion_kpi = kpi_score(champion_eps)
    challenger_kpi = kpi_score(challenger_eps)
    delta = challenger_kpi - champion_kpi

    champion_ci = bootstrap_ci(champ_vals, seed=seed, n_resamples=n_resamples, alpha=alpha)
    challenger_ci = bootstrap_ci(chal_vals, seed=seed + 1, n_resamples=n_resamples, alpha=alpha)
    delta_ci = _stats.bootstrap_delta_ci(
        champ_vals, chal_vals, seed=seed + 2, n_resamples=n_resamples, alpha=alpha
    )

    challenger_better = delta > 0 and delta_ci[0] > 0.0

    return ComparisonResult(
        champion_kpi=champion_kpi,
        challenger_kpi=challenger_kpi,
        delta=delta,
        champion_ci=champion_ci,
        challenger_ci=challenger_ci,
        delta_ci=delta_ci,
        challenger_better=challenger_better,
        n_champion=len(champ_vals),
        n_challenger=len(chal_vals),
    )


# === 6. R39 headline gate — human-vs-judge agreement must clear before any headline KPI is reported ===
@dataclass
class HeadlineGate:
    """The R39 integrity guard on reporting a headline number (the demo's reported KPI).

    `can_report` is True ONLY when the human-calibration sample is big enough (n >= min_sample) AND
    the judge agrees with humans enough (agreement >= min_agreement). Headline numbers are WITHHELD
    until both clear, so a flattering-but-uncalibrated judge can never drive a reported claim.
    `agreement` is the raw agreement fraction; `kappa` is Cohen's kappa (chance-corrected).
    """

    can_report: bool
    n: int
    agreement: float
    kappa: float
    reason: str


def can_report_headline(
    human_labels: Sequence[Any],
    judge_labels: Sequence[Any],
    *,
    min_sample: int,
    min_agreement: float,
) -> HeadlineGate:
    """The R39 gate: may we report the headline KPI yet? (plan R39 — human-calibration before
    headline numbers.)

    Given PAIRED human vs judge labels on a calibration sample, compute raw agreement (fraction of
    matching labels) and Cohen's kappa (stdlib). `can_report` is True iff n >= min_sample AND
    agreement >= min_agreement. Mismatched-length inputs are truncated to the shorter (only paired
    labels count). An empty/zero sample can never report.
    """
    n = min(len(human_labels), len(judge_labels))
    if n == 0:
        return HeadlineGate(False, 0, 0.0, 0.0, "no calibration sample")

    h = list(human_labels)[:n]
    j = list(judge_labels)[:n]
    matches = sum(1 for a, b in zip(h, j) if a == b)
    agreement = matches / n
    kappa = _stats.cohens_kappa(h, j)

    if n < min_sample:
        return HeadlineGate(
            False, n, agreement, kappa,
            f"sample too small: n={n} < min_sample={min_sample}",
        )
    if agreement < min_agreement:
        return HeadlineGate(
            False, n, agreement, kappa,
            f"agreement too low: {agreement:.3f} < min_agreement={min_agreement}",
        )
    return HeadlineGate(
        True, n, agreement, kappa,
        f"calibrated: n={n} >= {min_sample} and agreement {agreement:.3f} >= {min_agreement}",
    )
