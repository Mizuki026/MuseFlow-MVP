from __future__ import annotations

import hashlib
import io
import os
import struct
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast
from urllib.parse import quote, urlparse

import httpx
from PIL import Image, ImageOps

from museflow.assets import ResultDownloadError, SecureResultDownloader
from museflow.provider_profiles import (
    profile_for_id,
    selected_text_to_image_profile,
)
from museflow.tasks.domain import (
    GenerationType,
    VerifiedReferenceImage,
)
from museflow.tasks.execution_semantics import AttemptPhase

DEFAULT_DASHSCOPE_API_HOST = "https://dashscope.aliyuncs.com"
DASHSCOPE_MODEL = "wan2.6-t2i"
DASHSCOPE_SIZE = "1280*1280"


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        diagnostic: str | None = None,
        submission_state_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.code, self.retryable = code, retryable
        self.diagnostic = diagnostic
        self.submission_state_unknown = submission_state_unknown


class ProviderSubmissionUnknownError(ProviderError):
    def __init__(self, message: str = "provider may have accepted the creation request") -> None:
        super().__init__(
            "PROVIDER_SUBMISSION_UNKNOWN",
            message,
            retryable=False,
            submission_state_unknown=True,
        )


class TransientProviderError(ProviderError):
    def __init__(
        self,
        code: str = "PROVIDER_UNAVAILABLE",
        message: str = "provider unavailable",
        *,
        diagnostic: str | None = None,
    ) -> None:
        super().__init__(code, message, retryable=True, diagnostic=diagnostic)


class PermanentProviderError(ProviderError):
    def __init__(
        self,
        code: str = "PROVIDER_REJECTED",
        message: str = "provider rejected request",
        *,
        diagnostic: str | None = None,
    ) -> None:
        super().__init__(code, message, retryable=False, diagnostic=diagnostic)


class ProviderConfigurationError(PermanentProviderError):
    def __init__(self, message: str = "provider is not configured") -> None:
        super().__init__("PROVIDER_NOT_CONFIGURED", message)


@dataclass(frozen=True, slots=True)
class TextToImageProviderInput:
    prompt: str
    size_preset: str


@dataclass(frozen=True, slots=True)
class ImageToImageProviderInput:
    prompt: str
    size_preset: str
    reference_image: VerifiedReferenceImage


ProviderInput = TextToImageProviderInput | ImageToImageProviderInput


@dataclass(frozen=True, slots=True)
class ProviderGenerationRequest:
    input: ProviderInput
    deadline_at: datetime | None = None

    @property
    def prompt(self) -> str:
        return self.input.prompt

    @property
    def size_preset(self) -> str:
        return self.input.size_preset

    @property
    def generation_type(self) -> GenerationType:
        if isinstance(self.input, ImageToImageProviderInput):
            return GenerationType.IMAGE_TO_IMAGE
        return GenerationType.TEXT_TO_IMAGE


def GenerationRequest(
    prompt: str,
    size_preset: str,
    deadline_at: datetime | None = None,
) -> ProviderGenerationRequest:
    """Build the unified provider DTO for existing text-only call sites."""
    return ProviderGenerationRequest(
        input=TextToImageProviderInput(prompt=prompt, size_preset=size_preset),
        deadline_at=deadline_at,
    )


@dataclass(frozen=True, slots=True)
class GenerationResult:
    provider_name: str
    provider_request_id: str | None
    result_digest: str
    metadata: dict[str, str]
    content: bytes = b""
    content_type: str = "image/png"


class LeaseChecker(Protocol):
    def require_ownership(self) -> None: ...


class GenerationProvider(Protocol):
    def generate(
        self,
        request: ProviderGenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
        on_remote_request_id: Callable[[str], None] | None = None,
        on_phase: Callable[[AttemptPhase], None] | None = None,
        lease_guard: LeaseChecker | None = None,
    ) -> GenerationResult: ...


