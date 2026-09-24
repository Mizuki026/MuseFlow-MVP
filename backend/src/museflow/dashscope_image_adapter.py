from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, cast
from urllib.parse import quote

import httpx
from PIL import Image, UnidentifiedImageError

from museflow.provider_profiles import validate_wan26_workspace_origin
from museflow.providers import (
    GenerationResult,
    ImageToImageProviderInput,
    LeaseChecker,
    PermanentProviderError,
    ProviderConfigurationError,
    ProviderError,
    ProviderGenerationRequest,
    ProviderSubmissionUnknownError,
    TransientProviderError,
)
from museflow.safe_artifacts import (
    SafeArtifactError,
    SafeArtifactFetcher,
    SafeArtifactPolicy,
)
from museflow.safe_logging import log_task_event
from museflow.tasks.execution_semantics import AttemptPhase

logger = logging.getLogger(__name__)

DASHSCOPE_IMAGE_MODEL = "wan2.6-image"
DASHSCOPE_IMAGE_PROFILE = "dashscope-wan2.6-image-cn-beijing-edit"
DASHSCOPE_IMAGE_CAPABILITY_VERSION = "official-doc-snapshot-2026-09-24"
DASHSCOPE_IMAGE_CREATE_PATH = "/api/v1/services/aigc/image-generation/generation"
DASHSCOPE_IMAGE_RESULT_HOSTS = frozenset({"dashscope-a717.oss-accelerate.aliyuncs.com"})
DASHSCOPE_IMAGE_MAX_BODY_BYTES = 8_100_000
DASHSCOPE_IMAGE_MAX_RESPONSE_BYTES = 1_048_576
DASHSCOPE_IMAGE_MAX_REFERENCE_BYTES = 6_000_000
DASHSCOPE_IMAGE_MAX_RESULT_BYTES = 20 * 1024 * 1024
DASHSCOPE_IMAGE_POLL_INTERVAL_SECONDS = 10.0
DASHSCOPE_IMAGE_MAX_POLLS = 60
DASHSCOPE_IMAGE_TOTAL_TIMEOUT_SECONDS = 600.0

_INPUT_LIMITS = {
    "min_edge": 240,
    "max_edge": 2_048,
    "max_pixels": 4_194_304,
    "min_ratio": 0.25,
    "max_ratio": 4.0,
}
_RESULT_POLICY = SafeArtifactPolicy(
    allowed_hosts=DASHSCOPE_IMAGE_RESULT_HOSTS,
    max_response_bytes=DASHSCOPE_IMAGE_MAX_RESULT_BYTES,
    allowed_content_types=frozenset({"image/png"}),
    max_width=1_440,
    max_height=1_440,
    max_pixels=1_440 * 1_440,
    max_frames=1,
    allowed_modes=frozenset({"RGB"}),
    allow_alpha=False,
    max_redirects=0,
)


