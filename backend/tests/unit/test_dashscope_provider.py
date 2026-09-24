from __future__ import annotations

import hashlib

import httpx
import pytest

from museflow.assets import MAX_RESULT_BYTES, SecureResultDownloader
from museflow.providers import (
    DASHSCOPE_SIZE,
    DashScopeProvider,
    GenerationRequest,
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
)
from museflow.tasks.execution_semantics import AttemptPhase


def _png(width: int = 1280, height: int = 1280) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def _provider(handler, *, monotonic=None) -> tuple[DashScopeProvider, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = SecureResultDownloader(
        client=client,
        resolve_host=lambda _: ["93.184.216.34"],
        expected_dimensions=(1280, 1280),
    )
    provider = DashScopeProvider(
        api_key="test-key",
        api_host="https://api.example",
        client=client,
        downloader=downloader,
        poll_interval_seconds=0,
        poll_timeout_seconds=0.01,
        monotonic=monotonic or (lambda: 0.0),
        sleep=lambda _: None,
    )
    return provider, client


def test_dashscope_success_submits_once_polls_and_downloads() -> None:
    calls: list[httpx.Request] = []
    states = iter(["RUNNING", "SUCCEEDED"])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            body = request.read().decode()
            assert '"model":"wan2.6-t2i"' in body
            assert '"n":1' in body
            assert '"size":"1280*1280"' in body
            assert '"prompt_extend":false' in body
            assert request.headers["X-DashScope-Async"] == "enable"
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if request.url.host == "dashscope-result-bj.oss-cn-beijing.aliyuncs.com":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=_png())
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_status": next(states),
                    "results": [
                        {"url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result"}
                    ],
                }
            },
        )

    provider, client = _provider(handler)
    remote_ids: list[str] = []
    phases: list[AttemptPhase] = []
    try:
        result = provider.generate(
            GenerationRequest("a lighthouse", DASHSCOPE_SIZE),
            request_key="task:attempt:1",
            remote_request_id=None,
            on_remote_request_id=remote_ids.append,
            on_phase=phases.append,
        )
    finally:
        client.close()
    assert result.provider_request_id == "task-1"
    assert remote_ids == ["task-1"]
    assert phases == [
        AttemptPhase.PROVIDER_SUBMITTING,
        AttemptPhase.PROVIDER_RUNNING,
        AttemptPhase.RESULT_FETCHING,
    ]
    assert result.metadata["task_status_sequence"] == "RUNNING->SUCCEEDED"
    assert result.metadata["width"] == "1280"
    assert result.metadata["result_host_allowlisted"] == "yes"
    assert len(result.metadata["result_host_digest"]) == 12
    assert result.result_digest == hashlib.sha256(_png()).hexdigest()
    assert sum(request.method == "POST" for request in calls) == 1


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (429, {"code": "Throttling.RateQuota"}, "PROVIDER_RATE_LIMITED"),
        (500, {"code": "InternalError"}, "PROVIDER_SUBMISSION_UNKNOWN"),
        (401, {"code": "InvalidApiKey"}, "PROVIDER_AUTHENTICATION"),
    ],
)
def test_http_error_mapping(status_code: int, payload: dict[str, str], expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    provider, client = _provider(handler)
    try:
        with pytest.raises(ProviderError) as error:
            provider.generate(
                GenerationRequest("prompt", DASHSCOPE_SIZE),
                request_key="key",
                remote_request_id=None,
            )
    finally:
        client.close()
    assert error.value.code == expected


def test_timeout_is_transient_and_creation_is_not_retried() -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        return httpx.Response(200, json={"output": {"task_status": "RUNNING"}})

    clock_values = iter([0.0, 0.0, 0.02, 0.02])
    provider, client = _provider(handler, monotonic=lambda: next(clock_values))
    try:
        with pytest.raises(TransientProviderError) as error:
            provider.generate(
                GenerationRequest("prompt", DASHSCOPE_SIZE),
                request_key="key",
                remote_request_id=None,
            )
    finally:
        client.close()
    assert error.value.code == "PROVIDER_POLL_TIMEOUT"
    assert posts == 1


def test_existing_remote_request_is_polled_without_a_second_post() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "dashscope-result-bj.oss-cn-beijing.aliyuncs.com":
            return httpx.Response(
                200, headers={"content-type": "image/png"}, content=_png()
            )
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [
                        {
                            "url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result"
                        }
                    ],
                }
            },
        )

    provider, client = _provider(handler)
    try:
        result = provider.generate(
            GenerationRequest("prompt", DASHSCOPE_SIZE),
            request_key="task:attempt:1",
            remote_request_id="known-task-id",
        )
    finally:
        client.close()
    assert result.provider_request_id == "known-task-id"
    assert all(request.method != "POST" for request in calls)


