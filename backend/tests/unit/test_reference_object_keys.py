from __future__ import annotations

from uuid import UUID

import pytest

from museflow.reference_assets.object_keys import reference_object_key


def test_reference_object_key_uses_asset_id_digest_and_detected_content_type() -> None:
    asset_id = UUID("e46b3094-29d8-43fa-a799-0fb49efb6c0d")
    digest = "a" * 64

    assert (
        reference_object_key(asset_id, digest, "image/jpeg")
        == f"references/{asset_id}/{digest}.jpg"
    )


@pytest.mark.parametrize(
    ("digest", "content_type"),
    [("../" + "a" * 61, "image/png"), ("A" * 64, "image/png"), ("a" * 64, "image/gif")],
)
def test_reference_object_key_rejects_untrusted_digest_or_format(
    digest: str, content_type: str
) -> None:
    with pytest.raises(ValueError):
        reference_object_key(UUID(int=1), digest, content_type)
