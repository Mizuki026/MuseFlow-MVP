from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    prompt: str
    size_preset: str


@dataclass(frozen=True, slots=True)
class GenerationResult:
    provider_name: str
    provider_request_id: str | None
    result_digest: str
    metadata: dict[str, str]


class GenerationProvider(Protocol):
    def generate(
        self,
        request: GenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
    ) -> GenerationResult: ...


class MockProvider:
    def generate(
        self,
        request: GenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
    ) -> GenerationResult:
        digest = hashlib.sha256(
            f"{request.prompt}\n{request.size_preset}\n{request_key}".encode()
        ).hexdigest()
        return GenerationResult(
            provider_name="mock",
            provider_request_id=remote_request_id or f"mock-{request_key}",
            result_digest=f"mock-result-{request_key}",
            metadata={"sha256": digest, "content_type": "image/mock"},
        )
