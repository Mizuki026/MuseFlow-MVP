from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from museflow.db.models import GenerationTaskModel
from museflow.provider_profiles import (
    ProviderProfileUnavailableError,
    configured_profile,
    profile_for_id,
    require_profile_available,
    require_profile_capability,
)
from museflow.reference_assets.repository import (
    ReferenceAssetRepository,
    ReferenceAssetStateError,
)
from museflow.tasks.domain import (
    CreateTaskRequest,
    DomainErrorCode,
    DomainValidationError,
    GenerationType,
    ProviderTaskSnapshot,
    TaskPolicy,
    TaskStatus,
    create_queued_task,
    normalize_create_request,
    request_fingerprint,
    retry_is_allowed,
)
from museflow.tasks.execution_semantics import LeaseSettings
from museflow.tasks.repository import EventRecord, TaskRecord, TaskRepository


class IdempotencyConflictError(ValueError):
    pass


class TaskNotFoundError(LookupError):
    pass


class ManualRetryNotAllowedError(ValueError):
    pass


class TaskReferenceAssetError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code.lower().replace("_", " "))
        self.code = code


@dataclass(frozen=True, slots=True)
class CreateTaskResult:
    task: TaskRecord
    idempotency_replayed: bool


@dataclass(frozen=True, slots=True)
class TaskDetail:
    task: TaskRecord
    events: list[EventRecord]


@dataclass(frozen=True, slots=True)
class TaskPage:
    items: list[TaskRecord]
    next_cursor: str | None


