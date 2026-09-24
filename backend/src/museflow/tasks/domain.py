from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class GenerationType(StrEnum):
    TEXT_TO_IMAGE = "TEXT_TO_IMAGE"
    IMAGE_TO_IMAGE = "IMAGE_TO_IMAGE"


class ReferenceAssetStatus(StrEnum):
    STAGING = "STAGING"
    READY = "READY"
    FAILED = "FAILED"
    DELETE_PENDING = "DELETE_PENDING"
    DELETED = "DELETED"


class DomainErrorCode(StrEnum):
    INVALID_PROMPT = "INVALID_PROMPT"
    INVALID_GENERATION_OPTIONS = "INVALID_GENERATION_OPTIONS"
    PROVIDER_CAPABILITY_UNSUPPORTED = "PROVIDER_CAPABILITY_UNSUPPORTED"
    REFERENCE_ASSET_NOT_ALLOWED = "REFERENCE_ASSET_NOT_ALLOWED"
    REFERENCE_ASSET_REQUIRED = "REFERENCE_ASSET_REQUIRED"
    REFERENCE_ASSET_INVALID = "REFERENCE_ASSET_INVALID"
    RETRY_NOT_ALLOWED = "RETRY_NOT_ALLOWED"


class DomainValidationError(ValueError):
    def __init__(self, code: DomainErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CreateTaskRequest:
    prompt: str
    size_preset: str | None = None
    image_count: int | None = None
    execution_profile: str | None = None
    generation_type: GenerationType = GenerationType.TEXT_TO_IMAGE
    reference_asset_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class TextToImageInput:
    prompt: str
    size_preset: str

    @property
    def generation_type(self) -> GenerationType:
        return GenerationType.TEXT_TO_IMAGE


@dataclass(frozen=True, slots=True)
class ImageToImageInput:
    prompt: str
    size_preset: str
    reference_asset_id: UUID
    reference_sha256: str

    @property
    def generation_type(self) -> GenerationType:
        return GenerationType.IMAGE_TO_IMAGE


TaskInput = TextToImageInput | ImageToImageInput


@dataclass(frozen=True, slots=True)
class VerifiedReferenceImage:
    content: bytes
    content_type: str
    width: int
    height: int
    sha256: str


@dataclass(frozen=True, slots=True)
class NormalizedCreateTaskRequest:
    input: TaskInput
    image_count: int
    execution_profile: str | None

    @property
    def prompt(self) -> str:
        return self.input.prompt

    @property
    def size_preset(self) -> str:
        return self.input.size_preset

    @property
    def generation_type(self) -> GenerationType:
        return self.input.generation_type

    @property
    def reference_asset_id(self) -> UUID | None:
        if isinstance(self.input, ImageToImageInput):
            return self.input.reference_asset_id
        return None

    @property
    def reference_sha256(self) -> str | None:
        if isinstance(self.input, ImageToImageInput):
            return self.input.reference_sha256
        return None


@dataclass(frozen=True, slots=True)
class ProviderTaskSnapshot:
    profile: str
    provider_name: str
    model_name: str
    capability_version: str


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    max_attempts: int
    policy_version: str
    deadline_seconds: int
    retry_initial_delay_seconds: float = 2.0
    retry_multiplier: float = 2.0
    retry_max_delay_seconds: float = 30.0
    lease_seconds: float = 240.0
    heartbeat_interval_seconds: float = 30.0

    def snapshot(self) -> dict[str, object]:
        return {
            "version": self.policy_version,
            "frozen": True,
            "max_attempts": self.max_attempts,
            "deadline_seconds": self.deadline_seconds,
            "retry_policy": {
                "initial_delay_seconds": self.retry_initial_delay_seconds,
                "multiplier": self.retry_multiplier,
                "max_delay_seconds": self.retry_max_delay_seconds,
            },
            "lease_settings": {
                "lease_seconds": self.lease_seconds,
                "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            },
        }


@dataclass(frozen=True, slots=True)
class QueuedTask:
    id: UUID
    idempotency_key: str
    request_fingerprint: str
    prompt: str
    size_preset: str
    status: TaskStatus
    max_attempts: int
    policy_version: str
    deadline_at: datetime
    created_at: datetime
    queued_at: datetime
    retried_from_task_id: UUID | None = None
    execution_profile: str | None = None
    generation_type: GenerationType = GenerationType.TEXT_TO_IMAGE
    reference_asset_id: UUID | None = None
    reference_sha256: str | None = None
    provider_profile: str = "legacy-unfrozen-v1"
    provider_name: str = "legacy-unknown"
    model_name: str = "legacy-unknown"
    capability_version: str = "legacy-unknown"
    policy_snapshot: dict[str, object] = field(default_factory=lambda: dict[str, object]())

    @property
    def input(self) -> TaskInput:
        if self.generation_type is GenerationType.IMAGE_TO_IMAGE:
            if self.reference_asset_id is None or self.reference_sha256 is None:
                raise ValueError("image-to-image task is missing its reference snapshot")
            return ImageToImageInput(
                prompt=self.prompt,
                size_preset=self.size_preset,
                reference_asset_id=self.reference_asset_id,
                reference_sha256=self.reference_sha256,
            )
        return TextToImageInput(prompt=self.prompt, size_preset=self.size_preset)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    initial_delay_seconds: float = 2.0
    multiplier: float = 2.0
    max_delay_seconds: float = 30.0

    def delay_seconds(self, sequence: int, random_value: float) -> float:
        if sequence < 1 or not 0.0 <= random_value <= 1.0:
            raise ValueError("invalid retry delay inputs")
        base = min(
            self.max_delay_seconds,
            self.initial_delay_seconds * self.multiplier ** (sequence - 1),
        )
        return base * random_value


def retry_is_allowed(error_code: str | None) -> bool:
    return error_code in {
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_TIMEOUT",
        "PROVIDER_NOT_CONFIGURED",
        "PROVIDER_AUTHENTICATION",
        "INPUT_STORAGE_UNAVAILABLE",
        "RESULT_STORAGE_ERROR",
        "RETRY_EXHAUSTED",
        "DEADLINE_EXCEEDED",
    }


def normalize_create_request(
    request: CreateTaskRequest,
    *,
    reference_sha256: str | None = None,
) -> NormalizedCreateTaskRequest:
    try:
        generation_type = GenerationType(request.generation_type)
    except ValueError as error:
        raise DomainValidationError(
            DomainErrorCode.PROVIDER_CAPABILITY_UNSUPPORTED, "generation type is not supported"
        ) from error
    if not request.prompt.strip():
        raise DomainValidationError(
            DomainErrorCode.INVALID_PROMPT,
            "prompt must contain at least one non-whitespace character",
        )
    if not 1 <= len(request.prompt) <= 2000:
        raise DomainValidationError(
            DomainErrorCode.INVALID_PROMPT,
            "prompt length must be between 1 and 2000 characters",
        )
    if request.size_preset not in (None, "1280*1280") or request.image_count not in (None, 1):
        raise DomainValidationError(
            DomainErrorCode.INVALID_GENERATION_OPTIONS,
            "only the 1280*1280 single-image preset is supported",
        )
    size_preset = request.size_preset or "1280*1280"
    if generation_type is GenerationType.TEXT_TO_IMAGE:
        if request.reference_asset_id is not None or reference_sha256 is not None:
            raise DomainValidationError(
                DomainErrorCode.REFERENCE_ASSET_NOT_ALLOWED,
                "text-to-image tasks cannot reference an image asset",
            )
        input_value: TaskInput = TextToImageInput(request.prompt, size_preset)
    else:
        if request.reference_asset_id is None:
            raise DomainValidationError(
                DomainErrorCode.REFERENCE_ASSET_REQUIRED,
                "image-to-image tasks require one reference asset",
            )
        if (
            reference_sha256 is None
            or len(reference_sha256) != 64
            or any(character not in "0123456789abcdef" for character in reference_sha256)
        ):
            raise DomainValidationError(
                DomainErrorCode.REFERENCE_ASSET_INVALID,
                "image-to-image tasks require a server-verified reference digest",
            )
        input_value = ImageToImageInput(
            prompt=request.prompt,
            size_preset=size_preset,
            reference_asset_id=request.reference_asset_id,
            reference_sha256=reference_sha256,
        )
    return NormalizedCreateTaskRequest(
        input=input_value,
        image_count=1,
        execution_profile=request.execution_profile,
    )


def request_fingerprint(request: NormalizedCreateTaskRequest) -> str:
    payload: dict[str, str | int] = {
        "image_count": request.image_count,
        "prompt": request.prompt,
        "size_preset": request.size_preset,
    }
    if request.execution_profile is not None:
        payload["execution_profile"] = request.execution_profile
    if isinstance(request.input, ImageToImageInput):
        payload.update(
            {
                "generation_type": GenerationType.IMAGE_TO_IMAGE.value,
                "reference_asset_id": str(request.input.reference_asset_id),
                "reference_sha256": request.input.reference_sha256,
            }
        )
    canonical_request = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical_request).hexdigest()


def create_queued_task(
    *,
    task_id: UUID,
    idempotency_key: str,
    request: NormalizedCreateTaskRequest,
    created_at: datetime,
    policy: TaskPolicy,
    retried_from_task_id: UUID | None = None,
    provider_profile: str = "legacy-unfrozen-v1",
    provider_name: str = "legacy-unknown",
    model_name: str = "legacy-unknown",
    capability_version: str = "legacy-unknown",
    policy_snapshot: dict[str, object] | None = None,
) -> QueuedTask:
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    if policy.max_attempts < 1 or policy.deadline_seconds < 1:
        raise ValueError("task policy must allow at least one attempt and one second")
    return QueuedTask(
        id=task_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint(request),
        prompt=request.prompt,
        size_preset=request.size_preset,
        status=TaskStatus.QUEUED,
        max_attempts=policy.max_attempts,
        policy_version=policy.policy_version,
        deadline_at=created_at + timedelta(seconds=policy.deadline_seconds),
        created_at=created_at,
        queued_at=created_at,
        retried_from_task_id=retried_from_task_id,
        execution_profile=request.execution_profile,
        generation_type=request.generation_type,
        reference_asset_id=request.reference_asset_id,
        reference_sha256=request.reference_sha256,
        provider_profile=provider_profile,
        provider_name=provider_name,
        model_name=model_name,
        capability_version=capability_version,
        policy_snapshot=policy_snapshot if policy_snapshot is not None else policy.snapshot(),
    )