class MockScenario(StrEnum):
    SUCCESS = "success"
    LONG_RUNNING_SUCCESS = "long_running_success"
    TRANSIENT_THEN_SUCCESS = "transient_then_success"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True, slots=True)
class MockProviderCall:
    generation_type: GenerationType
    reference_sha256: str | None


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    body = chunk_type + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _build_mock_png() -> bytes:
    """Build a deterministic, visible image for the local demo provider."""
    width = height = 1280
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            if (x - 960) ** 2 + (y - 270) ** 2 <= 150**2:
                color = (245, 181, 95)
            elif y > 600 + abs(x - 640) // 3:
                color = (44, 66, 83)
            elif y > 760 + abs(x - 640) // 5:
                color = (21, 37, 49)
            elif y >= 700:
                color = (18, 28, 47)
            else:
                color = (39, 29, 74)
            pixels.extend(color)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(pixels), level=9))
        + _png_chunk(b"IEND", b"")
    )


class MockProvider:
    name = "mock"
    _DEMO_PNG = _build_mock_png()
    LONG_RUNNING_DELAY_SECONDS = 4.0

    def __init__(
        self,
        failures: list[ProviderError] | None = None,
        *,
        scenario: MockScenario | str = MockScenario.SUCCESS,
        execution_delay_seconds: float = 0,
        poll_failures: list[ProviderError] | None = None,
        result_fetch_failures: list[ProviderError] | None = None,
        submission_unknown_once: bool = False,
    ) -> None:
        self._failures = list(failures or [])
        self._scenario = MockScenario(scenario)
        self._execution_delay_seconds = (
            self.LONG_RUNNING_DELAY_SECONDS
            if execution_delay_seconds == 0 and self._scenario is MockScenario.LONG_RUNNING_SUCCESS
            else execution_delay_seconds
        )
        self._poll_failures = list(poll_failures or [])
        self._result_fetch_failures = list(result_fetch_failures or [])
        self._submission_unknown_once = submission_unknown_once
        self._counter_lock = threading.Lock()
        self.create_calls = 0
        self.recovery_calls = 0
        self.poll_calls = 0
        self.result_fetch_calls = 0
        self.call_records: list[MockProviderCall] = []

    def generate(
        self,
        request: ProviderGenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
        on_remote_request_id: Callable[[str], None] | None = None,
        on_phase: Callable[[AttemptPhase], None] | None = None,
        lease_guard: LeaseChecker | None = None,
    ) -> GenerationResult:
        reference_sha256: str | None = None
        if isinstance(request.input, ImageToImageProviderInput):
            reference_image = request.input.reference_image
            reference_sha256 = hashlib.sha256(reference_image.content).hexdigest()
            if reference_sha256 != reference_image.sha256:
                raise PermanentProviderError(
                    "INPUT_REFERENCE_INVALID", "verified reference content digest changed"
                )
        with self._counter_lock:
            self.call_records.append(
                MockProviderCall(
                    generation_type=request.generation_type,
                    reference_sha256=reference_sha256,
                )
            )
        if lease_guard is not None:
            lease_guard.require_ownership()
        if remote_request_id is None:
            if on_phase is not None:
                on_phase(AttemptPhase.PROVIDER_SUBMITTING)
            if self._failures:
                raise self._failures.pop(0)
            if (
                self._scenario is MockScenario.TRANSIENT_THEN_SUCCESS
                and int(request_key.rsplit(":attempt:", 1)[-1]) == 1
            ):
                raise TransientProviderError("PROVIDER_UNAVAILABLE", "mock provider is recovering")
            if lease_guard is not None:
                lease_guard.require_ownership()
            with self._counter_lock:
                self.create_calls += 1
            if self._submission_unknown_once:
                self._submission_unknown_once = False
                raise ProviderSubmissionUnknownError()
            remote_request_id = f"mock-{request_key}"
            if on_remote_request_id is not None:
                on_remote_request_id(remote_request_id)
        else:
            with self._counter_lock:
                self.recovery_calls += 1
        if on_phase is not None:
            on_phase(AttemptPhase.PROVIDER_RUNNING)
        if self._scenario is MockScenario.RATE_LIMITED:
            raise TransientProviderError("PROVIDER_RATE_LIMITED", "mock provider rate limited")
        if self._scenario is MockScenario.TIMEOUT:
            raise TransientProviderError("PROVIDER_TIMEOUT", "mock provider timed out")
        if self._scenario is MockScenario.PERMANENT_FAILURE:
            raise PermanentProviderError("PROVIDER_REJECTED", "mock provider rejected request")
        with self._counter_lock:
            self.poll_calls += 1
        if lease_guard is not None:
            lease_guard.require_ownership()
        if self._execution_delay_seconds:
            time.sleep(self._execution_delay_seconds)
        if lease_guard is not None:
            lease_guard.require_ownership()
        if self._poll_failures:
            raise self._poll_failures.pop(0)
        if on_phase is not None:
            on_phase(AttemptPhase.RESULT_FETCHING)
        with self._counter_lock:
            self.result_fetch_calls += 1
        if lease_guard is not None:
            lease_guard.require_ownership()
        if self._result_fetch_failures:
            raise self._result_fetch_failures.pop(0)
        if isinstance(request.input, ImageToImageProviderInput):
            content = self._build_image_to_image_png(
                request,
                request_key=request_key,
                reference=request.input.reference_image,
                reference_sha256=reference_sha256,
            )
        else:
            content = self._DEMO_PNG
        digest = hashlib.sha256(content).hexdigest()
        return GenerationResult(
            provider_name=self.name,
            provider_request_id=remote_request_id,
            result_digest=f"mock-result-{request_key}",
            metadata={"sha256": digest, "content_type": "image/png"},
            content=content,
            content_type="image/png",
        )

    def _build_image_to_image_png(
        self,
        request: ProviderGenerationRequest,
        *,
        request_key: str,
        reference: VerifiedReferenceImage,
        reference_sha256: str | None,
    ) -> bytes:
        if reference_sha256 is None:
            raise PermanentProviderError(
                "INPUT_REFERENCE_INVALID", "image-to-image input is missing a reference"
            )
        seed = hashlib.sha256(
            "\0".join(
                (
                    request.prompt,
                    request.size_preset,
                    reference_sha256,
                    request_key,
                    self._scenario.value,
                )
            ).encode("utf-8")
        ).digest()
        with Image.open(io.BytesIO(reference.content)) as source:
            fitted = ImageOps.fit(
                source.convert("RGB"), (1280, 1280), method=Image.Resampling.LANCZOS
            )
        overlay = Image.new("RGB", fitted.size, (seed[0], seed[1], seed[2]))
        output = Image.blend(fitted, overlay, alpha=0.18)
        buffer = io.BytesIO()
        output.save(buffer, format="PNG", optimize=False)
        return buffer.getvalue()


