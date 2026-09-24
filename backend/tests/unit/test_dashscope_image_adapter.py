from __future__ import annotations

import base64
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from PIL import Image

from museflow.dashscope_image_adapter import (
    DASHSCOPE_IMAGE_CREATE_PATH,
    DASHSCOPE_IMAGE_MAX_BODY_BYTES,
    DASHSCOPE_IMAGE_MAX_REFERENCE_BYTES,
    DASHSCOPE_IMAGE_MODEL,
    DASHSCOPE_IMAGE_RESULT_HOSTS,
    DashScopeWan26ImageAdapter,
)
from museflow.providers import (
    GenerationRequest,
    ImageToImageProviderInput,
    PermanentProviderError,
    ProviderGenerationRequest,
    ProviderSubmissionUnknownError,
    TransientProviderError,
)
from museflow.safe_artifacts import SafeArtifactFetcher
from museflow.tasks.domain import VerifiedReferenceImage
from museflow.tasks.execution_semantics import AttemptPhase
from museflow.tasks.lease_guard import OwnershipLostError

WORKSPACE_ORIGIN = "https://test-workspace.cn-beijing.maas.aliyuncs.com"
RESULT_HOST = next(iter(DASHSCOPE_IMAGE_RESULT_HOSTS))
SIGNED_RESULT_URL = f"https://{RESULT_HOST}/generated.png?Expires=secret-signature"


def _image_bytes(
    *, format: str = "PNG", mode: str = "RGB", size: tuple[int, int] = (256, 256)
) -> bytes:
    buffer = io.BytesIO()
    color: object = (23, 54, 89) if mode == "RGB" else (23, 54, 89, 255)
    if mode == "L":
        color = 73
    Image.new(mode, size, color).save(buffer, format=format)
    return buffer.getvalue()


def _reference(*, format: str = "PNG", mode: str = "RGB") -> VerifiedReferenceImage:
    content = _image_bytes(format=format, mode=mode)
    content_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[format]
    return VerifiedReferenceImage(
        content=content,
        content_type=content_type,
        width=256,
        height=256,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _request(reference: VerifiedReferenceImage | None = None) -> ProviderGenerationRequest:
    return ProviderGenerationRequest(
        input=ImageToImageProviderInput(
            prompt="Preserve the subject and make it a watercolor illustration.",
            size_preset="1280*1280",
            reference_image=reference or _reference(),
        ),
        deadline_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _success_payload(task_id: str = "remote-task-1") -> dict[str, object]:
    return {"output": {"task_id": task_id, "task_status": "PENDING"}}


def _poll_payload(status: str = "SUCCEEDED") -> dict[str, object]:
    output: dict[str, object] = {"task_status": status}
    if status == "SUCCEEDED":
        output["choices"] = [
            {"message": {"content": [{"image": SIGNED_RESULT_URL, "type": "image"}]}}
        ]
    return {"output": output}


def _api_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _safe_fetcher(result: bytes, *, first_failure: bool = False) -> SafeArtifactFetcher:
    attempts = 0

    def result_handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if first_failure and attempts == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=result,
        )

    return SafeArtifactFetcher(
        resolver=lambda _: ["93.184.216.34"],
        transport_factory=lambda *_: httpx.MockTransport(result_handler),
    )


def _adapter(
    client: httpx.Client, *, result: bytes | None = None, fetcher=None
) -> DashScopeWan26ImageAdapter:
    return DashScopeWan26ImageAdapter(
        api_key="unit-test-secret-key",
        api_host=WORKSPACE_ORIGIN,
        client=client,
        fetcher=fetcher or _safe_fetcher(result or _image_bytes()),
        poll_interval_seconds=0,
        total_timeout_seconds=30,
        sleep=lambda _: None,
    )


def test_adapter_builds_exact_async_edit_request_and_downloads_verified_output() -> None:
    api_requests: list[httpx.Request] = []
    poll_count = 0
    result = _image_bytes()

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        api_requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=_success_payload("remote-task-1"))
        poll_count += 1
        status = "RUNNING" if poll_count == 1 else "SUCCEEDED"
        return httpx.Response(200, json=_poll_payload(status))

    adapter = _adapter(_api_client(provider), result=result)
    remote_ids: list[str] = []
    phases: list[AttemptPhase] = []
    generated = adapter.generate(
        _request(),
        request_key="task:attempt:1",
        remote_request_id=None,
        on_remote_request_id=remote_ids.append,
        on_phase=phases.append,
    )

    assert len([request for request in api_requests if request.method == "POST"]) == 1
    create_request = api_requests[0]
    assert str(create_request.url) == f"{WORKSPACE_ORIGIN}{DASHSCOPE_IMAGE_CREATE_PATH}"
    assert create_request.headers["x-dashscope-async"] == "enable"
    assert create_request.headers["authorization"] == "Bearer unit-test-secret-key"
    payload = json.loads(create_request.content)
    assert payload["model"] == DASHSCOPE_IMAGE_MODEL
    message = payload["input"]["messages"][0]
    assert message["role"] == "user"
    assert message["content"][0]["text"] == _request().prompt
    image_data_url = message["content"][1]["image"]
    assert image_data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(image_data_url.split(",", 1)[1]) == _reference().content
    assert payload["parameters"] == {
        "enable_interleave": False,
        "n": 1,
        "size": "1K",
        "prompt_extend": False,
        "watermark": False,
    }
    assert remote_ids == ["remote-task-1"]
    assert phases == [
        AttemptPhase.PROVIDER_SUBMITTING,
        AttemptPhase.PROVIDER_RUNNING,
        AttemptPhase.RESULT_FETCHING,
    ]
    assert generated.provider_request_id == "remote-task-1"
    assert generated.content == result
    assert generated.result_digest == hashlib.sha256(result).hexdigest()
    assert generated.metadata["task_status_sequence"] == "RUNNING->SUCCEEDED"
    assert (
        generated.metadata["result_host_sha256_12"]
        == hashlib.sha256(RESULT_HOST.encode()).hexdigest()[:12]
    )
    assert SIGNED_RESULT_URL not in repr(generated)
    assert "Expires=secret-signature" not in repr(generated.metadata)
    assert all(urlsplit(str(request.url)).query == "" for request in api_requests)
    adapter.close()


