from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def select_mock_provider_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests offline unless a test explicitly selects a simulated provider."""
    monkeypatch.setenv("MUSEFLOW_PROVIDER", "mock")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_HOST", raising=False)
