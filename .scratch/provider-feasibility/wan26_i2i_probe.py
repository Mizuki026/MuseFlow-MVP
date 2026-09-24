from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import time
import zlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

REGION = "cn-beijing"
REGION_LABEL = "China (Beijing)"
MODEL = "wan2.6-image"
PROVIDER_NAME = "dashscope"
PROFILE_NAME = "dashscope-wan2.6-image-cn-beijing-edit"
CAPABILITY_VERSION = "official-doc-snapshot-2026-09-24"
CREATE_PATH = "/api/v1/services/aigc/image-generation/generation"
TASK_PATH = "/api/v1/tasks/"
PROMPT = (
    "Edit this simple reference illustration into a polished watercolor scene. "
    "Keep the central warm sun above a calm deep-blue sea and preserve the square composition."
)
INPUT_MAX_BYTES = 6_000_000
INPUT_MIN_SIDE = 240
INPUT_MAX_SIDE = 2_048
INPUT_MAX_PIXELS = 4_194_304
INPUT_MAX_ASPECT_RATIO = 4.0
OUTPUT_MAX_SIDE = 1_440
BASE64_JSON_MAX_BYTES = 8_100_000
PROVIDER_RESPONSE_MAX_BYTES = 1_048_576
RESULT_MAX_BYTES = 20 * 1024 * 1024
RESULT_HOSTS = frozenset({"dashscope-a717.oss-accelerate.aliyuncs.com"})
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0
WRITE_TIMEOUT_SECONDS = 30.0
POOL_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 600.0
MAX_POLL_REQUESTS = 60
TASK_STATE_TTL_SECONDS = 23 * 60 * 60
TASK_STATE_PATH = Path(__file__).with_name("private-task-state.json")


class ProbeFailure(RuntimeError):
    def __init__(
        self,
        category: str,
        *,
        http_status: int | None = None,
        remote_id_hash: str | None = None,
        result_host_hash: str | None = None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.http_status = http_status
        self.remote_id_hash = remote_id_hash
        self.result_host_hash = result_host_hash


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    format: str
    content_type: str
    width: int
    height: int
    mode: str
    frame_count: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ResultUrlDecision:
    allowed: bool
    host_sha256_12: str | None
    reason: str


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def fixed_reference_png(width: int = 1_024, height: int = 1_024) -> bytes:
    """Create a deterministic RGB image in memory; never save a generated image."""
    if width < 1 or height < 1:
        raise ValueError("fixture dimensions must be positive")
    raw = bytearray()
    rng = 0x4D555345
    for y in range(height):
        raw.append(0)
        for x in range(width):
            rng = (1_664_525 * rng + 1_013_904_223) & 0xFFFFFFFF
            grain = ((rng >> 24) % 7) - 3
            sun = (x - width * 3 // 4) ** 2 + (y - height // 4) ** 2 < (width // 8) ** 2
            if sun:
                color = (238, 183, 104)
            elif y > height * 3 // 5:
                color = (24, 61, 82)
            else:
                color = (74, 113, 142)
            raw.extend(min(255, max(0, channel + grain)) for channel in color)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), level=1))
        + png_chunk(b"IEND", b"")
    )


def normalize_prompt(prompt: str) -> str:
    normalized = prompt.strip()
    if not normalized:
        raise ValueError("prompt is empty")
    if len(normalized) > 2_000:
        raise ValueError("prompt exceeds the official 2000-character limit")
    return normalized


