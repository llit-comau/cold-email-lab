"""Phase 12: draft QA lint — pure functions, no LLM calls.

Checks a generated OutreachAngle set for the voice/anti-fabrication rules in
CLAUDE.md and prompts/email_generation.md before it's saved. Never blocks a
save; callers record the resulting flags (`qa_flags`) so a human can see them
at review time.
"""

import re

from ..storage.models import Email, OutreachAngle

MAX_SUBJECT_WORDS = 6
MAX_INITIAL_WORDS = 120
MAX_FOLLOWUP_WORDS = 80

# Same voice rules as CLAUDE.md / prompts/email_generation.md.
BANNED_PHRASES = [
    "i hope this finds you well",
    "circle back",
    "synergy",
    "touching base",
    "i noticed your impressive",
    "quick question",
    "let's hop on a call",
    "book a time",
    "let me know your thoughts",
]

# Heuristic patterns for fabricated social proof. A match is allowed only when
# the exact matched text also appears (case-insensitively) in the profile's
# proof_points — i.e. it's a real, provided fact, not an invention.
FABRICATION_PATTERNS = [
    re.compile(r"\ba similar [a-z\s]{0,25}?firm\b", re.I),
    re.compile(r"\ba similar [a-z\s]{0,25}?(business|company|practice)\b", re.I),
    re.compile(r"\bone of our clients?\b", re.I),
    re.compile(r"\ba client of ours\b", re.I),
    re.compile(r"\brecently (solved|helped|worked with|fixed)\b", re.I),
    re.compile(r"\bwe(?:'ve| have) (?:helped|worked with|solved)\b", re.I),
    re.compile(r"\bcase stud(?:y|ies)\b", re.I),
    re.compile(r"\bsuccess stor(?:y|ies)\b", re.I),
    re.compile(r"\b\d{1,3}(?:\.\d+)?%"),  # unverifiable statistic
]

STEP_KEYS = ("initial_email", "followup_day3", "followup_day7")


def lint_email(
    email: Email,
    *,
    max_words: int,
    proof_points: list[str] | None = None,
) -> list[str]:
    """Pure lint of a single email. Returns a flat list of human-readable flags."""
    flags: list[str] = []
    proof_points = proof_points or []
    proof_text = " ".join(proof_points).lower()

    body_words = email.body.split()
    if len(body_words) > max_words:
        flags.append(f"body exceeds {max_words} words ({len(body_words)})")

    subject_words = email.subject.split()
    if len(subject_words) > MAX_SUBJECT_WORDS:
        flags.append(f"subject exceeds {MAX_SUBJECT_WORDS} words ({len(subject_words)})")
    if email.subject != email.subject.lower():
        flags.append("subject is not lowercase")

    combined_lower = f"{email.subject} {email.body}".lower()
    for phrase in BANNED_PHRASES:
        if phrase in combined_lower:
            flags.append(f"banned phrase: {phrase!r}")

    for pattern in FABRICATION_PATTERNS:
        for match in pattern.finditer(email.body):
            matched_text = match.group(0)
            if proof_points and matched_text.lower() in proof_text:
                continue  # matches a real, provided proof point — allowed
            flags.append(f"possible fabrication: {matched_text!r}")

    return flags


def lint_angle(
    angle: OutreachAngle,
    *,
    proof_points: list[str] | None = None,
) -> list[str]:
    """Lint all three emails in an angle. Flags are prefixed with which email they concern."""
    flags: list[str] = []
    emails = {
        "initial_email": (angle.initial_email, MAX_INITIAL_WORDS),
        "followup_day3": (angle.followup_day3, MAX_FOLLOWUP_WORDS),
        "followup_day7": (angle.followup_day7, MAX_FOLLOWUP_WORDS),
    }
    for key, (email, max_words) in emails.items():
        for f in lint_email(email, max_words=max_words, proof_points=proof_points):
            flags.append(f"{key}: {f}")
    return flags


def lint_offer_relevance(angles: list[OutreachAngle], offer_keywords: list[str] | None) -> list[str]:
    """At least one offer_keyword must appear somewhere across the whole email set."""
    if not offer_keywords:
        return []
    all_text = " ".join(
        f"{e.subject} {e.body}"
        for angle in angles
        for e in (angle.initial_email, angle.followup_day3, angle.followup_day7)
    ).lower()
    if not any(kw.lower() in all_text for kw in offer_keywords):
        return [
            "set: no offer_keyword found across the email set "
            f"(expected one of: {', '.join(offer_keywords)})"
        ]
    return []


def lint_email_set(
    angles: list[OutreachAngle],
    *,
    offer_keywords: list[str] | None = None,
    proof_points: list[str] | None = None,
) -> dict[str, list[str]]:
    """Lint a full 3-angle set. Returns {angle_name: [flags]} (only for angles with flags).

    Set-wide flags (e.g. offer relevance) are duplicated (prefixed "set:") onto every angle
    so each outreach_set/sequence_step row carries the full picture independently.
    """
    set_flags = [f"set: {f}" if not f.startswith("set:") else f for f in lint_offer_relevance(angles, offer_keywords)]

    result: dict[str, list[str]] = {}
    for angle in angles:
        flags = lint_angle(angle, proof_points=proof_points) + set_flags
        if flags:
            result[angle.angle_name] = flags
    return result


def flags_for_step(all_flags: list[str], step_key: str) -> list[str]:
    """Filter an angle's full flag list down to what's relevant for one sequence step.

    step_key is one of 'initial_email', 'followup_day3', 'followup_day7'. Set-wide
    flags (prefixed 'set:') are always included since they concern the whole angle.
    """
    return [f for f in all_flags if f.startswith(f"{step_key}:") or f.startswith("set:")]
