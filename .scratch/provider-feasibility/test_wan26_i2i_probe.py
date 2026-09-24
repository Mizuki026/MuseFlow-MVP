from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import wan26_i2i_probe as probe  # noqa: E402


def _small_png() -> bytes:
    return probe.fixed_reference_png(256, 256)


def test_normalize_parameters_freezes_single_image_edit_request() -> None:
    parameters = probe.normalize_parameters("  edit the image  ")
    assert parameters["model"] == "wan2.6-image"
    content = parameters["input"]["messages"][0]["content"]
    assert content == [{"text": "edit the image"}, {"image": "__IMAGE_DATA_URL__"}]
    assert parameters["parameters"] == {
        "enable_interleave": False,
        "n": 1,
        "size": "1K",
        "prompt_extend": False,
        "watermark": False,
    }


@pytest.mark.parametrize("prompt", ["", " \n ", "x" * 2_001])
def test_normalize_prompt_rejects_empty_or_overlong(prompt: str) -> None:
    with pytest.raises(ValueError):
        probe.normalize_prompt(prompt)


def test_input_image_requires_allowed_size_rgb_single_frame_and_complete_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = probe.inspect_input_image(_small_png())
    assert (metadata.format, metadata.content_type, metadata.width, metadata.height) == (
        "PNG",
        "image/png",
        256,
        256,
    )
    assert metadata.mode == "RGB"
    assert metadata.frame_count == 1
    with pytest.raises(ValueError, match="dimensions"):
        probe.inspect_input_image(probe.fixed_reference_png(100, 256))
    monkeypatch.setattr(probe, "INPUT_MAX_SIDE", 255)
    with pytest.raises(ValueError, match="dimensions"):
        probe.inspect_input_image(_small_png())
    monkeypatch.setattr(probe, "INPUT_MAX_SIDE", 2_048)
    with pytest.raises(ValueError, match="fully decoded"):
        probe.inspect_input_image(_small_png()[:-12])
    from PIL import Image

    image = Image.new("RGBA", (256, 256), (20, 40, 60, 128))
    output = io.BytesIO()
    image.save(output, format="PNG")
    with pytest.raises(ValueError, match="RGB"):
        probe.inspect_input_image(output.getvalue())

    bmp = Image.new("RGB", (256, 256), (20, 40, 60))
    output = io.BytesIO()
    bmp.save(output, format="BMP")
    with pytest.raises(ValueError, match="allowlist"):
        probe.inspect_input_image(output.getvalue())


def test_input_byte_pixel_and_aspect_limits_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    content = _small_png()
    monkeypatch.setattr(probe, "INPUT_MAX_BYTES", len(content) - 1)
    with pytest.raises(ValueError, match="byte limit"):
        probe.inspect_input_image(content)

    monkeypatch.setattr(probe, "INPUT_MAX_BYTES", 6_000_000)
    monkeypatch.setattr(probe, "INPUT_MAX_PIXELS", 256 * 256 - 1)
    with pytest.raises(ValueError, match="pixel limit"):
        probe.inspect_input_image(content)

    from PIL import Image

    image = Image.new("RGB", (256, 512), (20, 40, 60))
    output = io.BytesIO()
    image.save(output, format="PNG")
    monkeypatch.setattr(probe, "INPUT_MAX_PIXELS", 4_194_304)
    monkeypatch.setattr(probe, "INPUT_MAX_ASPECT_RATIO", 1.5)
    with pytest.raises(ValueError, match="aspect ratio"):
        probe.inspect_input_image(output.getvalue())