def inspect_input_image(content: bytes) -> ImageMetadata:
    if not content:
        raise ValueError("input image is empty")
    if len(content) > INPUT_MAX_BYTES:
        raise ValueError("input image exceeds the probe byte limit")
    try:
        from PIL import Image, ImageFile
    except ImportError as error:
        raise RuntimeError("Pillow is required for complete image decoding") from error

    ImageFile.LOAD_TRUNCATED_IMAGES = False
    content_types = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }
    try:
        with Image.open(io_bytes(content)) as image:
            image_format = image.format or ""
            width, height = image.size
            mode = image.mode
            frame_count = int(getattr(image, "n_frames", 1))
            if image_format not in content_types:
                raise ValueError("input image format is not on the probe allowlist")
            if (
                not INPUT_MIN_SIDE <= width <= INPUT_MAX_SIDE
                or not INPUT_MIN_SIDE <= height <= INPUT_MAX_SIDE
            ):
                raise ValueError("input image dimensions are outside the probe limits")
            if width * height > INPUT_MAX_PIXELS:
                raise ValueError("input image exceeds the probe pixel limit")
            if max(width / height, height / width) > INPUT_MAX_ASPECT_RATIO:
                raise ValueError("input image aspect ratio is outside the probe limit")
            if frame_count != 1:
                raise ValueError("animated or multi-frame images are not allowed")
            if mode != "RGB":
                raise ValueError(
                    "input color mode must be RGB; alpha and other modes are not allowed"
                )
            image.verify()
        with Image.open(io_bytes(content)) as image:
            image.load()
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("input image is corrupt or cannot be fully decoded") from error

    return ImageMetadata(
        format=image_format,
        content_type=content_types[image_format],
        width=width,
        height=height,
        mode=mode,
        frame_count=frame_count,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def io_bytes(content: bytes):
    from io import BytesIO

    return BytesIO(content)


def normalize_parameters(prompt: str) -> dict[str, Any]:
    return {
        "model": MODEL,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"text": normalize_prompt(prompt)},
                        {"image": "__IMAGE_DATA_URL__"},
                    ],
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


def build_request_body(prompt: str, image: ImageMetadata, content: bytes) -> bytes:
    if hashlib.sha256(content).hexdigest() != image.sha256 or len(content) != image.size_bytes:
        raise ValueError("input image changed after inspection")
    if len(content) > INPUT_MAX_BYTES:
        raise ValueError("input image exceeds the probe byte limit")
    payload = normalize_parameters(prompt)
    payload["input"]["messages"][0]["content"][1]["image"] = (
        f"data:{image.content_type};base64,{base64.b64encode(content).decode('ascii')}"
    )
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > BASE64_JSON_MAX_BYTES:
        raise ValueError("Base64 JSON request exceeds the probe body limit")
    return body


