from __future__ import annotations

import pytest

from museflow.provider_profiles import (
    PROVIDER_PROFILES,
    ProviderProfileUnavailableError,
    selected_text_to_image_profile,
)
from museflow.providers import (
    GenerationRequest,
    MockProvider,
    ProviderError,
    create_provider_for_task,
)
from museflow.tasks.domain import GenerationType


def test_code_defined_profiles_only_advertise_the_supported_text_generation_type() -> None:
    assert set(PROVIDER_PROFILES) == {
        "mock-text-to-image-v1",
        "dashscope-wan2.6-t2i-cn-beijing-v1",
    }
    assert all(
        profile.generation_types == frozenset({GenerationType.TEXT_TO_IMAGE})
        for profile in PROVIDER_PROFILES.values()
    )


def test_new_mock_task_profile_is_selected_from_the_shared_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSEFLOW_PROVIDER", "mock")
    profile = selected_text_to_image_profile()

    assert profile.profile_id == "mock-text-to-image-v1"
    provider = create_provider_for_task(
        profile_id=profile.profile_id,
        provider_name=profile.provider_name,
        model_name=profile.model_name,
        capability_version=profile.capability_version,
    )
    assert isinstance(provider, MockProvider)


def test_unavailable_frozen_profile_fails_without_switching_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_HOST", raising=False)
    provider = create_provider_for_task(
        profile_id="dashscope-wan2.6-t2i-cn-beijing-v1",
        provider_name="dashscope",
        model_name="wan2.6-t2i",
        capability_version="text-to-image-v1",
    )

    with pytest.raises(ProviderError) as error:
        provider.generate(
            GenerationRequest(prompt="frozen", size_preset="1280*1280"),
            request_key="profile-test",
            remote_request_id=None,
        )
    assert error.value.code == "PROVIDER_PROFILE_UNAVAILABLE"


def test_unknown_profile_is_not_resolved_from_current_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSEFLOW_PROVIDER", "unsupported")
    with pytest.raises(ProviderProfileUnavailableError):
        selected_text_to_image_profile()

    provider = create_provider_for_task(
        profile_id="legacy-unfrozen-v1",
        provider_name="legacy-unknown",
        model_name="legacy-unknown",
        capability_version="legacy-unknown",
    )
    with pytest.raises(ProviderError) as error:
        provider.generate(
            GenerationRequest(prompt="legacy", size_preset="1280*1280"),
            request_key="legacy-profile-test",
            remote_request_id=None,
        )
    assert error.value.code == "PROVIDER_PROFILE_UNAVAILABLE"
