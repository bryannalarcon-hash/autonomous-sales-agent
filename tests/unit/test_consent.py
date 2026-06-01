# Unit tests for the compliance core (plan U13; R33/R40/R41/R42/R3). These pin the GRADEABLE
# substance: the jurisdiction-aware ConsentGate state machine (all-party vs one-party recording
# consent + the refusal path), suspected-minor detection -> parental-consent gate, deterministic
# PII scrubbing, and the deterministic sticky-voice assignment. All pure + network-free + seeded.
from __future__ import annotations

import pytest

from src.voice.consent import (
    ConsentGate,
    RefusalPolicy,
    assign_voice,
    consent_mode,
    detect_minor,
    scrub_pii,
)


# =============================== JURISDICTION (R33) ==============================================


def test_consent_mode_all_party_regions_require_explicit_consent():
    """All-party regions (CA/FL/PA/WA/IL/...) demand explicit recording consent before ANY
    recording; one-party / unknown jurisdictions default to one_party."""
    for j in ("CA", "California", "FL", "PA", "WA", "IL", "MA"):
        assert consent_mode(j) == "all_party", j
    for j in ("NY", "TX", "one-party-state", "", "ZZ"):
        assert consent_mode(j) == "one_party", j


def test_consent_mode_is_case_insensitive_and_trimmed():
    assert consent_mode(" ca ") == "all_party"
    assert consent_mode("california") == "all_party"


# =============================== STATE MACHINE: HAPPY PATH =======================================


def test_gate_starts_pending_and_cannot_record():
    gate = ConsentGate(jurisdiction="CA")
    assert gate.state == "pending"
    assert gate.can_record is False
    assert gate.is_ready is False
    # disclosure_text states it is an AI AND that the call may be recorded.
    disc = gate.disclosure_text.lower()
    assert "ai" in disc
    assert "record" in disc


def test_happy_path_all_party_grant_recording_reaches_ready_and_records():
    gate = ConsentGate(jurisdiction="CA")
    gate.acknowledge_ai()
    assert gate.state == "ai_disclosed"
    gate.grant_recording()
    assert gate.state == "ready"
    assert gate.is_ready is True
    assert gate.can_record is True
    assert gate.recorded is True


def test_recording_blocked_until_ready():
    """A call may NOT be recorded until state == ready with recording granted."""
    gate = ConsentGate(jurisdiction="CA")
    assert gate.can_record is False  # pending
    gate.acknowledge_ai()
    assert gate.can_record is False  # ai_disclosed, no recording consent yet


# =============================== REFUSAL PATH (R41) ==============================================


def test_refusal_default_policy_proceeds_unrecorded():
    """Default RefusalPolicy.PROCEED_UNRECORDED: refusing recording consent keeps the call going
    but in the 'unrecorded' state — nothing is recorded."""
    gate = ConsentGate(jurisdiction="CA")  # default policy
    gate.acknowledge_ai()
    gate.refuse_recording()
    assert gate.state == "unrecorded"
    assert gate.can_record is False
    assert gate.recorded is False
    # the call still proceeds (it is NOT ended)
    assert gate.is_active is True


def test_refusal_end_policy_ends_the_call():
    gate = ConsentGate(jurisdiction="CA", refusal_policy=RefusalPolicy.END)
    gate.acknowledge_ai()
    gate.refuse_recording()
    assert gate.state == "ended"
    assert gate.can_record is False
    assert gate.is_active is False


def test_unrecorded_call_can_still_advance_dialogue():
    """An unrecorded call proceeds normally (dialogue allowed) — it simply records nothing."""
    gate = ConsentGate(jurisdiction="CA")
    gate.acknowledge_ai()
    gate.refuse_recording()
    assert gate.can_converse is True
    assert gate.can_record is False


# =============================== MINOR DETECTION + PARENTAL (R40) ================================


def test_detect_minor_from_self_reported_age():
    assert detect_minor({"self_reported_age": 15}) is True
    assert detect_minor({"self_reported_age": 17}) is True
    assert detect_minor({"self_reported_age": 18}) is False
    assert detect_minor({"self_reported_age": 42}) is False


def test_detect_minor_from_student_calling_for_self():
    """'I'm in 9th grade and calling for myself' — caller-is-the-student in a school grade."""
    assert detect_minor({"text": "I'm in 9th grade and I'm calling for myself"}) is True
    assert detect_minor({"text": "I'm a sophomore in high school calling about my own tutoring"}) is True
    # A PARENT calling about their child is NOT a minor caller.
    assert detect_minor({"text": "I'm calling about my son who is in 9th grade"}) is False


