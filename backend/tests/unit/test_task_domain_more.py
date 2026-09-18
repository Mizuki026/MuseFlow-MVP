import pytest

from museflow.tasks.domain import (
    CreateTaskRequest,
    DomainErrorCode,
    DomainValidationError,
    normalize_create_request,
    request_fingerprint,
)


@pytest.mark.parametrize("prompt", ["a", "x" * 2000])
def test_normalize_create_request_accepts_prompt_length_boundaries(prompt: str) -> None:
    assert normalize_create_request(CreateTaskRequest(prompt=prompt)).prompt == prompt


@pytest.mark.parametrize("prompt", ["", "   ", "\t\n"])
def test_normalize_create_request_rejects_empty_or_whitespace_prompt(prompt: str) -> None:
    with pytest.raises(DomainValidationError) as error:
        normalize_create_request(CreateTaskRequest(prompt=prompt))

    assert error.value.code == DomainErrorCode.INVALID_PROMPT


def test_normalize_create_request_rejects_prompt_longer_than_2000_characters() -> None:
    with pytest.raises(DomainValidationError) as error:
        normalize_create_request(CreateTaskRequest(prompt="x" * 2001))

    assert error.value.code == DomainErrorCode.INVALID_PROMPT


def test_request_fingerprint_uses_normalized_defaults_without_rewriting_prompt() -> None:
    with_defaults = normalize_create_request(CreateTaskRequest(prompt="  same text  "))
    explicit_defaults = normalize_create_request(
        CreateTaskRequest(prompt="  same text  ", size_preset="1280*1280", image_count=1)
    )
    different_prompt = normalize_create_request(CreateTaskRequest(prompt="same text"))
    demo_profile = normalize_create_request(
        CreateTaskRequest(prompt="  same text  ", execution_profile="permanent_failure")
    )

    assert request_fingerprint(with_defaults) == request_fingerprint(explicit_defaults)
    assert request_fingerprint(with_defaults) != request_fingerprint(different_prompt)
    assert request_fingerprint(with_defaults) != request_fingerprint(demo_profile)