def test_invalid_json_missing_task_id_and_permanent_task_failure() -> None:
    cases = [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"output": {}}),
        httpx.Response(200, json={"output": {"task_id": "t"}}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return cases.pop(0)
        return httpx.Response(
            200, json={"output": {"task_status": "FAILED", "code": "DataInspectionFailed"}}
        )

    provider, client = _provider(handler)
    try:
        with pytest.raises(ProviderError) as first:
            provider.generate(
                GenerationRequest("p", DASHSCOPE_SIZE), request_key="k", remote_request_id=None
            )
        assert first.value.code == "PROVIDER_SUBMISSION_UNKNOWN"
    finally:
        client.close()

    provider, client = _provider(lambda request: httpx.Response(200, json={"output": {}}))
    try:
        with pytest.raises(ProviderError) as second:
            provider.generate(
                GenerationRequest("p", DASHSCOPE_SIZE), request_key="k", remote_request_id=None
            )
        assert second.value.code == "PROVIDER_SUBMISSION_UNKNOWN"
    finally:
        client.close()


def test_result_url_and_download_security_contract() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "image/png"}, content=_png())
    )
    client = httpx.Client(transport=transport)
    downloader = SecureResultDownloader(
        client=client,
        resolve_host=lambda _: ["93.184.216.34"],
        expected_dimensions=(1280, 1280),
    )
    try:
        with pytest.raises(ValueError, match="host"):
            downloader.download("https://evil.example/result")
        with pytest.raises(ValueError, match="host"):
            bad_redirect = httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        302, headers={"location": "https://evil.example/result"}
                    )
                )
            )
            SecureResultDownloader(
                client=bad_redirect,
                resolve_host=lambda _: ["93.184.216.34"],
            ).download("https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result")
        assert downloader.download(
            "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result"
        ).size_bytes == len(_png())
    finally:
        client.close()


@pytest.mark.parametrize(
    ("headers", "content", "message"),
    [
        ({"content-type": "text/plain"}, _png(), "content type"),
        ({"content-type": "image/png"}, b"not-an-image", "header"),
        (
            {"content-type": "image/png", "content-length": str(MAX_RESULT_BYTES + 1)},
            _png(),
            "larger",
        ),
    ],
)
def test_download_rejects_invalid_result(
    headers: dict[str, str], content: bytes, message: str
) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers=headers, content=content)
        )
    )
    downloader = SecureResultDownloader(
        client=client,
        resolve_host=lambda _: ["93.184.216.34"],
        expected_dimensions=(1280, 1280),
    )
    try:
        with pytest.raises(ValueError, match=message):
            downloader.download("https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result")
    finally:
        client.close()


def test_missing_result_url_is_permanent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_status": "SUCCEEDED",
                    "choices": [{"message": {"content": [{"type": "image"}]}}],
                }
            },
        )

    provider, client = _provider(handler)
    try:
        with pytest.raises(PermanentProviderError) as error:
            provider.generate(
                GenerationRequest("prompt", DASHSCOPE_SIZE),
                request_key="key",
                remote_request_id=None,
            )
    finally:
        client.close()
    assert error.value.code == "PROVIDER_RESULT_URL_MISSING"


