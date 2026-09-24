from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from museflow.tasks.domain import DomainErrorCode, DomainValidationError, GenerationType


class ProviderProfileUnavailableError(RuntimeError):
    """A frozen or configured profile cannot run in this deployment."""


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    profile_id: str
    provider_name: str
    model_name: str
    capability_version: str
    generation_types: frozenset[GenerationType]
    size_presets: frozenset[str]
    input_content_types: frozenset[str]
    max_reference_bytes: int | None
    adapter_available: bool


TEXT_TO_IMAGE = frozenset({GenerationType.TEXT_TO_IMAGE})
IMAGE_GENERATION = frozenset(GenerationType)
MOCK_IMAGE_FORMATS = frozenset({"image/png", "image/jpeg", "image/webp"})
PROVIDER_PROFILES = MappingProxyType(
    {
        "mock-text-to-image-v1": ProviderProfile(
            profile_id="mock-text-to-image-v1",
            provider_name="mock",
            model_name="mock-deterministic-image",
            capability_version="text-to-image-v1",
            generation_types=TEXT_TO_IMAGE,
            size_presets=frozenset({"1280*1280"}),
            input_content_types=frozenset(),
            max_reference_bytes=None,
            adapter_available=True,
        ),
        "mock-image-generation-v2": ProviderProfile(
            profile_id="mock-image-generation-v2",
            provider_name="mock",
            model_name="mock-deterministic-image",
            capability_version="image-generation-v2",
            generation_types=IMAGE_GENERATION,
            size_presets=frozenset({"1280*1280"}),
            input_content_types=MOCK_IMAGE_FORMATS,
            max_reference_bytes=6_000_000,
            adapter_available=True,
        ),
        "dashscope-wan2.6-t2i-cn-beijing-v1": ProviderProfile(
            profile_id="dashscope-wan2.6-t2i-cn-beijing-v1",
            provider_name="dashscope",
            model_name="wan2.6-t2i",
            capability_version="text-to-image-v1",
            generation_types=TEXT_TO_IMAGE,
            size_presets=frozenset({"1280*1280"}),
            input_content_types=frozenset(),
            max_reference_bytes=None,
            adapter_available=True,
        ),
        "dashscope-wan2.6-image-cn-beijing-edit": ProviderProfile(
            profile_id="dashscope-wan2.6-image-cn-beijing-edit",
            provider_name="dashscope",
            model_name="wan2.6-image",
            capability_version="official-doc-snapshot-2026-09-24",
            generation_types=frozenset({GenerationType.IMAGE_TO_IMAGE}),
            size_presets=frozenset({"1280*1280"}),
            input_content_types=MOCK_IMAGE_FORMATS,
            max_reference_bytes=6_000_000,
            adapter_available=True,
        ),
    }
)
_PROFILE_BY_PROVIDER_AND_TYPE = MappingProxyType(
    {
        ("mock", GenerationType.TEXT_TO_IMAGE): PROVIDER_PROFILES["mock-text-to-image-v1"],
        ("mock", GenerationType.IMAGE_TO_IMAGE): PROVIDER_PROFILES["mock-image-generation-v2"],
        ("dashscope", GenerationType.TEXT_TO_IMAGE): PROVIDER_PROFILES[
            "dashscope-wan2.6-t2i-cn-beijing-v1"
        ],
        ("dashscope", GenerationType.IMAGE_TO_IMAGE): PROVIDER_PROFILES[
            "dashscope-wan2.6-image-cn-beijing-edit"
        ],
    }
)

LEGACY_PROVIDER_PROFILE = "legacy-unfrozen-v1"
LEGACY_PROVIDER_NAME = "legacy-unknown"
LEGACY_MODEL_NAME = "legacy-unknown"
LEGACY_CAPABILITY_VERSION = "legacy-unknown"


def profile_for_id(profile_id: str) -> ProviderProfile | None:
    return PROVIDER_PROFILES.get(profile_id)


def configured_profile(generation_type: GenerationType) -> ProviderProfile:
    provider_name = os.environ.get("MUSEFLOW_PROVIDER", "mock").lower()
    profile = _PROFILE_BY_PROVIDER_AND_TYPE.get((provider_name, generation_type))
    if profile is None:
        raise ProviderProfileUnavailableError("configured provider profile is unavailable")
    return profile


def require_profile_capability(
    profile: ProviderProfile, generation_type: GenerationType, size_preset: str
) -> None:
    if generation_type not in profile.generation_types or size_preset not in profile.size_presets:
        raise DomainValidationError(
            DomainErrorCode.PROVIDER_CAPABILITY_UNSUPPORTED,
            "the selected provider profile does not support this generation request",
        )


def require_profile_available(profile: ProviderProfile) -> None:
    if not profile.adapter_available:
        raise ProviderProfileUnavailableError("provider adapter is unavailable in this deployment")
    if profile.provider_name == "dashscope":
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        api_host = os.environ.get("DASHSCOPE_API_HOST")
        if not api_key or api_key.strip() != api_key or not api_host or not api_host.strip():
            raise ProviderProfileUnavailableError("configured provider profile is unavailable")
        if profile.profile_id == "dashscope-wan2.6-image-cn-beijing-edit":
            try:
                validate_wan26_workspace_origin(api_host)
            except ValueError:
                raise ProviderProfileUnavailableError(
                    "configured provider profile is unavailable"
                ) from None


def validate_wan26_workspace_origin(value: str) -> str:
    raw = value.strip()
    if not raw or any(character.isspace() or ord(character) < 32 for character in raw):
        raise ValueError("invalid Beijing workspace endpoint")
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("invalid Beijing workspace endpoint") from None
    suffix = ".cn-beijing.maas.aliyuncs.com"
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid Beijing workspace endpoint")
    try:
        normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("invalid Beijing workspace endpoint") from None
    labels = normalized.split(".")
    if (
        not normalized.endswith(suffix)
        or len(labels) != 5
        or not labels[0]
        or len(labels[0]) > 63
        or not all(
            character.isascii() and (character.isalnum() or character == "-")
            for character in labels[0]
        )
        or labels[0].startswith("-")
        or labels[0].endswith("-")
    ):
        raise ValueError("invalid Beijing workspace endpoint")
    return f"https://{normalized}"


def selected_text_to_image_profile() -> ProviderProfile:
    """Compatibility helper for existing text-to-image callers."""
    profile = configured_profile(GenerationType.TEXT_TO_IMAGE)
    require_profile_capability(profile, GenerationType.TEXT_TO_IMAGE, "1280*1280")
    require_profile_available(profile)
    return profile
