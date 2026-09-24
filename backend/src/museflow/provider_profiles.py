from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType

from museflow.tasks.domain import GenerationType


class ProviderProfileUnavailableError(RuntimeError):
    """The requested task profile is not supported by this deployment."""


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    profile_id: str
    provider_name: str
    model_name: str
    capability_version: str
    generation_types: frozenset[GenerationType]


TEXT_TO_IMAGE = frozenset({GenerationType.TEXT_TO_IMAGE})
PROVIDER_PROFILES = MappingProxyType(
    {
        "mock-text-to-image-v1": ProviderProfile(
            profile_id="mock-text-to-image-v1",
            provider_name="mock",
            model_name="mock-deterministic-image",
            capability_version="text-to-image-v1",
            generation_types=TEXT_TO_IMAGE,
        ),
        "dashscope-wan2.6-t2i-cn-beijing-v1": ProviderProfile(
            profile_id="dashscope-wan2.6-t2i-cn-beijing-v1",
            provider_name="dashscope",
            model_name="wan2.6-t2i",
            capability_version="text-to-image-v1",
            generation_types=TEXT_TO_IMAGE,
        ),
    }
)
_PROFILE_BY_PROVIDER = MappingProxyType(
    {profile.provider_name: profile for profile in PROVIDER_PROFILES.values()}
)

LEGACY_PROVIDER_PROFILE = "legacy-unfrozen-v1"
LEGACY_PROVIDER_NAME = "legacy-unknown"
LEGACY_MODEL_NAME = "legacy-unknown"
LEGACY_CAPABILITY_VERSION = "legacy-unknown"


def profile_for_id(profile_id: str) -> ProviderProfile | None:
    return PROVIDER_PROFILES.get(profile_id)


def selected_text_to_image_profile() -> ProviderProfile:
    provider_name = os.environ.get("MUSEFLOW_PROVIDER", "mock").lower()
    profile = _PROFILE_BY_PROVIDER.get(provider_name)
    if profile is None or GenerationType.TEXT_TO_IMAGE not in profile.generation_types:
        raise ProviderProfileUnavailableError("configured provider profile is unavailable")
    if profile.provider_name == "dashscope":
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        api_host = os.environ.get("DASHSCOPE_API_HOST")
        if not api_key or not api_host or not api_host.strip():
            raise ProviderProfileUnavailableError("configured provider profile is unavailable")
    return profile