@pytest.mark.parametrize(
    ("format", "content_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_request_body_uses_reference_actual_media_type(format: str, content_type: str) -> None:
    body = DashScopeWan26ImageAdapter.build_request_body(_request(_reference(format=format)).input)
    payload = json.loads(body)
    data_url = payload["input"]["messages"][0]["content"][1]["image"]
    assert data_url.startswith(f"data:{content_type};base64,")
    assert len(body) <= DASHSCOPE_IMAGE_MAX_BODY_BYTES


def test_body_size_guards_run_before_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = VerifiedReferenceImage(
        content=b"",
        content_type="image/png",
        width=256,
        height=256,
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    with pytest.raises(PermanentProviderError) as empty_error:
        DashScopeWan26ImageAdapter.build_request_body(_request(empty).input)
    assert empty_error.value.code == "PROVIDER_INVALID_REQUEST"

    oversized = VerifiedReferenceImage(
        content=b"x" * (DASHSCOPE_IMAGE_MAX_REFERENCE_BYTES + 1),
        content_type="image/png",
        width=256,
        height=256,
        sha256="irrelevant",
    )
    with pytest.raises(PermanentProviderError) as raw_error:
        DashScopeWan26ImageAdapter.build_request_body(_request(oversized).input)
    assert raw_error.value.code == "PROVIDER_INVALID_REQUEST"

    monkeypatch.setattr("museflow.dashscope_image_adapter.DASHSCOPE_IMAGE_MAX_BODY_BYTES", 120)
    with pytest.raises(PermanentProviderError) as body_error:
        DashScopeWan26ImageAdapter.build_request_body(_request().input)
    assert body_error.value.code == "PROVIDER_INVALID_REQUEST"


def test_text_to_image_input_cannot_enter_the_image_adapter() -> None:
    requests: list[httpx.Request] = []
    client = _api_client(lambda request: requests.append(request) or httpx.Response(500))
    adapter = _adapter(client)

    with pytest.raises(PermanentProviderError) as error:
        adapter.generate(
            GenerationRequest("text only", "1280*1280"),
            request_key="text-only",
            remote_request_id=None,
        )

    assert error.value.code == "PROVIDER_CAPABILITY_UNSUPPORTED"
    assert requests == []
    adapter.close()


def test_existing_remote_task_id_skips_create_and_resumes_same_task() -> None:
    requests: list[httpx.Request] = []
    result = _image_bytes()

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/tasks/remote-task-existing"
        return httpx.Response(200, json=_poll_payload())

    adapter = _adapter(_api_client(provider), result=result)
    generated = adapter.generate(
        _request(),
        request_key="task:attempt:1",
        remote_request_id="remote-task-existing",
    )

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert generated.provider_request_id == "remote-task-existing"
    adapter.close()


@pytest.mark.parametrize(
    "remote_task_id", ["invalid\nrequest-id", "invalid request-id", "bad\u0085id"]
)
def test_invalid_existing_task_id_fails_without_network_request(remote_task_id: str) -> None:
    requests: list[httpx.Request] = []
    adapter = _adapter(_api_client(lambda request: requests.append(request) or httpx.Response(500)))

    with pytest.raises(ProviderSubmissionUnknownError):
        adapter.generate(
            _request(),
            request_key="task:attempt:2",
            remote_request_id=remote_task_id,
        )

    assert requests == []
    adapter.close()


def test_create_response_timeout_is_unknown_and_never_reposted() -> None:
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("sensitive signed endpoint")

    adapter = _adapter(_api_client(provider))
    with pytest.raises(ProviderSubmissionUnknownError) as error:
        adapter.generate(_request(), request_key="task:attempt:1", remote_request_id=None)
    assert len(requests) == 1
    assert "sensitive" not in str(error.value)
    adapter.close()


def test_lost_lease_before_create_sends_no_provider_request() -> None:
    requests: list[httpx.Request] = []
    adapter = _adapter(_api_client(lambda request: requests.append(request) or httpx.Response(500)))

    class LostLease:
        def require_ownership(self) -> None:
            raise OwnershipLostError("test ownership lost")

    with pytest.raises(OwnershipLostError):
        adapter.generate(
            _request(),
            request_key="task:attempt:1",
            remote_request_id=None,
            lease_guard=LostLease(),
        )

    assert requests == []
    adapter.close()


def test_lost_lease_after_task_id_persistence_stops_before_poll() -> None:
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_success_payload())

    class RevocableLease:
        revoked = False

        def require_ownership(self) -> None:
            if self.revoked:
                raise OwnershipLostError("test ownership lost")

    guard = RevocableLease()
    saved_ids: list[str] = []
    adapter = _adapter(_api_client(provider))

    def persist_task_id(task_id: str) -> None:
        saved_ids.append(task_id)
        guard.revoked = True

    with pytest.raises(OwnershipLostError):
        adapter.generate(
            _request(),
            request_key="task:attempt:1",
            remote_request_id=None,
            on_remote_request_id=persist_task_id,
            lease_guard=guard,
        )

    assert saved_ids == ["remote-task-1"]
    assert [request.method for request in requests] == ["POST"]
    adapter.close()


def test_expired_task_deadline_sends_no_provider_request() -> None:
    requests: list[httpx.Request] = []
    adapter = _adapter(_api_client(lambda request: requests.append(request) or httpx.Response(500)))
    request = ProviderGenerationRequest(
        input=_request().input,
        deadline_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    with pytest.raises(TransientProviderError) as error:
        adapter.generate(request, request_key="task:attempt:1", remote_request_id=None)

    assert error.value.code == "PROVIDER_POLL_TIMEOUT"
    assert requests == []
    adapter.close()


def test_polling_deadline_stops_after_running_status_without_extra_get() -> None:
    requests: list[httpx.Request] = []
    clock = 10.0

    def monotonic() -> float:
        return clock

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=_success_payload())
        return httpx.Response(200, json=_poll_payload("RUNNING"))

    adapter = DashScopeWan26ImageAdapter(
        api_key="unit-test-secret-key",
        api_host=WORKSPACE_ORIGIN,
        client=_api_client(provider),
        fetcher=_safe_fetcher(_image_bytes()),
        poll_interval_seconds=10,
        total_timeout_seconds=1,
        monotonic=monotonic,
        sleep=sleep,
    )

    with pytest.raises(TransientProviderError) as error:
        adapter.generate(_request(), request_key="task:attempt:1", remote_request_id=None)

    assert error.value.code == "PROVIDER_POLL_TIMEOUT"
    assert [request.method for request in requests] == ["POST", "GET"]
    adapter.close()


def test_remote_task_id_persistence_failure_is_unknown_and_not_retried() -> None:
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_success_payload())

    adapter = _adapter(_api_client(provider))

    def fail_to_persist(_: str) -> None:
        raise OSError("database detail")

    with pytest.raises(ProviderSubmissionUnknownError) as error:
        adapter.generate(
            _request(),
            request_key="task:attempt:1",
            remote_request_id=None,
            on_remote_request_id=fail_to_persist,
        )

    assert len(requests) == 1
    assert "database detail" not in str(error.value)
    adapter.close()


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("CANCELED", "PROVIDER_TASK_CANCELED"),
        ("UNKNOWN", "PROVIDER_UNKNOWN_STATUS"),
        ("future-status", "PROVIDER_INVALID_RESPONSE"),
    ],
)
def test_terminal_and_unknown_protocol_states_fail_closed_without_create(
    status: str, expected_code: str
) -> None:
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=_success_payload())
        return httpx.Response(200, json=_poll_payload(status))

    adapter = _adapter(_api_client(provider))
    with pytest.raises(PermanentProviderError) as error:
        adapter.generate(_request(), request_key="task:attempt:1", remote_request_id=None)
    assert error.value.code == expected_code
    assert sum(request.method == "POST" for request in requests) == 1
    adapter.close()