class DashScopeWan26ImageAdapter:
    """DashScope async image-edit protocol adapter for the frozen Beijing profile."""

    name = "dashscope"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_host: str | None = None,
        client: httpx.Client | None = None,
        fetcher: SafeArtifactFetcher | None = None,
        poll_interval_seconds: float = DASHSCOPE_IMAGE_POLL_INTERVAL_SECONDS,
        max_polls: int = DASHSCOPE_IMAGE_MAX_POLLS,
        total_timeout_seconds: float = DASHSCOPE_IMAGE_TOTAL_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved_api_key = api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY")
        raw_api_host = api_host if api_host is not None else os.environ.get("DASHSCOPE_API_HOST")
        if not resolved_api_key or resolved_api_key.strip() != resolved_api_key or not raw_api_host:
            raise ProviderConfigurationError("DashScope credentials are not configured")
        try:
            self._api_host = validate_wan26_workspace_origin(raw_api_host)
        except ValueError:
            raise ProviderConfigurationError("DashScope Beijing endpoint is invalid") from None
        self._api_key = resolved_api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
            transport=httpx.HTTPTransport(retries=0, trust_env=False),
        )
        self._fetcher = fetcher or SafeArtifactFetcher()
        self._poll_interval_seconds = poll_interval_seconds
        self._max_polls = max_polls
        self._total_timeout_seconds = total_timeout_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

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
        if not isinstance(request.input, ImageToImageProviderInput):
            raise PermanentProviderError(
                "PROVIDER_CAPABILITY_UNSUPPORTED", "Wan2.6 image profile requires a reference image"
            )
        if request.size_preset != "1280*1280":
            raise PermanentProviderError("PROVIDER_INVALID_REQUEST", "unsupported image size")
        self._validate_input(request.input)
        deadline = self._make_deadline(request)
        self._require_lease(lease_guard)

        task_id = remote_request_id
        if task_id is None:
            if on_phase is not None:
                on_phase(AttemptPhase.PROVIDER_SUBMITTING)
            self._require_lease(lease_guard)
            body = self.build_request_body(request.input)
            submission_started = self._monotonic()
            payload, _ = self._send(
                "POST",
                DASHSCOPE_IMAGE_CREATE_PATH,
                deadline=deadline,
                lease_guard=lease_guard,
                content=body,
                submission=True,
            )
            task_id = self._task_id(payload)
            log_task_event(
                logger,
                "provider_submission_accepted",
                generation_type="IMAGE_TO_IMAGE",
                provider_name=self.name,
                provider_profile=DASHSCOPE_IMAGE_PROFILE,
                phase=AttemptPhase.PROVIDER_SUBMITTING.value,
                duration_ms=(self._monotonic() - submission_started) * 1000,
                remote_request_id=task_id,
                status="accepted",
            )
            self._require_lease(lease_guard)
            if on_remote_request_id is not None:
                try:
                    on_remote_request_id(task_id)
                except Exception:
                    self._require_lease(lease_guard)
                    raise ProviderSubmissionUnknownError(
                        "provider task ID could not be persisted after submission"
                    ) from None
        else:
            task_id = self._validate_task_id(task_id)
        if on_phase is not None:
            on_phase(AttemptPhase.PROVIDER_RUNNING)
        self._require_lease(lease_guard)
        result_url, status_sequence, poll_statuses = self._poll(
            task_id,
            deadline=deadline,
            lease_guard=lease_guard,
            on_phase=on_phase,
        )
        self._require_lease(lease_guard)
        fetch_started = self._monotonic()
        try:
            artifact = self._fetcher.fetch(
                result_url,
                policy=_RESULT_POLICY,
                deadline_at=request.deadline_at,
                timeout_seconds=max(0.0, deadline - self._monotonic()),
                ownership_check=lambda: self._require_lease(lease_guard),
            )
        except SafeArtifactError as error:
            log_task_event(
                logger,
                "provider_result_fetch_rejected",
                level=logging.WARNING,
                generation_type="IMAGE_TO_IMAGE",
                provider_name=self.name,
                provider_profile=DASHSCOPE_IMAGE_PROFILE,
                phase=AttemptPhase.RESULT_FETCHING.value,
                error_code=error.code,
                error_type=type(error).__name__,
                remote_request_id=task_id,
                status="rejected",
            )
            if lease_guard is not None:
                lease_guard.require_ownership()
            error_type = TransientProviderError if error.retryable else PermanentProviderError
            raise error_type(error.code, str(error)) from None
        log_task_event(
            logger,
            "provider_result_fetched",
            generation_type="IMAGE_TO_IMAGE",
            provider_name=self.name,
            provider_profile=DASHSCOPE_IMAGE_PROFILE,
            phase=AttemptPhase.RESULT_FETCHING.value,
            duration_ms=(self._monotonic() - fetch_started) * 1000,
            remote_request_id=task_id,
            status="fetched",
        )
        self._require_lease(lease_guard)
        return GenerationResult(
            provider_name=self.name,
            provider_request_id=task_id,
            result_digest=artifact.sha256,
            metadata={
                "sha256": artifact.sha256,
                "content_type": artifact.content_type,
                "format": artifact.format,
                "width": str(artifact.width),
                "height": str(artifact.height),
                "frame_count": str(artifact.frame_count),
                "mode": artifact.mode,
                "size_bytes": str(artifact.size_bytes),
                "task_status_sequence": "->".join(status_sequence),
                "poll_http_statuses": ",".join(map(str, poll_statuses)),
                "result_host_sha256_12": artifact.host_sha256_12,
                "result_redirect_count": str(artifact.redirect_count),
            },
            content=artifact.content,
            content_type=artifact.content_type,
        )

    @staticmethod
    def build_request_body(image_input: ImageToImageProviderInput) -> bytes:
        if (
            not image_input.reference_image.content
            or len(image_input.reference_image.content) > DASHSCOPE_IMAGE_MAX_REFERENCE_BYTES
        ):
            raise PermanentProviderError(
                "PROVIDER_INVALID_REQUEST", "reference image size is unsupported"
            )
        if image_input.reference_image.content_type not in {
            "image/png",
            "image/jpeg",
            "image/webp",
        }:
            raise PermanentProviderError(
                "PROVIDER_INVALID_REQUEST", "reference media type is unsupported"
            )
        encoded = base64.b64encode(image_input.reference_image.content).decode("ascii")
        data_url = f"data:{image_input.reference_image.content_type};base64,{encoded}"
        payload = {
            "model": DASHSCOPE_IMAGE_MODEL,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": image_input.prompt}, {"image": data_url}],
                    }
                ]
            },
            "parameters": {
                "enable_interleave": False,
                "n": 1,
                "size": "1K",
                "prompt_extend": False,
                "watermark": False,
            },
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > DASHSCOPE_IMAGE_MAX_BODY_BYTES:
            raise PermanentProviderError(
                "PROVIDER_INVALID_REQUEST", "serialized image request exceeds its local limit"
            )
        return body

    def _poll(
        self,
        task_id: str,
        *,
        deadline: float,
        lease_guard: LeaseChecker | None,
        on_phase: Callable[[AttemptPhase], None] | None,
    ) -> tuple[str, list[str], list[int]]:
        statuses: list[str] = []
        http_statuses: list[int] = []
        path = f"/api/v1/tasks/{quote(task_id, safe='')}"
        for poll_number in range(self._max_polls):
            self._require_lease(lease_guard)
            self._check_deadline(deadline)
            request_started = self._monotonic()
            payload, status_code = self._send(
                "GET", path, deadline=deadline, lease_guard=lease_guard, submission=False
            )
            http_statuses.append(status_code)
            output = self._object(payload.get("output"))
            raw_status = output.get("task_status")
            if not isinstance(raw_status, str):
                raise PermanentProviderError(
                    "PROVIDER_INVALID_RESPONSE", "provider task status is missing"
                )
            status = raw_status.upper()
            if status not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}:
                raise PermanentProviderError(
                    "PROVIDER_INVALID_RESPONSE", "provider returned an unsupported task status"
                )
            if not statuses or statuses[-1] != status:
                statuses.append(status)
            log_task_event(
                logger,
                "provider_poll_completed",
                generation_type="IMAGE_TO_IMAGE",
                provider_name=self.name,
                provider_profile=DASHSCOPE_IMAGE_PROFILE,
                phase=AttemptPhase.PROVIDER_RUNNING.value,
                duration_ms=(self._monotonic() - request_started) * 1000,
                remote_request_id=task_id,
                status=status.lower(),
            )
            if status in {"PENDING", "RUNNING"}:
                if poll_number + 1 >= self._max_polls:
                    raise TransientProviderError(
                        "PROVIDER_POLL_TIMEOUT", "provider polling limit was reached"
                    )
                self._require_lease(lease_guard)
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise TransientProviderError(
                        "PROVIDER_POLL_TIMEOUT", "provider polling deadline was reached"
                    )
                self._sleep(min(self._poll_interval_seconds, remaining))
                self._require_lease(lease_guard)
                continue
            if status == "SUCCEEDED":
                if on_phase is not None:
                    on_phase(AttemptPhase.RESULT_FETCHING)
                self._require_lease(lease_guard)
                return self._result_url(output), statuses, http_statuses
            if status == "FAILED":
                self._raise_task_failure(output)
            if status == "CANCELED":
                raise PermanentProviderError("PROVIDER_TASK_CANCELED", "provider task was canceled")
            raise PermanentProviderError(
                "PROVIDER_UNKNOWN_STATUS", "provider task status is unknown"
            )
        raise TransientProviderError("PROVIDER_POLL_TIMEOUT", "provider polling limit was reached")

    def _send(
        self,
        method: str,
        path: str,
        *,
        deadline: float,
        lease_guard: LeaseChecker | None,
        content: bytes | None = None,
        submission: bool,
    ) -> tuple[dict[str, Any], int]:
        self._require_lease(lease_guard)
        timeout = self._request_timeout(deadline)
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        if submission:
            headers["X-DashScope-Async"] = "enable"
        try:
            with self._client.stream(
                method,
                f"{self._api_host}{path}",
                headers=headers,
                content=content,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                body = self._read_response(response, deadline)
                status_code = response.status_code
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            if submission:
                raise TransientProviderError(
                    "PROVIDER_ENDPOINT_UNAVAILABLE", "provider connection failed before submission"
                ) from None
            raise TransientProviderError(
                "PROVIDER_NETWORK_ERROR", "provider query connection failed"
            ) from None
        except httpx.TransportError:
            if submission:
                raise ProviderSubmissionUnknownError() from None
            raise TransientProviderError(
                "PROVIDER_NETWORK_ERROR", "provider query failed"
            ) from None
        except SafeArtifactError as error:
            if submission:
                raise ProviderSubmissionUnknownError(
                    "provider submission response could not be read"
                ) from None
            if error.retryable:
                raise TransientProviderError(
                    "PROVIDER_NETWORK_ERROR", "provider query response could not be read"
                ) from None
            raise PermanentProviderError(
                "PROVIDER_INVALID_RESPONSE", "provider response failed protocol validation"
            ) from None
        self._require_lease(lease_guard)
        if not 200 <= status_code < 300:
            raise self._http_error(status_code, body, submission=submission)
        try:
            parsed_payload: object = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            if submission:
                raise ProviderSubmissionUnknownError(
                    "provider accepted the request without a usable task response"
                ) from None
            raise PermanentProviderError(
                "PROVIDER_INVALID_RESPONSE", "provider returned invalid JSON"
            ) from None
        if not isinstance(parsed_payload, dict):
            if submission:
                raise ProviderSubmissionUnknownError(
                    "provider accepted the request without a usable task response"
                ) from None
            raise PermanentProviderError(
                "PROVIDER_INVALID_RESPONSE", "provider response is not an object"
            )
        return cast(dict[str, Any], parsed_payload), status_code

    def _read_response(self, response: httpx.Response, deadline: float) -> bytes:
        self._check_deadline(deadline)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise SafeArtifactError(
                "PROVIDER_INVALID_RESPONSE",
                "compressed provider responses are not accepted",
                retryable=False,
            )
        content_length = response.headers.get("content-length")
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdecimal():
                raise SafeArtifactError(
                    "PROVIDER_INVALID_RESPONSE",
                    "provider response size is invalid",
                    retryable=False,
                )
            if int(content_length) > DASHSCOPE_IMAGE_MAX_RESPONSE_BYTES:
                raise SafeArtifactError(
                    "PROVIDER_INVALID_RESPONSE",
                    "provider response exceeds its byte limit",
                    retryable=False,
                )
        chunks: list[bytes] = []
        total = 0
        if response.is_stream_consumed:
            content = response.content
            if len(content) > DASHSCOPE_IMAGE_MAX_RESPONSE_BYTES:
                raise SafeArtifactError(
                    "PROVIDER_INVALID_RESPONSE",
                    "provider response exceeds its byte limit",
                    retryable=False,
                )
            return content
        try:
            for chunk in response.iter_raw():
                self._check_deadline(deadline)
                total += len(chunk)
                if total > DASHSCOPE_IMAGE_MAX_RESPONSE_BYTES:
                    raise SafeArtifactError(
                        "PROVIDER_INVALID_RESPONSE",
                        "provider response exceeds its byte limit",
                        retryable=False,
                    )
                chunks.append(chunk)
        except SafeArtifactError:
            raise
        except httpx.TransportError:
            raise SafeArtifactError(
                "PROVIDER_INVALID_RESPONSE", "provider response stream failed", retryable=True
            ) from None
        return b"".join(chunks)

    def _http_error(self, status_code: int, body: bytes, *, submission: bool) -> ProviderError:
        code = ""
        try:
            parsed_payload: object = json.loads(body)
            if isinstance(parsed_payload, dict):
                payload = cast(dict[str, object], parsed_payload)
                raw_code: object = payload.get("code") or payload.get("error_code")
                if isinstance(raw_code, str):
                    code = raw_code
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        normalized = code.casefold()
        if status_code == 429:
            if normalized in {"throttling.ratequota", "throttling.burstrate"}:
                return TransientProviderError("PROVIDER_RATE_LIMITED", "provider is rate limited")
            return PermanentProviderError(
                "PROVIDER_ACCOUNT_NOT_READY", "provider quota or account limit was reached"
            )
        if code in {"DataInspectionFailed", "IPInfringementSuspect"}:
            return PermanentProviderError(
                "PROVIDER_CONTENT_REJECTED", "provider rejected content during safety review"
            )
        if code == "InvalidApiKey" or status_code == 401:
            return PermanentProviderError(
                "PROVIDER_AUTHENTICATION", "provider credentials were rejected"
            )
        if (
            code in {"Workspace.AccessDenied", "Endpoint.AccessDenied", "Model.AccessDenied"}
            or status_code == 403
        ):
            return PermanentProviderError(
                "PROVIDER_PERMISSION_DENIED", "provider access is not permitted"
            )
        if status_code == 400 or code in {"InvalidParameter", "InvalidImage", "InvalidFile"}:
            return PermanentProviderError(
                "PROVIDER_INVALID_REQUEST", "provider rejected the request"
            )
        if status_code >= 500 or status_code in {408, 425}:
            return TransientProviderError(
                "PROVIDER_UNAVAILABLE",
                "provider service returned an error",
                submission_state_unknown=submission,
            )
        return PermanentProviderError("PROVIDER_REJECTED", "provider rejected the request")

    @staticmethod
    def _task_id(payload: dict[str, Any]) -> str:
        output = DashScopeWan26ImageAdapter._object(payload.get("output"))
        task_id = output.get("task_id")
        return DashScopeWan26ImageAdapter._validate_task_id(task_id)

    @staticmethod
    def _validate_task_id(task_id: object) -> str:
        if (
            not isinstance(task_id, str)
            or not task_id
            or len(task_id) > 256
            or any(
                character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
                for character in task_id
            )
        ):
            raise ProviderSubmissionUnknownError("provider task ID is missing or unusable")
        return task_id

    @staticmethod
    def _result_url(output: dict[str, Any]) -> str:
        choices = output.get("choices")
        if not isinstance(choices, list):
            raise PermanentProviderError(
                "PROVIDER_RESULT_URL_MISSING", "provider result is missing"
            )
        typed_choices = cast(list[object], choices)
        if len(typed_choices) > 1:
            raise PermanentProviderError(
                "PROVIDER_INVALID_RESPONSE", "provider returned multiple result choices"
            )
        result_urls: list[str] = []
        for raw_choice in typed_choices:
            choice = raw_choice
            if not isinstance(choice, dict):
                raise PermanentProviderError(
                    "PROVIDER_INVALID_RESPONSE", "provider result structure is invalid"
                )
            typed_choice = cast(dict[str, object], choice)
            message = typed_choice.get("message")
            typed_message = cast(dict[str, object], message) if isinstance(message, dict) else None
            content = typed_message.get("content") if typed_message is not None else None
            if not isinstance(content, list):
                raise PermanentProviderError(
                    "PROVIDER_INVALID_RESPONSE", "provider result structure is invalid"
                )
            for raw_item in cast(list[object], content):
                item = raw_item
                if not isinstance(item, dict):
                    raise PermanentProviderError(
                        "PROVIDER_INVALID_RESPONSE", "provider result structure is invalid"
                    )
                typed_item = cast(dict[str, object], item)
                item_type = typed_item.get("type")
                if item_type == "image":
                    image_url = typed_item.get("image")
                    if not isinstance(image_url, str) or not image_url:
                        raise PermanentProviderError(
                            "PROVIDER_INVALID_RESPONSE", "provider image result is invalid"
                        )
                    result_urls.append(image_url)
                elif item_type == "text":
                    if not isinstance(typed_item.get("text"), str):
                        raise PermanentProviderError(
                            "PROVIDER_INVALID_RESPONSE", "provider text result is invalid"
                        )
                else:
                    raise PermanentProviderError(
                        "PROVIDER_INVALID_RESPONSE", "provider result structure is invalid"
                    )
        if len(result_urls) != 1:
            code = "PROVIDER_RESULT_URL_MISSING" if not result_urls else "PROVIDER_INVALID_RESPONSE"
            message = (
                "provider result is missing"
                if not result_urls
                else "provider returned multiple images"
            )
            raise PermanentProviderError(code, message)
        return result_urls[0]

    def _raise_task_failure(self, output: dict[str, Any]) -> None:
        code_value = output.get("code")
        if not isinstance(code_value, str):
            raise PermanentProviderError("PROVIDER_REJECTED", "provider task failed")
        normalized = code_value.casefold()
        if code_value in {"DataInspectionFailed", "IPInfringementSuspect"}:
            raise PermanentProviderError(
                "PROVIDER_CONTENT_REJECTED", "provider rejected content during safety review"
            )
        if normalized in {"throttling.ratequota", "throttling.burstrate"}:
            raise TransientProviderError("PROVIDER_RATE_LIMITED", "provider is rate limited")
        if normalized in {"modelservicefailed", "serviceunavailable", "internalerror"}:
            raise TransientProviderError("PROVIDER_UNAVAILABLE", "provider task failed temporarily")
        raise PermanentProviderError("PROVIDER_REJECTED", "provider task failed")

    def _validate_input(self, image_input: ImageToImageProviderInput) -> None:
        reference = image_input.reference_image
        if not 1 <= len(image_input.prompt) <= 2_000:
            raise PermanentProviderError("PROVIDER_INVALID_REQUEST", "prompt length is unsupported")
        if not reference.content or len(reference.content) > DASHSCOPE_IMAGE_MAX_REFERENCE_BYTES:
            raise PermanentProviderError(
                "PROVIDER_INVALID_REQUEST", "reference image size is unsupported"
            )
        expected_types = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
        expected_format = expected_types.get(reference.content_type)
        if expected_format is None:
            raise PermanentProviderError(
                "PROVIDER_INVALID_REQUEST", "reference media type is unsupported"
            )
        signature = {
            "PNG": b"\x89PNG\r\n\x1a\n",
            "JPEG": b"\xff\xd8\xff",
            "WEBP": b"RIFF",
        }[expected_format]
        if not reference.content.startswith(signature):
            raise PermanentProviderError(
                "PROVIDER_INVALID_REQUEST", "reference image signature is invalid"
            )
        if expected_format == "WEBP" and reference.content[8:12] != b"WEBP":
            raise PermanentProviderError(
                "PROVIDER_INVALID_REQUEST", "reference image signature is invalid"
            )
        try:
            with Image.open(BytesIO(reference.content)) as image:
                width, height = image.size
                frame_count = getattr(image, "n_frames", 1)
                mode = image.mode
                has_alpha = "A" in image.getbands() or "transparency" in image.info
                if image.format != expected_format:
                    raise ValueError("reference format mismatch")
                if (
                    frame_count != 1
                    or mode != "RGB"
                    or has_alpha
                    or width < _INPUT_LIMITS["min_edge"]
                    or height < _INPUT_LIMITS["min_edge"]
                    or width > _INPUT_LIMITS["max_edge"]
                    or height > _INPUT_LIMITS["max_edge"]
                    or width * height > _INPUT_LIMITS["max_pixels"]
                    or not _INPUT_LIMITS["min_ratio"]
                    <= width / height
                    <= _INPUT_LIMITS["max_ratio"]
                ):
                    raise ValueError("reference metadata is unsupported")
                image.verify()
            with Image.open(BytesIO(reference.content)) as image:
                image.load()
        except (
            OSError,
            SyntaxError,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ):
            raise PermanentProviderError(
                "PROVIDER_INVALID_REQUEST", "reference image failed profile validation"
            ) from None
        digest = hashlib.sha256(reference.content).hexdigest()
        if digest != reference.sha256 or (width, height) != (reference.width, reference.height):
            raise PermanentProviderError(
                "PROVIDER_INVALID_REQUEST",
                "reference image metadata did not match its verified content",
            )

    def _make_deadline(self, request: ProviderGenerationRequest) -> float:
        remaining = self._total_timeout_seconds
        if request.deadline_at is not None:
            if request.deadline_at.tzinfo is None:
                raise PermanentProviderError("PROVIDER_INVALID_REQUEST", "task deadline is invalid")
            remaining = min(
                remaining,
                (request.deadline_at - self._wall_clock()).total_seconds(),
            )
        if remaining <= 0:
            raise TransientProviderError("PROVIDER_POLL_TIMEOUT", "task deadline has elapsed")
        return self._monotonic() + remaining

    def _request_timeout(self, deadline: float) -> httpx.Timeout:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TransientProviderError("PROVIDER_POLL_TIMEOUT", "task deadline has elapsed")
        return httpx.Timeout(
            connect=min(5.0, remaining),
            read=min(30.0, remaining),
            write=min(30.0, remaining),
            pool=min(5.0, remaining),
        )

    def _check_deadline(self, deadline: float) -> None:
        if deadline - self._monotonic() <= 0:
            raise TransientProviderError("PROVIDER_POLL_TIMEOUT", "task deadline has elapsed")

    @staticmethod
    def _require_lease(lease_guard: LeaseChecker | None) -> None:
        if lease_guard is not None:
            lease_guard.require_ownership()

    @staticmethod
    def _object(value: object) -> dict[str, Any]:
        return cast(dict[str, Any], value) if isinstance(value, dict) else {}
