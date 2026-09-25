from __future__ import annotations

import pytest

from museflow.provider_profiles import (
    PROVIDER_PROFILES,
    ProviderProfileUnavailableError,
    configured_profile,
    require_profile_available,
    require_profile_capability,
    selected_text_to_image_profile,
    validate_wan26_workspace_origin,
)
from museflow.providers import (
    GenerationRequest,
    MockProvider,
    ProviderError,
    create_provider_for_task,
)
from museflow.tasks.domain import DomainValidationError, GenerationType


def test_registry_freezes_capabilities_sizes_and_adapter_availability() -> None:
    assert set(PROVIDER_PROFILES) == {
        "mock-text-to-image-v1",
        "mock-image-generation-v2",
        "dashscope-wan2.6-t2i-cn-beijing-v1",
        "dashscope-wan2.6-image-cn-beijing-edit",
    }
    assert PROVIDER_PROFILES["mock-image-generation-v2"].generation_types == frozenset(
        GenerationType
    )
    assert PROVIDER_PROFILES["mock-image-generation-v2"].input_content_types == frozenset(
        {"image/png", "image/jpeg", "image/webp"}
    )
    assert PROVIDER_PROFILES["mock-image-generation-v2"].adapter_available is True
    assert PROVIDER_PROFILES["dashscope-wan2.6-t2i-cn-beijing-v1"].generation_types == frozenset(
        {GenerationType.TEXT_TO_IMAGE}
    )
    image_profile = PROVIDER_PROFILES["dashscope-wan2.6-image-cn-beijing-edit"]
    assert image_profile.model_name == "wan2.6-image"
    assert image_profile.capability_version == "official-doc-snapshot-2026-09-24"
    assert image_profile.generation_types == frozenset({GenerationType.IMAGE_TO_IMAGE})
    assert image_profile.input_content_types == frozenset({"image/png", "image/jpeg", "image/webp"})


def test_dashscope_profiles_are_default_when_provider_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MUSEFLOW_PROVIDER", raising=False)

    assert (
        configured_profile(GenerationType.TEXT_TO_IMAGE).profile_id
        == "dashscope-wan2.6-t2i-cn-beijing-v1"
    )
    assert (
        configured_profile(GenerationType.IMAGE_TO_IMAGE).profile_id
        == "dashscope-wan2.6-image-cn-beijing-edit"
    )


def test_mock_profile_supports_both_types_and_dashscope_rejects_i2i_before_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSEFLOW_PROVIDER", "mock")
    profile = configured_profile(GenerationType.IMAGE_TO_IMAGE)
    assert profile.profile_id == "mock-image-generation-v2"
    require_profile_capability(profile, GenerationType.IMAGE_TO_IMAGE, "1280*1280")

    monkeypatch.setenv("MUSEFLOW_PROVIDER", "dashscope")
    profile = configured_profile(GenerationType.IMAGE_TO_IMAGE)
    assert profile.profile_id == "dashscope-wan2.6-image-cn-beijing-edit"
    require_profile_capability(profile, GenerationType.IMAGE_TO_IMAGE, "1280*1280")
    with pytest.raises(DomainValidationError) as error:
        require_profile_capability(profile, GenerationType.TEXT_TO_IMAGE, "1280*1280")
    assert error.value.code.value == "PROVIDER_CAPABILITY_UNSUPPORTED"


@pytest.mark.parametrize(
    "endpoint",
    [
        "workspace.cn-beijing.maas.aliyuncs.com",
        "https://workspace.cn-beijing.maas.aliyuncs.com:443",
    ],
)
def test_wan26_endpoint_accepts_only_normalized_beijing_workspace_origins(endpoint: str) -> None:
    assert validate_wan26_workspace_origin(endpoint) == (
        "https://workspace.cn-beijing.maas.aliyuncs.com"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://workspace.cn-beijing.maas.aliyuncs.com",
        "https://workspace.cn-beijing.maas.aliyuncs.com.evil.test",
        "https://a.b.cn-beijing.maas.aliyuncs.com",
        "https://workspace_name.cn-beijing.maas.aliyuncs.com",
        "https://user@workspace.cn-beijing.maas.aliyuncs.com",
        "https://workspace.cn-beijing.maas.aliyuncs.com/path",
        "https://workspace.cn-beijing.maas.aliyuncs.com:444",
        "https://workspace.cn-beijing.maas.aliyuncs.com?query=1",
    ],
)
def test_wan26_endpoint_rejects_unsafe_or_nonfrozen_origins(endpoint: str) -> None:
    with pytest.raises(ValueError):
        validate_wan26_workspace_origin(endpoint)


def test_wan26_factory_requires_valid_explicit_workspace_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from museflow.dashscope_image_adapter import DashScopeWan26ImageAdapter

    monkeypatch.setenv("MUSEFLOW_PROVIDER", "dashscope")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_API_HOST", "workspace.cn-beijing.maas.aliyuncs.com")
    profile = configured_profile(GenerationType.IMAGE_TO_IMAGE)
    require_profile_available(profile)
    provider = create_provider_for_task(
        profile_id=profile.profile_id,
        provider_name=profile.provider_name,
        model_name=profile.model_name,
        capability_version=profile.capability_version,
        generation_type=GenerationType.IMAGE_TO_IMAGE,
    )
    assert isinstance(provider, DashScopeWan26ImageAdapter)
    provider.close()

    monkeypatch.setenv("DASHSCOPE_API_HOST", "workspace.cn-beijing.maas.aliyuncs.com.evil")
    with pytest.raises(ProviderProfileUnavailableError):
        require_profile_available(profile)

    monkeypatch.setenv("DASHSCOPE_API_HOST", "workspace.cn-beijing.maas.aliyuncs.com")
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    with pytest.raises(ProviderProfileUnavailableError):
        require_profile_available(profile)

    monkeypatch.setenv("DASHSCOPE_API_KEY", "   ")
    with pytest.raises(ProviderProfileUnavailableError):
        require_profile_available(profile)

    monkeypatch.setenv("DASHSCOPE_API_KEY", " key-with-surrounding-space ")
    with pytest.raises(ProviderProfileUnavailableError):
        require_profile_available(profile)


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

    image_profile = configured_profile(GenerationType.IMAGE_TO_IMAGE)
    image_provider = create_provider_for_task(
        profile_id=image_profile.profile_id,
        provider_name=image_profile.provider_name,
        model_name=image_profile.model_name,
        capability_version=image_profile.capability_version,
        generation_type=GenerationType.IMAGE_TO_IMAGE,
    )
    assert isinstance(image_provider, MockProvider)


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
