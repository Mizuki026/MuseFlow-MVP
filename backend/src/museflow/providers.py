from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TransientProviderError(ProviderError):
    def __init__(
        self, code: str = "PROVIDER_UNAVAILABLE", message: str = "provider unavailable"
    ) -> None:
        super().__init__(code, message, retryable=True)


class PermanentProviderError(ProviderError):
    def __init__(
        self, code: str = "PROVIDER_REJECTED", message: str = "provider rejected request"
    ) -> None:
        super().__init__(code, message, retryable=False)


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
    content: bytes = b""
    content_type: str = "image/png"


class GenerationProvider(Protocol):
    def generate(
        self,
        request: GenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
    ) -> GenerationResult: ...


class MockScenario(StrEnum):
    SUCCESS = "success"
    TRANSIENT_THEN_SUCCESS = "transient_then_success"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PERMANENT_FAILURE = "permanent_failure"


class MockProvider:
    _PNG_1X1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def __init__(
        self,
        failures: list[ProviderError] | None = None,
        *,
        scenario: MockScenario | str = MockScenario.SUCCESS,
    ) -> None:
        self._failures = list(failures or [])
        self._scenario = MockScenario(scenario)

    def generate(
        self,
        request: GenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
    ) -> GenerationResult:
        if self._failures:
            raise self._failures.pop(0)
        if self._scenario is MockScenario.TRANSIENT_THEN_SUCCESS:
            sequence = int(request_key.rsplit(":attempt:", maxsplit=1)[-1])
            if sequence == 1:
                raise TransientProviderError(
                    "PROVIDER_UNAVAILABLE", "mock provider is recovering"
                )
        if self._scenario is MockScenario.RATE_LIMITED:
            raise TransientProviderError("PROVIDER_RATE_LIMITED", "mock provider rate limited")
        if self._scenario is MockScenario.TIMEOUT:
            raise TransientProviderError("PROVIDER_TIMEOUT", "mock provider timed out")
        if self._scenario is MockScenario.PERMANENT_FAILURE:
            raise PermanentProviderError("PROVIDER_REJECTED", "mock provider rejected request")
        digest = hashlib.sha256(
            f"{request.prompt}\n{request.size_preset}\n{request_key}".encode()
        ).hexdigest()
        return GenerationResult(
            provider_name="mock",
            provider_request_id=remote_request_id or f"mock-{request_key}",
            result_digest=f"mock-result-{request_key}",
            metadata={"sha256": digest, "content_type": "image/png"},
            content=self._PNG_1X1,
            content_type="image/png",
        )
