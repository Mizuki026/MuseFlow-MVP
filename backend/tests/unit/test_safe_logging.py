from __future__ import annotations

import hashlib
import logging


def test_task_event_logs_fixed_fields_and_only_remote_id_digest(caplog) -> None:
    from museflow.safe_logging import log_task_event

    remote_id = "remote-id-must-not-appear-in-logs"
    logger = logging.getLogger("museflow.safe_logging_test")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_task_event(
            logger,
            "provider_poll",
            task_id="task-123",
            attempt_id="attempt-456",
            generation_type="IMAGE_TO_IMAGE",
            provider_name="dashscope",
            provider_profile="dashscope-wan2.6-image-cn-beijing-edit",
            phase="PROVIDER_RUNNING",
            duration_ms=125.0,
            remote_request_id=remote_id,
            recovery=True,
        )

    record = caplog.records[-1]
    assert record.event_name == "provider_poll"
    assert record.task_id == "task-123"
    assert record.asset_id is None
    assert record.attempt_id == "attempt-456"
    assert record.generation_type == "IMAGE_TO_IMAGE"
    assert record.provider_name == "dashscope"
    assert record.provider_profile == "dashscope-wan2.6-image-cn-beijing-edit"
    assert record.phase == "PROVIDER_RUNNING"
    assert record.duration_ms == 125.0
    assert record.remote_request_id_digest == hashlib.sha256(remote_id.encode()).hexdigest()[:12]
    assert record.recovery is True
    assert record.maintenance_task is False
    assert remote_id not in caplog.text
    assert not hasattr(record, "prompt")
    assert not hasattr(record, "authorization")
    assert not hasattr(record, "signed_url")
