from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class PublicationDecision(StrEnum):
    PUBLISH = "PUBLISH"
    PUBLISHED = "PUBLISHED"
    ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
    OWNERSHIP_LOST = "OWNERSHIP_LOST"
    CONFLICTING_RESULT = "CONFLICTING_RESULT"


class ResultPublicationStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
    OWNERSHIP_LOST = "OWNERSHIP_LOST"
    CONFLICTING_RESULT = "CONFLICTING_RESULT"


@dataclass(frozen=True, slots=True)
class ResultPointer:
    attempt_id: UUID
    object_key: str
    content_type: str
    size_bytes: int
    sha256: str


def decide_result_publication(
    *,
    token_matches: bool,
    lease_active: bool,
    deadline_active: bool = True,
    attempt_status: str,
    task_status: str,
    claim_attempt_id: UUID,
    existing: ResultPointer | None,
    candidate: ResultPointer,
) -> PublicationDecision:
    """Decide publication under the caller's locked database transaction."""
    if not token_matches:
        return PublicationDecision.OWNERSHIP_LOST

    if existing is not None:
        if (
            existing == candidate
            and claim_attempt_id == existing.attempt_id
            and attempt_status == "SUCCEEDED"
            and task_status == "SUCCEEDED"
        ):
            return PublicationDecision.ALREADY_PUBLISHED
        return PublicationDecision.CONFLICTING_RESULT

    if (
        not lease_active
        or not deadline_active
        or attempt_status != "RUNNING"
        or task_status != "RUNNING"
    ):
        return PublicationDecision.OWNERSHIP_LOST
    return PublicationDecision.PUBLISH
