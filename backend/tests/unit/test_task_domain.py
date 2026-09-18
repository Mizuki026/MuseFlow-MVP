from museflow.tasks.domain import CreateTaskRequest, normalize_create_request


def test_normalize_create_request_preserves_prompt_and_applies_generation_defaults() -> None:
    request = normalize_create_request(CreateTaskRequest(prompt="  a lighthouse at dusk  "))

    assert request.prompt == "  a lighthouse at dusk  "
    assert request.size_preset == "1280*1280"
    assert request.image_count == 1