def redact_remote_id(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _provider_code(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    for value in (
        payload.get("code"),
        payload.get("error_code"),
        output.get("code") if isinstance(output, dict) else None,
    ):
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,120}", value):
            return value[:120]
    return None


def classify_error(http_status: int | None, provider_code: str | None, *, submit: bool) -> str:
    code = re.sub(r"[^a-z0-9]", "", (provider_code or "").lower())
    if any(
        marker in code for marker in ("datainspection", "ipinfringement", "safety", "moderation")
    ):
        return "content_safety_rejected"
    if any(marker in code for marker in ("ratequota", "burstrate", "throttlingrate")):
        return "rate_limited"
    if any(
        marker in code
        for marker in (
            "allocationquota",
            "insufficientbalance",
            "billing",
            "payment",
            "freetier",
        )
    ):
        return "billing_or_account_quota"
    if "quota" in code:
        return "rate_or_account_quota_unclassified"
    if any(
        marker in code
        for marker in ("invalidapikey", "accessdenied", "workspace", "authentication")
    ):
        return "configuration_or_permission"
    if any(
        marker in code
        for marker in ("invalidimage", "invalidfile", "invalidparameter", "badrequest")
    ):
        return "invalid_input_or_parameters"
    if http_status == 429:
        return "rate_or_account_quota_unclassified"
    if http_status in {401, 403}:
        return "configuration_or_permission"
    if http_status == 400:
        return "invalid_input_or_parameters"
    if http_status is not None and http_status >= 500:
        return "provider_service_error"
    if http_status in {408, 425}:
        return "provider_transport_timeout"
    if http_status is None and submit:
        return "submission_state_unknown"
    return "protocol_or_provider_error"


def parse_task_status(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    output = payload.get("output")
    raw = output.get("task_status") if isinstance(output, dict) else None
    if not isinstance(raw, str):
        raise ValueError("task status is missing")
    status = raw.upper()
    if status not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}:
        raise ValueError("task status is not recognized")
    return status


def extract_task_id(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("create response is not a JSON object")
    output = payload.get("output")
    task_id = output.get("task_id") if isinstance(output, dict) else None
    if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
        raise ValueError("remote task ID is missing or invalid")
    return task_id


def extract_result_url(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("result response is not a JSON object")
    output = payload.get("output")
    if not isinstance(output, dict):
        raise ValueError("task output is missing")
    choices = output.get("choices")
    if not isinstance(choices, list):
        raise ValueError("result choices are missing")
    result_urls: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            value = item.get("image") if isinstance(item, dict) else None
            if isinstance(value, str) and value:
                result_urls.append(value)
    if not result_urls:
        raise ValueError("result image URL is missing")
    if len(result_urls) != 1:
        raise ValueError("provider returned an output count other than one")
    return result_urls[0]


def inspect_result_url(url: str, allowed_hosts: frozenset[str] = RESULT_HOSTS) -> ResultUrlDecision:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname.lower() if parsed.hostname else None
        has_credentials = parsed.username is not None or parsed.password is not None
        port = parsed.port
    except ValueError:
        return ResultUrlDecision(False, None, "malformed_url")
    host_digest = redact_remote_id(host) if host else None
    if parsed.scheme != "https" or not host or has_credentials or port not in (None, 443):
        return ResultUrlDecision(False, host_digest, "url_scheme_or_authority_rejected")
    if host not in allowed_hosts:
        return ResultUrlDecision(False, host_digest, "result_host_not_allowlisted")
    return ResultUrlDecision(True, host_digest, "allowed")


def resolve_public_addresses(host: str) -> list[str]:
    try:
        addresses = sorted(
            {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        )
    except OSError as error:
        raise ValueError("result host DNS lookup failed") from error
    if not addresses:
        raise ValueError("result host has no DNS addresses")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global or address.is_multicast:
            raise ValueError("result host resolves to a non-public IP")
    return addresses


def decode_result(content: bytes, content_type: str) -> ImageMetadata:
    if content_type.split(";", 1)[0].strip().lower() != "image/png":
        raise ValueError("result Content-Type is not image/png")
    if not content or len(content) > RESULT_MAX_BYTES:
        raise ValueError("result image is empty or exceeds the result byte limit")
    return inspect_png_result(content)


def inspect_png_result(content: bytes) -> ImageMetadata:
    try:
        from PIL import Image, ImageFile
    except ImportError as error:
        raise RuntimeError("Pillow is required for complete image decoding") from error
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with Image.open(io_bytes(content)) as image:
            image_format = image.format or ""
            width, height = image.size
            mode = image.mode
            frame_count = int(getattr(image, "n_frames", 1))
            if image_format != "PNG" or frame_count != 1:
                raise ValueError("result is not a single-frame PNG")
            if width <= 0 or height <= 0:
                raise ValueError("result dimensions are invalid")
            if width > OUTPUT_MAX_SIDE or height > OUTPUT_MAX_SIDE:
                raise ValueError("result image exceeds the probe dimension ceiling")
            image.verify()
        with Image.open(io_bytes(content)) as image:
            image.load()
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("result image is corrupt or cannot be fully decoded") from error
    return ImageMetadata(
        format=image_format,
        content_type="image/png",
        width=width,
        height=height,
        mode=mode,
        frame_count=frame_count,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _read_bounded(
    response: httpx.Response, maximum: int, *, deadline: float | None = None
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        if deadline is not None and time.monotonic() >= deadline:
            raise ProbeFailure("probe_total_timeout")
        size += len(chunk)
        if size > maximum:
            raise ValueError("response exceeds bounded read limit")
        chunks.append(chunk)
    if deadline is not None and time.monotonic() >= deadline:
        raise ProbeFailure("probe_total_timeout")
    return b"".join(chunks)


def _json_response(response: httpx.Response, stage: str, *, deadline: float) -> dict[str, Any]:
    try:
        body = _read_bounded(response, PROVIDER_RESPONSE_MAX_BYTES, deadline=deadline)
        payload = json.loads(body)
    except ProbeFailure:
        raise
    except Exception as error:
        raise ProbeFailure(f"{stage}_protocol_error", http_status=response.status_code) from error
    if not isinstance(payload, dict):
        raise ProbeFailure(f"{stage}_protocol_error", http_status=response.status_code)
    return payload


def endpoint_origin(raw_host: str) -> str:
    candidate = raw_host.strip()
    parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("DASHSCOPE_API_HOST must be an HTTPS endpoint")
    if parsed.query or parsed.fragment:
        raise ValueError("DASHSCOPE_API_HOST cannot contain query or fragment")
    host = parsed.hostname.lower()
    if not host.endswith(".cn-beijing.maas.aliyuncs.com"):
        raise ValueError("DASHSCOPE_API_HOST is not a Beijing workspace-dedicated endpoint")
    port = parsed.port
    if port not in (None, 443):
        raise ValueError("DASHSCOPE_API_HOST has an unsupported port")
    return f"https://{host}"


def endpoint_diagnostic(origin: str) -> str:
    del origin
    return f"region={REGION}, workspace_endpoint=dedicated"


def _load_state() -> str:
    try:
        state = json.loads(TASK_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeFailure("configuration_or_permission") from error
    if not isinstance(state, dict) or state.get("model") != MODEL or state.get("region") != REGION:
        raise ProbeFailure("configuration_or_permission")
    task_id = state.get("task_id")
    saved_at = state.get("saved_at_epoch")
    if (
        not isinstance(task_id, str)
        or not task_id
        or not isinstance(saved_at, (int, float))
        or isinstance(saved_at, bool)
        or saved_at > time.time()
    ):
        raise ProbeFailure("configuration_or_permission")
    if time.time() - saved_at > TASK_STATE_TTL_SECONDS:
        TASK_STATE_PATH.unlink(missing_ok=True)
        raise ProbeFailure("remote_task_state_expired")
    return task_id


def _save_state(task_id: str) -> None:
    state = {
        "model": MODEL,
        "region": REGION,
        "task_id": task_id,
        "saved_at_epoch": time.time(),
    }
    TASK_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def _payload_code_from_response(
    response: httpx.Response, stage: str, *, submit: bool, deadline: float
) -> None:
    payload: object = None
    try:
        payload = json.loads(
            _read_bounded(response, PROVIDER_RESPONSE_MAX_BYTES, deadline=deadline)
        )
    except ProbeFailure:
        raise
    except Exception:
        pass
    code = _provider_code(payload)
    category = classify_error(response.status_code, code, submit=submit)
    raise ProbeFailure(
        f"{stage}:{category}",
        http_status=response.status_code,
    )


def _request_timeout(deadline: float) -> httpx.Timeout:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeFailure("probe_total_timeout")
    return httpx.Timeout(
        connect=min(CONNECT_TIMEOUT_SECONDS, remaining),
        read=min(READ_TIMEOUT_SECONDS, remaining),
        write=min(WRITE_TIMEOUT_SECONDS, remaining),
        pool=min(POOL_TIMEOUT_SECONDS, remaining),
    )


def _create_once(
    client: httpx.Client,
    origin: str,
    api_key: str,
    body: bytes,
    *,
    deadline: float,
) -> tuple[str, int, str | None]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    try:
        with client.stream(
            "POST",
            f"{origin}{CREATE_PATH}",
            content=body,
            headers=headers,
            timeout=_request_timeout(deadline),
        ) as response:
            if not 200 <= response.status_code < 300:
                _payload_code_from_response(response, "create", submit=True, deadline=deadline)
            payload = _json_response(response, "create", deadline=deadline)
            status_code = response.status_code
    except ProbeFailure:
        raise
    except httpx.HTTPError as error:
        raise ProbeFailure("submission_state_unknown") from error
    try:
        return (
            extract_task_id(payload),
            status_code,
            payload.get("request_id") if isinstance(payload.get("request_id"), str) else None,
        )
    except ValueError as error:
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        raise ProbeFailure(
            "submission_state_unknown",
            remote_id_hash=redact_remote_id(request_id if isinstance(request_id, str) else None),
        ) from error


def _poll_once(
    client: httpx.Client,
    origin: str,
    api_key: str,
    task_id: str,
    *,
    deadline: float,
) -> tuple[dict[str, Any], int]:
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with client.stream(
            "GET",
            f"{origin}{TASK_PATH}{quote(task_id, safe='')}",
            headers=headers,
            timeout=_request_timeout(deadline),
        ) as response:
            if not 200 <= response.status_code < 300:
                _payload_code_from_response(response, "poll", submit=False, deadline=deadline)
            return _json_response(response, "poll", deadline=deadline), response.status_code
    except ProbeFailure:
        raise
    except httpx.HTTPError as error:
        raise ProbeFailure(
            "poll_transport_error", remote_id_hash=redact_remote_id(task_id)
        ) from error


def _download_once(
    client: httpx.Client, url: str, *, deadline: float
) -> tuple[bytes, ImageMetadata, str]:
    decision = inspect_result_url(url)
    if not decision.allowed:
        raise ProbeFailure("result_host_or_url_rejected", result_host_hash=decision.host_sha256_12)
    host = urlsplit(url).hostname
    assert host is not None
    try:
        resolve_public_addresses(host)
    except ValueError as error:
        raise ProbeFailure(
            "result_dns_or_ip_rejected", result_host_hash=decision.host_sha256_12
        ) from error
    try:
        with client.stream(
            "GET",
            url,
            follow_redirects=False,
            timeout=_request_timeout(deadline),
        ) as response:
            if 300 <= response.status_code < 400:
                raise ProbeFailure(
                    "result_redirect_rejected",
                    http_status=response.status_code,
                    result_host_hash=decision.host_sha256_12,
                )
            if response.status_code != 200:
                raise ProbeFailure(
                    "result_fetch_http_error",
                    http_status=response.status_code,
                    result_host_hash=decision.host_sha256_12,
                )
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            content_length = response.headers.get("content-length")
            if content_type != "image/png":
                raise ProbeFailure(
                    "result_media_type_rejected",
                    http_status=response.status_code,
                    result_host_hash=decision.host_sha256_12,
                )
            if content_length is not None:
                try:
                    if int(content_length) > RESULT_MAX_BYTES:
                        raise ProbeFailure("result_size_rejected", http_status=response.status_code)
                except ValueError as error:
                    if isinstance(error, ProbeFailure):
                        raise
                    raise ProbeFailure("result_protocol_error") from error
            content = _read_bounded(response, RESULT_MAX_BYTES, deadline=deadline)
            metadata = decode_result(content, content_type)
            if time.monotonic() >= deadline:
                raise ProbeFailure("probe_total_timeout")
            return content, metadata, decision.host_sha256_12 or "unavailable"
    except ProbeFailure:
        raise
    except (httpx.HTTPError, ValueError) as error:
        raise ProbeFailure(
            "result_fetch_or_decode_error", result_host_hash=decision.host_sha256_12
        ) from error


def _load_reference(path: Path | None) -> tuple[bytes, ImageMetadata]:
    content = path.read_bytes() if path else fixed_reference_png()
    return content, inspect_input_image(content)


def _evidence_base(input_image: ImageMetadata, body_size: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "started",
        "provider_name": PROVIDER_NAME,
        "provider_profile": PROFILE_NAME,
        "model_name": MODEL,
        "capability_version": CAPABILITY_VERSION,
        "region": REGION,
        "endpoint": f"https://<workspace>.cn-beijing.maas.aliyuncs.com{CREATE_PATH}",
        "input": asdict(input_image),
        "request_body_bytes": body_size,
        "parameters": {
            "mode": "image_editing",
            "enable_interleave": False,
            "n": 1,
            "size": "1K",
            "prompt_extend": False,
            "watermark": False,
            "prompt": PROMPT,
        },
        "create_requests": 0,
        "create_http_status": None,
        "poll_requests": 0,
        "poll_http_statuses": [],
        "task_status_sequence": [],
        "provider_request_id_sha256_12": None,
        "remote_task_id_sha256_12": None,
        "elapsed_seconds": None,
        "result": None,
        "error": None,
        "external_exactly_once": False,
    }


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ProbeFailure("configuration_or_permission")
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one explicitly authorized Wan2.6 I2I probe.")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate the fixed reference image without network access",
    )
    parser.add_argument(
        "--input-image",
        type=Path,
        help="optional local image; otherwise use fixed in-memory RGB PNG",
    )
    parser.add_argument(
        "--authorize-real-request",
        action="store_true",
        help="authorize exactly one image-creation POST",
    )
    parser.add_argument(
        "--confirm-max-cost-cny", type=float, help="must be exactly 0.20 for this one-image probe"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume polling a previously saved task ID; does not create a task",
    )
    parser.add_argument(
        "--evidence-output",
        type=Path,
        default=Path(__file__).with_name("evidence") / "i2i-probe.json",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.check_only:
        try:
            content, metadata = _load_reference(args.input_image)
            body = build_request_body(PROMPT, metadata, content)
        except Exception:
            print("Check failed:", "input_validation_error")
            return 2
        print(
            "No request sent. Input:",
            f"{metadata.format} {metadata.width}x{metadata.height}, "
            f"{metadata.size_bytes} bytes, RGB, 1 frame.",
        )
        print(f"Input SHA-256: {metadata.sha256}")
        print(
            f"Serialized Base64 JSON body: {len(body)} bytes; "
            f"configured ceiling: {BASE64_JSON_MAX_BYTES} bytes."
        )
        print(
            f"Prompt length: {len(normalize_prompt(PROMPT))} characters; "
            f"model: {MODEL}; outputs: 1."
        )
        return 0

    if not args.authorize_real_request:
        print(
            "No request sent. Real creation requires --authorize-real-request and "
            "--confirm-max-cost-cny 0.20."
        )
        return 2
    if args.confirm_max_cost_cny != 0.20:
        print(
            "No request sent. Confirm the documented maximum of CNY 0.20 with "
            "--confirm-max-cost-cny 0.20."
        )
        return 2
    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("No request sent. DASHSCOPE_API_KEY is not configured.")
        return 2
    try:
        origin = endpoint_origin(os.environ.get("DASHSCOPE_API_HOST", ""))
    except ValueError:
        print(
            "No request sent. DASHSCOPE_API_HOST is missing or is not a Beijing "
            "workspace HTTPS endpoint."
        )
        return 2
    if args.resume and not TASK_STATE_PATH.exists():
        print("No request sent. There is no saved remote task to resume.")
        return 2
    if not args.resume and TASK_STATE_PATH.exists():
        print(
            "No request sent. A remote task state already exists; use --resume "
            "to avoid duplicate creation."
        )
        return 2
    if args.evidence_output.exists():
        print(
            "No request sent. The evidence target already exists; choose a new path "
            "to avoid overwriting it."
        )
        return 2

    try:
        content, image = _load_reference(args.input_image)
        body = build_request_body(PROMPT, image, content) if not args.resume else b""
    except Exception:
        print("No request sent. Input configuration or validation failed.")
        return 2

    print(f"Preflight: endpoint=https://<workspace>.cn-beijing.maas.aliyuncs.com{CREATE_PATH}")
    print(
        f"Preflight: {endpoint_diagnostic(origin)}; model={MODEL}; "
        "output_count=1; max_cost=CNY 0.20."
    )
    print(
        f"Preflight: input={image.format} {image.width}x{image.height}, "
        f"{image.size_bytes} bytes, RGB, sha256={image.sha256}."
    )
    print(
        f"Preflight: request_body_bytes={len(body)}; "
        f"create_calls={0 if args.resume else 1}; automatic_create_retries=0."
    )
    print(
        "Preflight: unknown submission outcome could cause duplicate billing if "
        "separately resubmitted; this run will never resubmit."
    )

    evidence = _evidence_base(image, len(body))
    started = time.monotonic()
    deadline = started + TOTAL_TIMEOUT_SECONDS
    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=WRITE_TIMEOUT_SECONDS,
        pool=POOL_TIMEOUT_SECONDS,
    )
    task_id: str | None = None
    try:
        transport = httpx.HTTPTransport(retries=0)
        with httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            if args.resume:
                task_id = _load_state()
            else:
                evidence["create_requests"] = 1
                task_id, create_status, provider_request_id = _create_once(
                    client,
                    origin,
                    os.environ["DASHSCOPE_API_KEY"],
                    body,
                    deadline=deadline,
                )
                evidence["create_http_status"] = create_status
                evidence["provider_request_id_sha256_12"] = redact_remote_id(provider_request_id)
                evidence["remote_task_id_sha256_12"] = redact_remote_id(task_id)
                _save_state(task_id)

            statuses: list[str] = []
            for poll_index in range(MAX_POLL_REQUESTS):
                if time.monotonic() >= deadline:
                    raise ProbeFailure(
                        "poll_total_timeout", remote_id_hash=redact_remote_id(task_id)
                    )
                evidence["poll_requests"] += 1
                response, poll_status = _poll_once(
                    client,
                    origin,
                    os.environ["DASHSCOPE_API_KEY"],
                    task_id,
                    deadline=deadline,
                )
                evidence["poll_http_statuses"].append(poll_status)
                try:
                    status = parse_task_status(response)
                except ValueError as error:
                    raise ProbeFailure(
                        "poll_protocol_error", remote_id_hash=redact_remote_id(task_id)
                    ) from error
                if not statuses or statuses[-1] != status:
                    statuses.append(status)
                evidence["task_status_sequence"] = statuses
                if status in {"PENDING", "RUNNING"}:
                    if poll_index + 1 >= MAX_POLL_REQUESTS:
                        raise ProbeFailure(
                            "poll_request_limit", remote_id_hash=redact_remote_id(task_id)
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProbeFailure(
                            "poll_total_timeout", remote_id_hash=redact_remote_id(task_id)
                        )
                    time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
                    continue
                if status == "FAILED":
                    raise ProbeFailure(
                        "task_failed:"
                        + classify_error(200, _provider_code(response), submit=False),
                        http_status=200,
                        remote_id_hash=redact_remote_id(task_id),
                    )
                if status != "SUCCEEDED":
                    raise ProbeFailure(
                        f"task_{status.lower()}", remote_id_hash=redact_remote_id(task_id)
                    )
                try:
                    result_url = extract_result_url(response)
                except ValueError as error:
                    raise ProbeFailure(
                        "result_protocol_error", remote_id_hash=redact_remote_id(task_id)
                    ) from error
                _, result_image, host_hash = _download_once(client, result_url, deadline=deadline)
                evidence["result"] = asdict(result_image) | {"host_sha256_12": host_hash}
                evidence["status"] = "success"
                break
            else:
                raise ProbeFailure("poll_request_limit", remote_id_hash=redact_remote_id(task_id))
    except ProbeFailure as error:
        evidence["status"] = "failed"
        evidence["error"] = {
            "category": error.category,
            "http_status": error.http_status,
            "remote_task_id_sha256_12": error.remote_id_hash or redact_remote_id(task_id),
            "result_host_sha256_12": error.result_host_hash,
        }
        if error.category == "submission_state_unknown":
            evidence["external_state_unknown"] = True
    except Exception:
        evidence["status"] = "failed"
        evidence["error"] = {
            "category": "local_unexpected_error",
            "remote_task_id_sha256_12": redact_remote_id(task_id),
        }
    finally:
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if evidence["status"] == "success":
            TASK_STATE_PATH.unlink(missing_ok=True)
        try:
            _write_evidence(args.evidence_output, evidence)
        except (ProbeFailure, OSError):
            print("Evidence output was not written: target exists or is not writable.")

    if evidence["status"] == "success":
        result = evidence["result"]
        print(
            f"Probe succeeded. Statuses={','.join(evidence['task_status_sequence'])}; "
            f"polls={evidence['poll_requests']}; elapsed={evidence['elapsed_seconds']}s."
        )
        print(
            f"Result: {result['content_type']} {result['width']}x{result['height']}, "
            f"{result['size_bytes']} bytes, sha256={result['sha256']}."
        )
        print("Result image was fully decoded; image bytes were not saved.")
        print(f"Redacted evidence: {args.evidence_output}")
        return 0
    error = evidence["error"] or {}
    print(
        f"Probe stopped: {error.get('category', 'unknown')}; "
        f"HTTP={error.get('http_status')}; raw Provider error details were discarded."
    )
    print(f"Redacted evidence: {args.evidence_output}")
    if task_id:
        print(
            "Remote task state remains local for --resume; do not create a "
            "replacement request without new authorization."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
