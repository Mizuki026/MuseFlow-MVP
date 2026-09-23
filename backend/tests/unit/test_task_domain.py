import pytest

from museflow.tasks.domain import CreateTaskRequest, normalize_create_request, retry_is_allowed


def test_normalize_create_request_preserves_prompt_and_applies_generation_defaults() -> None:
    request = normalize_create_request(CreateTaskRequest(prompt="  a lighthouse at dusk  "))

    assert request.prompt == "  a lighthouse at dusk  "
    assert request.size_preset == "1280*1280"
    assert request.image_count == 1


@pytest.mark.parametrize(
    ("error_code", "allowed"),
    [
        ("PROVIDER_NOT_CONFIGURED", True),
        ("PROVIDER_AUTHENTICATION", True),
        ("RESULT_STORAGE_ERROR", True),
        ("RESULT_INVALID", False),
    ],
)
def test_manual_retry_policy_matches_documented_error_contract(
    error_code: str, allowed: bool
) -> None:
    assert retry_is_allowed(error_code) is allowed