def test_wan26_async_choice_image_url_is_downloaded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if request.url.host == "dashscope-result-bj.oss-cn-beijing.aliyuncs.com":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=_png())
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_id": "task-1",
                    "task_status": "SUCCEEDED",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "image": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result.png?Expires=redacted",
                                        "type": "image",
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        )

    provider, client = _provider(handler)
    try:
        result = provider.generate(
            GenerationRequest("prompt", DASHSCOPE_SIZE),
            request_key="key",
            remote_request_id=None,
        )
    finally:
        client.close()
    assert result.provider_request_id == "task-1"
    assert result.metadata["task_status_sequence"] == "SUCCEEDED"
    assert result.metadata["width"] == "1280"
    assert result.metadata["result_host_allowlisted"] == "yes"
    assert len(result.metadata["result_host_digest"]) == 12


def test_wan26_verified_accelerated_result_host_downloads_with_default_allowlist() -> None:
    calls: list[httpx.Request] = []
    result_host = "dashscope-a717.oss-accelerate.aliyuncs.com"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if request.url.host == result_host:
            return httpx.Response(200, headers={"content-type": "image/png"}, content=_png())
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_status": "SUCCEEDED",
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {
                                        "type": "image",
                                        "image": f"https://{result_host}/synthetic-result.png",
                                    }
                                ]
                            }
                        }
                    ],
                }
            },
        )

    provider, client = _provider(handler)
    try:
        result = provider.generate(
            GenerationRequest("prompt", DASHSCOPE_SIZE),
            request_key="key",
            remote_request_id=None,
        )
    finally:
        client.close()

    assert result.content == _png()
    assert result.metadata["result_host_allowlisted"] == "yes"
    assert result.metadata["result_host_digest"] == "0bd1575e39cb"
    assert sum(request.method == "POST" for request in calls) == 1


def test_network_timeout_during_create_is_unknown_and_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("provider timeout", request=request)

    provider, client = _provider(handler)
    try:
        with pytest.raises(ProviderError) as error:
            provider.generate(
                GenerationRequest("prompt", DASHSCOPE_SIZE),
                request_key="key",
                remote_request_id=None,
            )
    finally:
        client.close()
    assert error.value.code == "PROVIDER_SUBMISSION_UNKNOWN"
    assert calls == 1


@pytest.mark.parametrize(
    ("resolved_addresses", "message"),
    [
        (["127.0.0.1"], "forbidden network"),
        (["::1"], "forbidden network"),
        (["10.0.0.8"], "forbidden network"),
        (["fe80::8"], "forbidden network"),
        (["not-an-ip"], "could not be resolved safely"),
        ([], "forbidden network"),
    ],
)
def test_download_rejects_private_loopback_and_invalid_resolution(
    resolved_addresses: list[str], message: str
) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=_png())
        )
    )
    downloader = SecureResultDownloader(
        client=client,
        resolve_host=lambda _: resolved_addresses,
        expected_dimensions=(1280, 1280),
    )
    try:
        with pytest.raises(ValueError, match=message):
            downloader.download("https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result")
    finally:
        client.close()


def test_result_host_diagnostic_does_not_expose_host() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=_png())
        )
    )
    downloader = SecureResultDownloader(
        client=client,
        resolve_host=lambda _: ["93.184.216.34"],
        expected_dimensions=(1280, 1280),
    )
    try:
        diagnostic = downloader.diagnose_url("https://untrusted.example/result")
    finally:
        client.close()

    assert diagnostic.allowlisted is False
    assert diagnostic.host_digest == hashlib.sha256(b"untrusted.example").hexdigest()[:12]
    assert "untrusted.example" not in diagnostic.summary
    assert "allowlisted=no" in diagnostic.summary


def test_rejected_result_reports_safe_host_diagnostic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_status": "SUCCEEDED",
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {
                                        "image": "https://untrusted.example/result.png",
                                        "type": "image",
                                    }
                                ]
                            }
                        }
                    ],
                }
            },
        )

    provider, client = _provider(handler)
    try:
        with pytest.raises(PermanentProviderError) as error:
            provider.generate(
                GenerationRequest("prompt", DASHSCOPE_SIZE),
                request_key="key",
                remote_request_id=None,
            )
    finally:
        client.close()

    assert error.value.code == "RESULT_INVALID"
    assert error.value.diagnostic == (
        "allowlisted=no, host_digest=" + hashlib.sha256(b"untrusted.example").hexdigest()[:12]
    )
    assert "untrusted.example" not in str(error.value)