def test_detect_minor_explicit_caller_is_student_signal():
    assert detect_minor({"caller_is_student": True, "grade_level": 9}) is True
    assert detect_minor({"caller_is_student": False, "grade_level": 9}) is False


def test_minor_requires_parental_consent_before_proceeding():
    """A suspected minor REQUIRES parental consent (COPPA/FERPA); without it -> need_parental."""
    gate = ConsentGate(jurisdiction="NY")
    gate.acknowledge_ai()
    gate.grant_recording()
    # a one-party state would be 'ready', but flagging a minor blocks until parental consent
    gate.flag_minor()
    assert gate.state == "need_parental"
    assert gate.can_record is False
    assert gate.can_converse is False  # blocked until parental consent


def test_parental_consent_granted_unblocks():
    gate = ConsentGate(jurisdiction="NY")
    gate.acknowledge_ai()
    gate.grant_recording()
    gate.flag_minor()
    gate.grant_parental()
    assert gate.state == "ready"
    assert gate.can_record is True
    assert gate.can_converse is True


def test_parental_consent_denied_ends_call():
    gate = ConsentGate(jurisdiction="NY")
    gate.acknowledge_ai()
    gate.grant_recording()
    gate.flag_minor()
    gate.deny_parental()
    assert gate.state == "ended"
    assert gate.can_record is False
    assert gate.can_converse is False


def test_minor_on_unrecorded_call_still_needs_parental():
    """Even on an unrecorded (refused-recording) call, a suspected minor needs parental consent."""
    gate = ConsentGate(jurisdiction="NY")
    gate.acknowledge_ai()
    gate.refuse_recording()
    assert gate.state == "unrecorded"
    gate.flag_minor()
    assert gate.state == "need_parental"
    gate.grant_parental()
    # parental consent on an unrecorded call returns to unrecorded (NOT ready/recording)
    assert gate.state == "unrecorded"
    assert gate.can_record is False
    assert gate.can_converse is True


# =============================== PII SCRUB (R42) =================================================


def test_scrub_pii_redacts_email():
    out = scrub_pii("Reach me at jane.doe@example.com please")
    assert "jane.doe@example.com" not in out
    assert "[EMAIL]" in out


def test_scrub_pii_redacts_phone():
    for raw in ("call me at 555-010-0001", "my number is (555) 010-0001", "+1 555 010 0001"):
        out = scrub_pii(raw)
        assert "[PHONE]" in out
        assert "555" not in out


def test_scrub_pii_redacts_ssn_and_credit_card():
    out = scrub_pii("ssn 123-45-6789 and card 4111 1111 1111 1111")
    assert "123-45-6789" not in out
    assert "4111 1111 1111 1111" not in out
    assert "[SSN]" in out
    assert "[CARD]" in out


def test_scrub_pii_redacts_street_address():
    out = scrub_pii("I live at 1600 Pennsylvania Avenue")
    assert "1600 Pennsylvania" not in out
    assert "[ADDRESS]" in out


def test_scrub_pii_redacts_obvious_full_name():
    out = scrub_pii("My name is John Smith and I want tutoring")
    assert "John Smith" not in out
    assert "[NAME]" in out


def test_scrub_pii_is_deterministic_and_leaves_clean_text_untouched():
    clean = "I want algebra help twice a week for my child."
    assert scrub_pii(clean) == clean
    text = "email a@b.com or call 555-010-0001"
    assert scrub_pii(text) == scrub_pii(text)


def test_scrub_pii_handles_empty_and_none():
    assert scrub_pii("") == ""
    assert scrub_pii(None) == ""


# =============================== STICKY VOICE (R3) ==============================================


def test_assign_voice_is_deterministic_for_a_phone_hash():
    h = "a" * 64
    assert assign_voice(h) == assign_voice(h)
    assert assign_voice(h) in ("voice_a", "voice_b")


def test_assign_voice_distributes_across_two_voices():
    """Different phone-hashes spread across BOTH voices (deterministic hash -> one of two)."""
    seen = {assign_voice(f"{i:064x}") for i in range(40)}
    assert seen == {"voice_a", "voice_b"}


def test_assign_voice_respects_custom_voice_pair():
    v = assign_voice("deadbeef" * 8, voices=("nova", "atlas"))
    assert v in ("nova", "atlas")