@pytest.mark.parametrize(
    ("provider_code", "expected_code", "retryable"),
    [
        ("Throttling.RateQuota", "PROVIDER_RATE_LIMITED", True),
        ("Throttling.BurstRate", "PROVIDER_RATE_LIMITED", True),
        ("Throttling.AllocationQuota", "PROVIDER_ACCOUNT_NOT_READY", False),
        ("InvalidApiKey", "PROVIDER_AUTHENTICATION", False),
        ("Workspace.AccessDenied", "PROVIDER_PERMISSION_DENIED", False),
        ("DataInspectionFailed", "PROVIDER_CONTENT_REJECTED", False),
        ("IPInfringementSuspect", "PROVIDER_CONTENT_REJECTED", False),
        ("InvalidParameter", "PROVIDER_INVALID_REQUEST", False),
    ],
)
def test_provider_error_codes_are_mapped_without_message_matching(
    provider_code: str, expected_code: str, retryable: bool
) -> None:
    client = _api_client(
        lambda _: httpx.Response(
            429 if "Throttling" in provider_code else 400, json={"code": provider_code}
        )
    )
    adapter = _adapter(client)

    with pytest.raises((PermanentProviderError, TransientProviderError)) as error:
        adapter.generate(_request(), request_key="task:attempt:1", remote_request_id=None)

    assert error.value.code == expected_code
    assert error.value.retryable is retryable
    adapter.close()