def encode_cursor(created_at: datetime, task_id: UUID) -> str:
    raw = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(task_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padding = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode((cursor + padding).encode("ascii")))
        created_at = datetime.fromisoformat(data["created_at"])
        task_id = UUID(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid cursor") from error
    if created_at.tzinfo is None:
        raise ValueError("invalid cursor")
    return created_at, task_id


class CreateTask:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        repository: TaskRepository | None = None,
        *,
        policy: TaskPolicy | None = None,
        reference_repository: ReferenceAssetRepository | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or TaskRepository()
        self._reference_repository = reference_repository or ReferenceAssetRepository()
        if policy is None:
            lease_settings = LeaseSettings.from_environment()
            policy = TaskPolicy(
                max_attempts=3,
                policy_version="mvp-0.2",
                deadline_seconds=600,
                lease_seconds=lease_settings.lease_seconds,
                heartbeat_interval_seconds=lease_settings.heartbeat_interval_seconds,
            )
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory

    def execute(
        self,
        request: CreateTaskRequest,
        idempotency_key: str,
        *,
        retried_from_task_id: UUID | None = None,
        provider_snapshot: ProviderTaskSnapshot | None = None,
        policy: TaskPolicy | None = None,
        policy_snapshot: dict[str, object] | None = None,
    ) -> CreateTaskResult:
        if retried_from_task_id is not None:
            return self._retry(
                retried_from_task_id,
                idempotency_key,
                explicit_request=request,
            )
        generation_type = self._generation_type(request.generation_type)
        try:
            with self._session_factory.begin() as session:
                existing = self._repository.find_by_idempotency_key(session, idempotency_key)
                if existing is not None:
                    return self._replay_existing(existing, request)

                profile_snapshot = self._resolve_provider_snapshot(
                    generation_type, request.size_preset or "1280*1280", provider_snapshot
                )
                reference_sha256 = self._lock_reference(
                    session, generation_type, request.reference_asset_id
                )
                normalized = normalize_create_request(request, reference_sha256=reference_sha256)
                created_at = self._clock()
                task = create_queued_task(
                    task_id=self._id_factory(),
                    idempotency_key=idempotency_key,
                    request=normalized,
                    created_at=created_at,
                    policy=policy or self._policy,
                    provider_profile=profile_snapshot.profile,
                    provider_name=profile_snapshot.provider_name,
                    model_name=profile_snapshot.model_name,
                    capability_version=profile_snapshot.capability_version,
                    policy_snapshot=policy_snapshot,
                )
                created = self._repository.add_queued_task(session, task)
                self._repository.add_creation_event(session, task)
                self._repository.add_execution_outbox(session, task)
        except IntegrityError:
            winner = self._find_existing(idempotency_key)
            if winner is None:
                raise
            return self._replay_existing(winner, request)
        return CreateTaskResult(task=created, idempotency_replayed=False)

    def retry(self, task_id: UUID, idempotency_key: str) -> CreateTaskResult:
        return self._retry(task_id, idempotency_key)

    def _retry(
        self,
        task_id: UUID,
        idempotency_key: str,
        *,
        explicit_request: CreateTaskRequest | None = None,
    ) -> CreateTaskResult:
        try:
            with self._session_factory.begin() as session:
                existing = self._repository.find_by_idempotency_key(session, idempotency_key)
                if existing is not None:
                    if existing.retried_from_task_id != task_id:
                        raise IdempotencyConflictError(
                            "idempotency key was used for a different retry request"
                        )
                    return CreateTaskResult(existing, idempotency_replayed=True)

                source = session.get(GenerationTaskModel, task_id, with_for_update=True)
                if source is None:
                    raise TaskNotFoundError(str(task_id))
                if source.status != TaskStatus.FAILED.value or not retry_is_allowed(
                    source.error_code
                ):
                    raise ManualRetryNotAllowedError("this task is not eligible for manual retry")
                child_id = session.scalar(
                    select(GenerationTaskModel.id).where(
                        GenerationTaskModel.retried_from_task_id == task_id
                    )
                )
                if child_id is not None:
                    raise ManualRetryNotAllowedError("this task already has a manual retry")

                generation_type = self._generation_type(source.generation_type)
                provider_snapshot = ProviderTaskSnapshot(
                    profile=source.provider_profile,
                    provider_name=source.provider_name,
                    model_name=source.model_name,
                    capability_version=source.capability_version,
                )
                profile_snapshot = self._resolve_provider_snapshot(
                    generation_type, source.size_preset, provider_snapshot
                )
                request = CreateTaskRequest(
                    prompt=source.prompt,
                    size_preset=source.size_preset,
                    execution_profile=source.execution_profile,
                    generation_type=generation_type,
                    reference_asset_id=source.reference_asset_id,
                )
                if explicit_request is not None and not self._same_request(
                    request, explicit_request
                ):
                    raise ManualRetryNotAllowedError(
                        "manual retry must preserve the original task input"
                    )
                reference_sha256 = self._lock_reference(
                    session,
                    generation_type,
                    source.reference_asset_id,
                    expected_sha256=source.reference_sha256,
                )
                normalized = normalize_create_request(request, reference_sha256=reference_sha256)
                source_policy_snapshot = source.policy_snapshot
                source_deadline_seconds = source_policy_snapshot.get(
                    "deadline_seconds",
                    (source.deadline_at - source.created_at).total_seconds(),
                )
                selected_policy = TaskPolicy(
                    max_attempts=source.max_attempts,
                    policy_version=source.policy_version,
                    deadline_seconds=max(1, int(source_deadline_seconds)),
                )
                created_at = self._clock()
                task = create_queued_task(
                    task_id=self._id_factory(),
                    idempotency_key=idempotency_key,
                    request=normalized,
                    created_at=created_at,
                    policy=selected_policy,
                    retried_from_task_id=task_id,
                    provider_profile=profile_snapshot.profile,
                    provider_name=profile_snapshot.provider_name,
                    model_name=profile_snapshot.model_name,
                    capability_version=profile_snapshot.capability_version,
                    policy_snapshot=source_policy_snapshot,
                )
                created = self._repository.add_queued_task(session, task)
                self._repository.add_creation_event(session, task)
                self._repository.add_execution_outbox(session, task)
        except ReferenceAssetStateError as error:
            raise TaskReferenceAssetError(error.code) from error
        except IntegrityError as error:
            winner = self._find_existing(idempotency_key)
            if winner is None:
                raise
            if winner.retried_from_task_id != task_id:
                raise IdempotencyConflictError(
                    "idempotency key was used for a different retry request"
                ) from error
            return CreateTaskResult(winner, idempotency_replayed=True)
        return CreateTaskResult(task=created, idempotency_replayed=False)

    def _lock_reference(
        self,
        session: Session,
        generation_type: GenerationType,
        reference_asset_id: UUID | None,
        *,
        expected_sha256: str | None = None,
    ) -> str | None:
        if generation_type is GenerationType.TEXT_TO_IMAGE:
            return None
        if reference_asset_id is None:
            return None
        try:
            record = self._reference_repository.lock_ready_for_reference(
                session, reference_asset_id, expected_sha256=expected_sha256
            )
        except ReferenceAssetStateError as error:
            raise TaskReferenceAssetError(error.code) from error
        if record.sha256 is None:
            raise TaskReferenceAssetError("REFERENCE_ASSET_INVALID")
        return record.sha256

    @staticmethod
    def _generation_type(value: GenerationType | str) -> GenerationType:
        try:
            return GenerationType(value)
        except ValueError as error:
            raise DomainValidationError(
                DomainErrorCode.PROVIDER_CAPABILITY_UNSUPPORTED,
                "generation type is not supported",
            ) from error

    @staticmethod
    def _same_request(left: CreateTaskRequest, right: CreateTaskRequest) -> bool:
        return (
            left.prompt == right.prompt
            and (left.size_preset or "1280*1280")
            == (right.size_preset or "1280*1280")
            and (left.image_count or 1) == (right.image_count or 1)
            and left.execution_profile == right.execution_profile
            and left.generation_type == right.generation_type
            and left.reference_asset_id == right.reference_asset_id
        )

    @staticmethod
    def _resolve_provider_snapshot(
        generation_type: GenerationType,
        size_preset: str,
        snapshot: ProviderTaskSnapshot | None,
    ) -> ProviderTaskSnapshot:
        if snapshot is None:
            profile = configured_profile(generation_type)
        else:
            profile = profile_for_id(snapshot.profile)
            if (
                profile is None
                or profile.provider_name != snapshot.provider_name
                or profile.model_name != snapshot.model_name
                or profile.capability_version != snapshot.capability_version
            ):
                raise ProviderProfileUnavailableError("provider profile is unavailable")
        require_profile_capability(profile, generation_type, size_preset)
        require_profile_available(profile)
        return ProviderTaskSnapshot(
            profile=profile.profile_id,
            provider_name=profile.provider_name,
            model_name=profile.model_name,
            capability_version=profile.capability_version,
        )

    def _find_existing(self, idempotency_key: str) -> TaskRecord | None:
        with self._session_factory() as session:
            return self._repository.find_by_idempotency_key(session, idempotency_key)

    @staticmethod
    def _replay_existing(existing: TaskRecord, request: CreateTaskRequest) -> CreateTaskResult:
        try:
            generation_type = GenerationType(request.generation_type)
        except ValueError:
            raise IdempotencyConflictError(
                "idempotency key was used for a different request"
            ) from None
        if existing.generation_type is not generation_type:
            raise IdempotencyConflictError("idempotency key was used for a different request")
        if (
            generation_type is GenerationType.IMAGE_TO_IMAGE
            and existing.reference_asset_id != request.reference_asset_id
        ):
            raise IdempotencyConflictError("idempotency key was used for a different request")
        if (
            existing.generation_type is GenerationType.IMAGE_TO_IMAGE
            and existing.reference_asset_id == request.reference_asset_id
        ):
            reference_sha256 = existing.reference_sha256
        else:
            reference_sha256 = None
        normalized = normalize_create_request(request, reference_sha256=reference_sha256)
        if existing.request_fingerprint != request_fingerprint(normalized):
            raise IdempotencyConflictError("idempotency key was used for a different request")
        return CreateTaskResult(task=existing, idempotency_replayed=True)


class GetTaskDetail:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        repository: TaskRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or TaskRepository()

    def execute(self, task_id: UUID) -> TaskDetail:
        with self._session_factory() as session:
            task = self._repository.find_by_id(session, task_id)
            if task is None:
                raise TaskNotFoundError(str(task_id))
            return TaskDetail(task=task, events=self._repository.list_events(session, task_id))


class ListTasks:
    MAX_LIMIT = 100

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        repository: TaskRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or TaskRepository()

    def execute(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        status: TaskStatus | None = None,
        generation_type: GenerationType | None = None,
    ) -> TaskPage:
        if not 1 <= limit <= self.MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {self.MAX_LIMIT}")
        before = decode_cursor(cursor) if cursor else None
        with self._session_factory() as session:
            records = self._repository.list_page(
                session,
                limit=limit + 1,
                before=before,
                status=status,
                generation_type=generation_type,
            )
        has_more = len(records) > limit
        items = records[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
        )
        return TaskPage(items=items, next_cursor=next_cursor)