class DashScopeProvider:
    name = "dashscope"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_host: str | None = None,
        client: httpx.Client | None = None,
        downloader: SecureResultDownloader | None = None,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float = 240.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        raw_host = api_host or os.environ.get("DASHSCOPE_API_HOST")
        if not self._api_key or not raw_host:
            raise ProviderConfigurationError(
                "DASHSCOPE_API_KEY and DASHSCOPE_API_HOST are required"
            )
        parsed = urlparse(raw_host if "://" in raw_host else f"https://{raw_host}")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ProviderConfigurationError("DASHSCOPE_API_HOST must be an HTTPS host")
        self._api_host = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
            follow_redirects=False,
        )
        self._downloader = downloader or SecureResultDownloader(expected_dimensions=(1280, 1280))
        self._owns_downloader = downloader is None
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_timeout_seconds = poll_timeout_seconds
        self._monotonic, self._sleep = monotonic, sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        if self._owns_downloader:
            self._downloader.close()

    def generate(
        self,
        request: ProviderGenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
        on_remote_request_id: Callable[[str], None] | None = None,
        on_phase: Callable[[AttemptPhase], None] | None = None,
        lease_guard: LeaseChecker | None = None,
    ) -> GenerationResult:
        del request_key
        if not isinstance(request.input, TextToImageProviderInput):
            raise PermanentProviderError(
                "PROVIDER_CAPABILITY_UNSUPPORTED", "DashScope text profile cannot edit images"
            )
        if request.size_preset != DASHSCOPE_SIZE:
            raise PermanentProviderError("PROVIDER_INVALID_REQUEST", "unsupported image size")
        if lease_guard is not None:
            lease_guard.require_ownership()
        deadline = self._monotonic() + self._remaining_timeout(request)
        if remote_request_id is None:
            if on_phase is not None:
                on_phase(AttemptPhase.PROVIDER_SUBMITTING)
            if lease_guard is not None:
                lease_guard.require_ownership()
            task_id = self._submit(request)
            if lease_guard is not None:
                lease_guard.require_ownership()
            if on_remote_request_id is not None:
                on_remote_request_id(task_id)
        else:
            task_id = remote_request_id
        if on_phase is not None:
            on_phase(AttemptPhase.PROVIDER_RUNNING)
        if lease_guard is not None:
            lease_guard.require_ownership()
        result_url, statuses, poll_codes = self._poll(
            task_id, deadline, lease_guard=lease_guard, on_phase=on_phase
        )
        if lease_guard is not None:
            lease_guard.require_ownership()
        host_diagnostic = self._downloader.diagnose_url(result_url)
        try:
            image = self._downloader.download(result_url)
        except ResultDownloadError as error:
            if lease_guard is not None:
                lease_guard.require_ownership()
            error_type = TransientProviderError if error.retryable else PermanentProviderError
            raise error_type(error.code, str(error)) from error
        except httpx.HTTPError as error:
            if lease_guard is not None:
                lease_guard.require_ownership()
            raise TransientProviderError("RESULT_DOWNLOAD_UNAVAILABLE") from error
        except ValueError as error:
            if lease_guard is not None:
                lease_guard.require_ownership()
            raise PermanentProviderError(
                "RESULT_INVALID",
                str(error),
                diagnostic=host_diagnostic.summary,
            ) from error
        if lease_guard is not None:
            lease_guard.require_ownership()
        return GenerationResult(
            provider_name=self.name,
            provider_request_id=task_id,
            result_digest=image.sha256,
            metadata={
                "sha256": image.sha256,
                "content_type": image.content_type,
                "width": str(image.width),
                "height": str(image.height),
                "size_bytes": str(image.size_bytes),
                "task_status_sequence": "->".join(statuses),
                "poll_http_statuses": ",".join(map(str, poll_codes)),
                "download_http_status": str(image.status_code),
                "result_host_allowlisted": "yes" if host_diagnostic.allowlisted else "no",
                "result_host_digest": host_diagnostic.host_digest or "unavailable",
            },
            content=image.content,
            content_type=image.content_type,
        )

    def _submit(self, request: ProviderGenerationRequest) -> str:
        try:
            response = self._send(
                "POST",
                "/api/v1/services/aigc/image-generation/generation",
                json={
                    "model": DASHSCOPE_MODEL,
                    "input": {
                        "messages": [{"role": "user", "content": [{"text": request.prompt}]}]
                    },
                    "parameters": {"n": 1, "size": DASHSCOPE_SIZE, "prompt_extend": False},
                },
                headers={"X-DashScope-Async": "enable"},
            )
            payload = self._payload(response, "submit")
        except TransientProviderError as error:
            if error.code == "PROVIDER_RATE_LIMITED":
                raise
            raise ProviderSubmissionUnknownError() from error
        except ProviderError as error:
            if error.code == "PROVIDER_INVALID_RESPONSE":
                raise ProviderSubmissionUnknownError(
                    "provider responded without a usable task ID"
                ) from error
            raise
        except Exception as error:
            raise ProviderSubmissionUnknownError() from error
        output = self._mapping(payload.get("output"))
        task_id: object = output.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ProviderSubmissionUnknownError(
                "provider accepted the request without returning a task id"
            )
        return task_id

    @staticmethod
    def _result_url(output: dict[str, Any]) -> str | None:
        results = output.get("results")
        if isinstance(results, list):
            for raw_result in cast(list[object], results):
                if not isinstance(raw_result, dict):
                    continue
                result = cast(dict[str, object], raw_result)
                value = result.get("url")
                if isinstance(value, str) and value:
                    return value

        choices = output.get("choices")
        if isinstance(choices, list):
            for raw_choice in cast(list[object], choices):
                if not isinstance(raw_choice, dict):
                    continue
                choice = cast(dict[str, object], raw_choice)
                raw_message = choice.get("message")
                if not isinstance(raw_message, dict):
                    continue
                message = cast(dict[str, object], raw_message)
                raw_content = message.get("content")
                if not isinstance(raw_content, list):
                    continue
                for raw_item in cast(list[object], raw_content):
                    if not isinstance(raw_item, dict):
                        continue
                    item = cast(dict[str, object], raw_item)
                    value = item.get("image")
                    if isinstance(value, str) and value:
                        return value
        return None

    def _poll(
        self,
        task_id: str,
        deadline: float,
        *,
        lease_guard: LeaseChecker | None = None,
        on_phase: Callable[[AttemptPhase], None] | None = None,
    ) -> tuple[str, list[str], list[int]]:
        statuses: list[str] = []
        codes: list[int] = []
        path = f"/api/v1/tasks/{quote(task_id, safe='')}"
        while True:
            if lease_guard is not None:
                lease_guard.require_ownership()
            if self._monotonic() >= deadline:
                raise TransientProviderError("PROVIDER_POLL_TIMEOUT", "provider polling timed out")
            response = self._send("GET", path)
            if lease_guard is not None:
                lease_guard.require_ownership()
            codes.append(response.status_code)
            payload = self._payload(response, "poll")
            output = self._mapping(payload.get("output"))
            raw_status: object = (
                output.get("task_status") or output.get("status") or payload.get("status")
            )
            if not isinstance(raw_status, str):
                raise PermanentProviderError(
                    "PROVIDER_INVALID_RESPONSE", "provider task status is missing"
                )
            status = raw_status.upper()
            if not statuses or statuses[-1] != status:
                statuses.append(status)
            if status in {"PENDING", "RUNNING", "QUEUED"}:
                remaining = max(0.0, deadline - self._monotonic())
                if remaining <= 0:
                    raise TransientProviderError(
                        "PROVIDER_POLL_TIMEOUT", "provider polling timed out"
                    )
                self._sleep(min(self._poll_interval_seconds, remaining))
                if lease_guard is not None:
                    lease_guard.require_ownership()
                continue
            if status == "SUCCEEDED":
                if on_phase is not None:
                    on_phase(AttemptPhase.RESULT_FETCHING)
                if lease_guard is not None:
                    lease_guard.require_ownership()
                result_url = self._result_url(output)
                if result_url is None:
                    raise PermanentProviderError(
                        "PROVIDER_RESULT_URL_MISSING", "provider result URL is missing"
                    )
                return result_url, statuses, codes
            if status == "FAILED":
                code: object = cast(object, output.get("code") or payload.get("code"))
                lowered = code.lower() if isinstance(code, str) else ""
                if any(
                    marker in lowered for marker in ("internal", "system", "unavailable", "timeout")
                ):
                    raise TransientProviderError("PROVIDER_UNAVAILABLE")
                raise PermanentProviderError("PROVIDER_REJECTED")
            raise PermanentProviderError(
                "PROVIDER_UNKNOWN_STATUS", "provider returned an unknown task status"
            )

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        request_headers.update(headers or {})
        try:
            response = self._client.request(
                method,
                f"{self._api_host}{path}",
                json=json,
                headers=request_headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise TransientProviderError("PROVIDER_NETWORK_ERROR") from error
        if not 200 <= response.status_code < 300:
            raise self._http_error(response)
        return response

    def _http_error(self, response: httpx.Response) -> ProviderError:
        code = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                value: object = cast(dict[str, Any], payload).get("code") or cast(
                    dict[str, Any], payload
                ).get("error_code")
                code = value if isinstance(value, str) else ""
        except ValueError:
            pass
        lowered = code.lower()
        if response.status_code == 429 and any(
            marker in lowered for marker in ("rate", "burst", "thrott", "quota")
        ):
            return TransientProviderError("PROVIDER_RATE_LIMITED")
        if response.status_code == 408 or response.status_code >= 500:
            return TransientProviderError("PROVIDER_UNAVAILABLE")
        if response.status_code == 429:
            return PermanentProviderError("PROVIDER_ACCOUNT_NOT_READY")
        if response.status_code in {401, 403}:
            return PermanentProviderError("PROVIDER_AUTHENTICATION")
        if response.status_code == 400:
            return PermanentProviderError("PROVIDER_INVALID_REQUEST")
        return PermanentProviderError("PROVIDER_REJECTED")

    @staticmethod
    def _payload(response: httpx.Response, stage: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise PermanentProviderError(
                "PROVIDER_INVALID_RESPONSE",
                f"provider returned invalid {stage} JSON",
            ) from error
        if not isinstance(payload, dict):
            raise PermanentProviderError(
                "PROVIDER_INVALID_RESPONSE", "provider response is not an object"
            )
        return cast(dict[str, Any], payload)

    @staticmethod
    def _mapping(value: object) -> dict[str, Any]:
        return cast(dict[str, Any], value) if isinstance(value, dict) else {}

    def _remaining_timeout(self, request: ProviderGenerationRequest) -> float:
        timeout = self._poll_timeout_seconds
        if request.deadline_at is not None:
            timeout = min(
                timeout,
                max(0.0, (request.deadline_at - datetime.now(UTC)).total_seconds()),
            )
        if timeout <= 0:
            raise TransientProviderError("PROVIDER_POLL_TIMEOUT", "provider polling timed out")
        return timeout


def create_provider_from_environment() -> GenerationProvider:
    profile = selected_text_to_image_profile()
    return create_provider_for_task(
        profile_id=profile.profile_id,
        provider_name=profile.provider_name,
        model_name=profile.model_name,
        capability_version=profile.capability_version,
    )


class _UnavailableTaskProfileProvider:
    name = "unavailable"

    def generate(
        self,
        request: ProviderGenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
        on_remote_request_id: Callable[[str], None] | None = None,
        on_phase: Callable[[AttemptPhase], None] | None = None,
        lease_guard: LeaseChecker | None = None,
    ) -> GenerationResult:
        del request, request_key, remote_request_id, on_remote_request_id, on_phase, lease_guard
        raise ProviderError(
            "PROVIDER_PROFILE_UNAVAILABLE",
            "the task's frozen provider profile is unavailable in this deployment",
            retryable=False,
        )


def create_provider_for_task(
    *,
    profile_id: str,
    provider_name: str,
    model_name: str,
    capability_version: str,
    execution_profile: str | None = None,
    generation_type: GenerationType | None = None,
) -> GenerationProvider:
    profile = profile_for_id(profile_id)
    if (
        profile is None
        or profile.provider_name != provider_name
        or profile.model_name != model_name
        or profile.capability_version != capability_version
        or (generation_type is not None and generation_type not in profile.generation_types)
    ):
        return _UnavailableTaskProfileProvider()
    if profile.provider_name == "mock":
        return MockProvider(scenario=execution_profile or MockScenario.SUCCESS)
    if profile.provider_name == "dashscope":
        try:
            return DashScopeProvider()
        except ProviderConfigurationError:
            return _UnavailableTaskProfileProvider()
    return _UnavailableTaskProfileProvider()