def test_provider_503_after_create_is_unknown_and_not_safe_to_retry() -> None:
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"code": "ServiceUnavailable"})

    adapter = _adapter(_api_client(provider))
    with pytest.raises(TransientProviderError) as error:
        adapter.generate(_request(), request_key="task:attempt:1", remote_request_id=None)

    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert error.value.submission_state_unknown is True
    assert len(requests) == 1
    adapter.close()


def test_provider_failed_task_maps_safety_and_transient_codes() -> None:
    for code, error_type, expected_code in (
        ("DataInspectionFailed", PermanentProviderError, "PROVIDER_CONTENT_REJECTED"),
        ("ServiceUnavailable", TransientProviderError, "PROVIDER_UNAVAILABLE"),
    ):

        def provider(request: httpx.Request, failure_code: str = code) -> httpx.Response:
            payload = (
                _success_payload()
                if request.method == "POST"
                else {"output": {"task_status": "FAILED", "code": failure_code}}
            )
            return httpx.Response(200, json=payload)

        adapter = _adapter(_api_client(provider))
        with pytest.raises(error_type) as error:
            adapter.generate(_request(), request_key="task:attempt:1", remote_request_id=None)
        assert error.value.code == expected_code
        adapter.close()


def test_missing_or_multiple_result_urls_fail_closed() -> None:
    for output, expected_code in (
        ({"task_status": "SUCCEEDED", "choices": []}, "PROVIDER_RESULT_URL_MISSING"),
        (
            {
                "task_status": "SUCCEEDED",
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "image", "image": SIGNED_RESULT_URL},
                                {"type": "image", "image": SIGNED_RESULT_URL},
                            ]
                        }
                    }
                ],
            },
            "PROVIDER_INVALID_RESPONSE",
        ),
    ):

        def provider(
            request: httpx.Request, task_output: dict[str, object] = output
        ) -> httpx.Response:
            payload = _success_payload() if request.method == "POST" else {"output": task_output}
            return httpx.Response(200, json=payload)

        adapter = _adapter(_api_client(provider))
        with pytest.raises(PermanentProviderError) as error:
            adapter.generate(_request(), request_key="task:attempt:1", remote_request_id=None)
        assert error.value.code == expected_code
        adapter.close()