@pytest.mark.parametrize(
    ("image_format", "content_type"),
    [("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_frozen_input_format_subsets_accept_rgb_jpeg_and_webp(
    image_format: str, content_type: str
) -> None:
    from PIL import Image

    image = Image.new("RGB", (256, 256), (30, 80, 120))
    output = io.BytesIO()
    image.save(output, format=image_format)
    metadata = probe.inspect_input_image(output.getvalue())
    assert metadata.format == image_format
    assert metadata.content_type == content_type
    assert metadata.mode == "RGB"
    assert metadata.frame_count == 1


def test_animated_webp_is_rejected() -> None:
    from PIL import Image, features

    if not features.check("webp"):
        pytest.skip("Pillow was built without WebP support")
    frames = [
        Image.new("RGB", (256, 256), (30, 80, 120)),
        Image.new("RGB", (256, 256), (120, 80, 30)),
    ]
    output = io.BytesIO()
    frames[0].save(
        output,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    with pytest.raises(ValueError, match="multi-frame"):
        probe.inspect_input_image(output.getvalue())


def test_base64_body_size_and_integrity_guard() -> None:
    content = _small_png()
    metadata = probe.inspect_input_image(content)
    body = probe.build_request_body("edit this", metadata, content)
    decoded = json.loads(body)
    image_data_url = decoded["input"]["messages"][0]["content"][1]["image"]
    assert image_data_url.startswith("data:image/png;base64,")
    assert len(body) < probe.BASE64_JSON_MAX_BYTES
    with pytest.raises(ValueError, match="changed"):
        probe.build_request_body("edit this", metadata, content + b"x")
    previous_limit = probe.BASE64_JSON_MAX_BYTES
    probe.BASE64_JSON_MAX_BYTES = 100
    try:
        with pytest.raises(ValueError, match="body limit"):
            probe.build_request_body("edit this", metadata, content)
    finally:
        probe.BASE64_JSON_MAX_BYTES = previous_limit


@pytest.mark.parametrize(
    ("status", "code", "submit", "expected"),
    [
        (400, "DataInspectionFailed", True, "content_safety_rejected"),
        (400, "IPInfringementSuspect", True, "content_safety_rejected"),
        (401, "InvalidApiKey", True, "configuration_or_permission"),
        (403, "Workspace.AccessDenied", True, "configuration_or_permission"),
        (429, "Throttling.RateQuota", True, "rate_limited"),
        (429, "InsufficientBalance", True, "billing_or_account_quota"),
        (400, "FreeTierOnly", True, "billing_or_account_quota"),
        (403, "AccessDenied.Free-Tier-Only", True, "billing_or_account_quota"),
        (429, "ConcurrentQuota", True, "rate_or_account_quota_unclassified"),
        (400, "InvalidFile.ImageSize", True, "invalid_input_or_parameters"),
        (503, "ServiceUnavailable", True, "provider_service_error"),
        (None, None, True, "submission_state_unknown"),
        (None, None, False, "protocol_or_provider_error"),
    ],
)
def test_error_classification(
    status: int | None, code: str | None, submit: bool, expected: str
) -> None:
    assert probe.classify_error(status, code, submit=submit) == expected


def test_create_and_poll_response_parsing() -> None:
    task_id = probe.extract_task_id({"output": {"task_id": "remote-task"}})
    assert probe.redact_remote_id(task_id) == probe.redact_remote_id("remote-task")
    assert probe.parse_task_status({"output": {"task_status": "running"}}) == "RUNNING"
    assert probe.parse_task_status({"output": {"task_status": "SUCCEEDED"}}) == "SUCCEEDED"
    with pytest.raises(ValueError, match="missing"):
        probe.extract_task_id({"output": {}})
    with pytest.raises(ValueError, match="recognized"):
        probe.parse_task_status({"output": {"task_status": "SOMETHING_ELSE"}})


def test_result_url_policy_requires_https_exact_host_and_no_credentials() -> None:
    safe = "https://dashscope-a717.oss-accelerate.aliyuncs.com/image.png"
    decision = probe.inspect_result_url(safe)
    assert decision.allowed is True
    assert decision.host_sha256_12
    assert "image.png" not in decision.reason
    assert not probe.inspect_result_url(
        "http://dashscope-a717.oss-accelerate.aliyuncs.com/x"
    ).allowed
    assert not probe.inspect_result_url(
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/x"
    ).allowed
    assert not probe.inspect_result_url(
        "https://evil-dashscope-result-bj.oss-cn-beijing.aliyuncs.com/x"
    ).allowed
    assert not probe.inspect_result_url(
        "https://user:pass@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/x"
    ).allowed


def test_result_url_extraction_and_invalid_media_are_fail_closed() -> None:
    payload = {
        "output": {
            "choices": [
                {"message": {"content": [{"type": "image", "image": "https://safe.invalid/x"}]}}
            ]
        }
    }
    assert probe.extract_result_url(payload) == "https://safe.invalid/x"
    with pytest.raises(ValueError, match="missing"):
        probe.extract_result_url({"output": {"choices": []}})
    duplicate = {
        "output": {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"image": "https://safe.example/one"},
                            {"image": "https://safe.example/two"},
                        ]
                    }
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="other than one"):
        probe.extract_result_url(duplicate)
    with pytest.raises(ValueError, match="Content-Type"):
        probe.decode_result(_small_png(), "text/plain")


def test_result_requires_full_png_decode_and_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    content = _small_png()
    metadata = probe.decode_result(content, "image/png; charset=binary")
    assert metadata.content_type == "image/png"
    assert metadata.size_bytes == len(content)
    assert metadata.sha256 == __import__("hashlib").sha256(content).hexdigest()
    monkeypatch.setattr(probe, "RESULT_MAX_BYTES", len(content) - 1)
    with pytest.raises(ValueError, match="byte limit"):
        probe.decode_result(content, "image/png")
    monkeypatch.setattr(probe, "RESULT_MAX_BYTES", 20 * 1024 * 1024)
    with pytest.raises(ValueError, match="fully decoded"):
        probe.decode_result(content[:-12], "image/png")
    monkeypatch.setattr(probe, "OUTPUT_MAX_SIDE", 255)
    with pytest.raises(ValueError, match="dimension ceiling"):
        probe.decode_result(content, "image/png")


def test_endpoint_accepts_only_beijing_workspace_dedicated_https_host() -> None:
    assert probe.endpoint_origin("https://workspace.cn-beijing.maas.aliyuncs.com") == (
        "https://workspace.cn-beijing.maas.aliyuncs.com"
    )
    with pytest.raises(ValueError, match="Beijing"):
        probe.endpoint_origin("https://dashscope.aliyuncs.com")
    with pytest.raises(ValueError, match="HTTPS"):
        probe.endpoint_origin("http://workspace.cn-beijing.maas.aliyuncs.com")
    diagnostic = probe.endpoint_diagnostic(
        "https://sensitive-workspace.cn-beijing.maas.aliyuncs.com"
    )
    assert "sensitive-workspace" not in diagnostic
    assert diagnostic == "region=cn-beijing, workspace_endpoint=dedicated"


def test_probe_failure_discards_raw_provider_error_code() -> None:
    error = probe.ProbeFailure("create:configuration_or_permission", http_status=403)
    assert error.category == "create:configuration_or_permission"
    assert not hasattr(error, "provider_code")


def test_deadline_does_not_allow_an_expired_provider_request() -> None:
    with pytest.raises(probe.ProbeFailure, match="probe_total_timeout"):
        probe._request_timeout(probe.time.monotonic() - 1)


def test_public_address_policy_blocks_local_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("127.0.0.1", 0))]
    )
    with pytest.raises(ValueError, match="non-public"):
        probe.resolve_public_addresses("result.example")


def test_redacted_id_does_not_return_raw_value() -> None:
    remote_id = "account-task-identity-123"
    digest = probe.redact_remote_id(remote_id)
    assert digest and len(digest) == 12
    assert remote_id not in digest


def test_remote_task_state_is_recoverable_only_within_safe_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "private-task-state.json"
    now = 1_800_000_000.0
    monkeypatch.setattr(probe, "TASK_STATE_PATH", state_path)
    monkeypatch.setattr(probe.time, "time", lambda: now)
    probe._save_state("remote-task-id")
    assert probe._load_state() == "remote-task-id"

    monkeypatch.setattr(probe.time, "time", lambda: now + probe.TASK_STATE_TTL_SECONDS + 1)
    with pytest.raises(probe.ProbeFailure, match="remote_task_state_expired"):
        probe._load_state()
    assert not state_path.exists()
