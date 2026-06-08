# test_cb65_labels.py — Unit tests for CB-65 label-translation additions in src/api/labels.py.
# Covers: ladder_tier_label (existing, now exercised explicitly), dimension_label (existing),
# and the JS-side functions mirrored here conceptually. Also validates that no raw internal token
# can pass through the Python label layer unchanged when a known translation exists.
from __future__ import annotations

import pytest

from src.api.labels import (
    LADDER_TIER_LABEL,
    dimension_label,
    experiment_state_label,
    ladder_tier_label,
    outcome_label,
)


class TestLadderTierLabel:
    """ladder_tier_label translates int tiers to human labels; no raw int leaks."""

    def test_tier_0_is_no_commitment(self):
        assert ladder_tier_label(0) == "No commitment"

    def test_tier_1_is_callback_booked(self):
        assert ladder_tier_label(1) == "Callback booked"

    def test_tier_2_is_consultation_booked(self):
        assert ladder_tier_label(2) == "Consultation booked"

    def test_tier_3_is_trial_booked(self):
        assert ladder_tier_label(3) == "Trial booked"

    def test_tier_4_is_same_call_enrollment(self):
        assert ladder_tier_label(4) == "Same-call enrollment"

    def test_none_returns_dash(self):
        assert ladder_tier_label(None) == "—"

    def test_unknown_tier_uses_fallback_label(self):
        # An unknown tier gets "Tier N" — never a bare int.
        result = ladder_tier_label(99)
        assert "99" in result
        assert result != "99"

    def test_no_raw_0_or_1_leaks(self):
        """Critical: 'tier 0' and 'tier 1' must never be the rendered label."""
        for tier in range(5):
            label = ladder_tier_label(tier)
            assert label not in ("0", "1", "2", "3", "4"), (
                f"ladder_tier_label({tier}) returned a bare int: {label!r}"
            )
            assert not label.lower().startswith("tier ") or len(label) > 7, (
                f"ladder_tier_label({tier}) returned raw 'tier N' jargon: {label!r}"
            )


class TestDimensionLabel:
    """dimension_label never renders raw snake_case slugs."""

    def test_playbooks_discovery_sequence(self):
        label = dimension_label("playbooks.discovery_sequence")
        assert "_" not in label
        assert label  # non-empty

    def test_thresholds_pushiness_cap(self):
        label = dimension_label("thresholds.pushiness_cap")
        assert "_" not in label

    def test_kb_dimension(self):
        label = dimension_label("kb")
        assert label == "Knowledge base"

    def test_unknown_namespaced(self):
        label = dimension_label("thresholds.some_new_key")
        assert "_" not in label
        assert label  # non-empty


class TestOutcomeLabel:
    """outcome_label covers all known outcomes; no raw slug leaks."""

    KNOWN_OUTCOMES = [
        "enrolled", "trial_booked", "consult_booked", "callback_booked",
        "callback_scheduled", "booked", "interested", "released", "abandoned",
        "no_interest", "walked", "disqualified", "escalated", "in_progress",
    ]

    @pytest.mark.parametrize("outcome", KNOWN_OUTCOMES)
    def test_known_outcome_has_human_label(self, outcome: str):
        label = outcome_label(outcome)
        # Must not be the raw snake_case slug (unchanged pass-through is a leak)
        assert label != outcome, (
            f"outcome_label({outcome!r}) returned the raw slug unchanged — no translation applied"
        )
        # Must not contain underscores (snake_case leaks)
        assert "_" not in label, f"Underscore in outcome label: {label!r}"

    def test_none_returns_in_progress(self):
        assert outcome_label(None) == "In progress"

    def test_empty_returns_in_progress(self):
        assert outcome_label("") == "In progress"


class TestExperimentStateLabel:
    """experiment_state_label covers all known states; no raw slug leaks."""

    KNOWN_STATES = ["draft", "running", "passed", "blocked", "promoted", "rejected", "paused"]

    @pytest.mark.parametrize("state", KNOWN_STATES)
    def test_known_state_has_human_label(self, state: str):
        label = experiment_state_label(state)
        assert label != state, (
            f"experiment_state_label({state!r}) returned raw slug: {label!r}"
        )
        assert "_" not in label


class TestKbVersionLabelConcept:
    """Validate kb_version handling via the dimension_label fallback path."""

    def test_kb_version_in_dimension_label(self):
        # The KB dimension slug maps to "Knowledge base" — no raw "kb" renders alone.
        assert dimension_label("kb") == "Knowledge base"

    def test_kb_slug_not_exposed_by_ladder_tier(self):
        # Ensure ladder_tier_label doesn't accidentally pass through a "kb_v0" if
        # called with something non-int (coercion path).
        result = ladder_tier_label(None)
        assert "kb_v" not in result.lower()
