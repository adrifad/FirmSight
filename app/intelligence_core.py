"""Pure Project Intelligence domain rules: lifecycle, fingerprints, migrations.

This module must stay dependency-free (only app.schemas type mapping) so the
persistence layer and the service layer can share it without cycles. State
values are plain strings matching IntelligenceState in intelligence_schemas.
"""

from __future__ import annotations

import hashlib
import re

from .schemas import canonical_memory_type

PROVISIONAL = "PROVISIONAL"
REINFORCED = "REINFORCED"
VERIFIED = "VERIFIED"
NEEDS_REVALIDATION = "NEEDS_REVALIDATION"
CONFLICTED = "CONFLICTED"
SUPERSEDED = "SUPERSEDED"
DISABLED = "DISABLED"

ALL_STATES = {PROVISIONAL, REINFORCED, VERIFIED, NEEDS_REVALIDATION, CONFLICTED, SUPERSEDED, DISABLED}
# Lifecycle states that may still transition.
NON_TERMINAL_STATES = {PROVISIONAL, REINFORCED, VERIFIED, NEEDS_REVALIDATION, CONFLICTED}
# States eligible for normal review/chat retrieval.
RETRIEVABLE_STATES = {REINFORCED, VERIFIED}
# Minimum Memory Verifier confidence required for VERIFIED (with direct source support).
VERIFIED_CONFIDENCE_THRESHOLD = 0.85
# Independent observations required for PROVISIONAL -> REINFORCED.
REINFORCEMENT_OBSERVATIONS = 2

# Legacy status -> canonical state. Unknown legacy values stay untrusted.
LEGACY_STATE_MIGRATION = {
    "ACTIVE": REINFORCED,
    "NEEDS_REVALIDATION": NEEDS_REVALIDATION,
    "SUPERSEDED": SUPERSEDED,
    "DISABLED": DISABLED,
}


def legacy_status_for(state: str) -> str:
    """Project a canonical intelligence state onto the legacy status field.

    REINFORCED/VERIFIED project to legacy ACTIVE so existing list/context
    consumers keep seeing engineer-approved knowledge as active. PROVISIONAL
    and CONFLICTED keep their own value: pretending they are ACTIVE would
    silently elevate unverified or contradicted knowledge.
    """
    if state in {REINFORCED, VERIFIED}:
        return "ACTIVE"
    if state in ALL_STATES:
        return state
    raise ValueError(f"Unknown intelligence state: {state}")


# Allowed lifecycle transitions. Automatic synthesis may only start PROVISIONAL;
# VERIFIED additionally requires direct current-source support enforced by the
# intelligence service, not merely by this table.
VALID_TRANSITIONS: set[tuple[str, str]] = {
    (PROVISIONAL, REINFORCED),
    (PROVISIONAL, VERIFIED),
    (REINFORCED, VERIFIED),
    (PROVISIONAL, NEEDS_REVALIDATION),
    (REINFORCED, NEEDS_REVALIDATION),
    (VERIFIED, NEEDS_REVALIDATION),
    (PROVISIONAL, CONFLICTED),
    (REINFORCED, CONFLICTED),
    (VERIFIED, CONFLICTED),
    (PROVISIONAL, SUPERSEDED),
    (REINFORCED, SUPERSEDED),
    (VERIFIED, SUPERSEDED),
    (PROVISIONAL, DISABLED),
    (REINFORCED, DISABLED),
    (VERIFIED, DISABLED),
    (NEEDS_REVALIDATION, DISABLED),
    (CONFLICTED, DISABLED),
    (NEEDS_REVALIDATION, VERIFIED),
    (NEEDS_REVALIDATION, REINFORCED),
    (NEEDS_REVALIDATION, SUPERSEDED),
    (CONFLICTED, VERIFIED),
    (CONFLICTED, REINFORCED),
    (CONFLICTED, SUPERSEDED),
    (NEEDS_REVALIDATION, CONFLICTED),
    (CONFLICTED, NEEDS_REVALIDATION),
    (REINFORCED, REINFORCED),  # self-transition recorded as reinforcement observation
    (VERIFIED, VERIFIED),      # self-transition recorded as re-verification observation
}


def transition_allowed(from_state: str, to_state: str) -> bool:
    if from_state == to_state:
        # Self-transitions are only meaningful as reinforcement/re-verification
        # observations on non-terminal, non-provisional states.
        return from_state in {REINFORCED, VERIFIED}
    return (from_state, to_state) in VALID_TRANSITIONS


def normalize_statement(statement: str) -> str:
    """Case- and whitespace-normalized statement used for fingerprints."""
    return re.sub(r"\s+", " ", statement.strip().casefold()).rstrip(".").strip()


def compute_fingerprint(
    project_id: str,
    memory_type: str,
    statement: str,
    primary_links: list[str],
    evidence_locations: list[str],
) -> str:
    """Deterministic identity for equivalent-knowledge matching.

    Includes normalized type, normalized statement, sorted primary
    symbol/component/file links, and sorted evidence locations. Equivalent
    observations at different locations produce different fingerprints on
    purpose: they fall through to Memory Verifier link-overlap matching.
    """
    payload = "|".join(
        [
            project_id,
            canonical_memory_type(memory_type),
            normalize_statement(statement),
            ",".join(sorted({link.strip().casefold() for link in primary_links if link})),
            ",".join(sorted({location.strip() for location in evidence_locations if location})),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evidence_fingerprint(kind: str, file: str | None, line: int | None, symbol: str | None, description: str) -> str:
    """Identity of one evidence item; independent observations differ here."""
    payload = "|".join([kind, file or "", str(line or ""), symbol or "", normalize_statement(description[:280])])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bounded_confidence(current: float, reinforcement_delta: float, maximum: float = 0.95) -> float:
    """Documented bounded confidence formula for reinforcement.

    Each independent reinforcement adds a bounded delta and can never push
    confidence past the ceiling. Automatic records stay below the engineer
    trust band until current-source verification.
    """
    step = max(0.0, min(0.1, reinforcement_delta))
    return round(min(maximum, current + step), 3)
