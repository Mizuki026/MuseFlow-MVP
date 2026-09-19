from __future__ import annotations

import base64
import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast
from urllib.parse import quote, urlparse

import httpx

from museflow.assets import ResultDownloadError, SecureResultDownloader

DEFAULT_DASHSCOPE_API_HOST = "https://dashscope.aliyuncs.com"
DASHSCOPE_MODEL = "wan2.6-t2i"
DASHSCOPE_SIZE = "1280*1280"


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code, self.retryable = code, retryable


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


class ProviderConfigurationError(PermanentProviderError):
    def __init__(self, message: str = "provider is not configured") -> None:
        super().__init__("PROVIDER_NOT_CONFIGURED", message)


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    prompt: str
    size_preset: str
    deadline_at: datetime | None = None


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
        on_remote_request_id: Callable[[str], None] | None = None,
    ) -> GenerationResult: ...


class MockScenario(StrEnum):
    SUCCESS = "success"
    TRANSIENT_THEN_SUCCESS = "transient_then_success"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PERMANENT_FAILURE = "permanent_failure"


class MockProvider:
    name = "mock"
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
        on_remote_request_id: Callable[[str], None] | None = None,
    ) -> GenerationResult:
        if self._failures:
            raise self._failures.pop(0)
        if self._scenario is MockScenario.TRANSIENT_THEN_SUCCESS:
            if int(request_key.rsplit(":attempt:", 1)[-1]) == 1:
                raise TransientProviderError("PROVIDER_UNAVAILABLE", "mock provider is recovering")
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
            provider_name=self.name,
            provider_request_id=remote_request_id or f"mock-{request_key}",
            result_digest=f"mock-result-{request_key}",
            metadata={"sha256": digest, "content_type": "image/png"},
            content=self._PNG_1X1,
            content_type="image/png",
        )


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
        request: GenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
        on_remote_request_id: Callable[[str], None] | None = None,
    ) -> GenerationResult:
        del request_key
        if request.size_preset != DASHSCOPE_SIZE:
            raise PermanentProviderError("PROVIDER_INVALID_REQUEST", "unsupported image size")
        deadline = self._monotonic() + self._remaining_timeout(request)
        task_id = remote_request_id or self._submit(request)
        if remote_request_id is None and on_remote_request_id is not None:
            on_remote_request_id(task_id)
        result_url, statuses, poll_codes = self._poll(task_id, deadline)
        try:
            image = self._downloader.download(result_url)
        except ResultDownloadError as error:
            error_type = TransientProviderError if error.retryable else PermanentProviderError
            raise error_type(error.code, str(error)) from error
        except httpx.HTTPError as error:
            raise TransientProviderError("RESULT_DOWNLOAD_UNAVAILABLE") from error
        except ValueError as error:
            raise PermanentProviderError("RESULT_INVALID", str(error)) from error
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
            },
            content=image.content,
            content_type=image.content_type,
        )

    def _submit(self, request: GenerationRequest) -> str:
        response = self._send(
            "POST",
            "/api/v1/services/aigc/image-generation/generation",
            json={
                "model": DASHSCOPE_MODEL,
                "input": {"messages": [{"role": "user", "content": [{"text": request.prompt}]}]},
                "parameters": {"n": 1, "size": DASHSCOPE_SIZE, "prompt_extend": False},
            },
            headers={"X-DashScope-Async": "enable"},
        )
        output = self._mapping(self._payload(response, "submit").get("output"))
        task_id: object = output.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise PermanentProviderError("PROVIDER_INVALID_RESPONSE", "provider task id is missing")
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

    def _poll(self, task_id: str, deadline: float) -> tuple[str, list[str], list[int]]:
        statuses: list[str] = []
        codes: list[int] = []
        path = f"/api/v1/tasks/{quote(task_id, safe='')}"
        while True:
            if self._monotonic() >= deadline:
                raise TransientProviderError("PROVIDER_POLL_TIMEOUT", "provider polling timed out")
            response = self._send("GET", path)
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
                continue
            if status == "SUCCEEDED":
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

    def _remaining_timeout(self, request: GenerationRequest) -> float:
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
    name = os.environ.get("MUSEFLOW_PROVIDER", "mock").lower()
    if name == "mock":
        return MockProvider()
    if name == "dashscope":
        return DashScopeProvider()
    raise ProviderConfigurationError(f"unsupported MUSEFLOW_PROVIDER: {name}")