def test_multiple_result_choices_fail_closed_when_n_is_one() -> None:
    choices = [
        {"message": {"content": [{"type": "image", "image": SIGNED_RESULT_URL}]}},
        {"message": {"content": [{"type": "image", "image": SIGNED_RESULT_URL}]}},
    ]

    def provider(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_success_payload())
        return httpx.Response(
            200,
            json={"output": {"task_status": "SUCCEEDED", "choices": choices}},
        )

    adapter = _adapter(_api_client(provider))
    with pytest.raises(PermanentProviderError) as error:
        adapter.generate(_request(), request_key="task:attempt:1", remote_request_id=None)
    assert error.value.code == "PROVIDER_INVALID_RESPONSE"
    adapter.close()


@pytest.mark.parametrize(
    ("content_item", "expected_code"),
    [
        ({"image": SIGNED_RESULT_URL}, "PROVIDER_INVALID_RESPONSE"),
        ({"type": "video", "image": SIGNED_RESULT_URL}, "PROVIDER_INVALID_RESPONSE"),
        ({"type": "image"}, "PROVIDER_INVALID_RESPONSE"),
        ({"type": "text", "text": "completed"}, "PROVIDER_RESULT_URL_MISSING"),
    ],
)
def test_result_content_items_require_official_type_shape(
    content_item: dict[str, str], expected_code: str
) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = _success_payload()
        if request.method == "GET":
            payload = {
                "output": {
                    "task_status": "SUCCEEDED",
                    "choices": [{"message": {"content": [content_item]}}],
                }
            }
        return httpx.Response(200, json=payload)

    adapter = _adapter(_api_client(provider))
    with pytest.raises(PermanentProviderError) as error:
        adapter.generate(_request(), request_key="task:attempt:1", remote_request_id=None)
    assert error.value.code == expected_code
    adapter.close()


def test_fetch_failure_does_not_create_again_when_same_remote_id_is_retried() -> None:
    api_requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        api_requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=_success_payload())
        return httpx.Response(200, json=_poll_payload())

    failed_fetcher = _safe_fetcher(_image_bytes(), first_failure=True)
    first_adapter = _adapter(_api_client(provider), fetcher=failed_fetcher)
    # The profile refuses signed URLs on any host beyond its frozen exact allowlist.
    with pytest.raises((TransientProviderError, PermanentProviderError)):
        first_adapter.generate(
            _request(), request_key="task:attempt:1", remote_request_id="remote-task-1"
        )
    first_adapter.close()

    recovery_adapter = _adapter(_api_client(provider), result=_image_bytes())
    recovery_adapter.generate(
        _request(), request_key="task:attempt:1", remote_request_id="remote-task-1"
    )
    assert all(request.method == "GET" for request in api_requests)
    recovery_adapter.close()
