from __future__ import annotations

from museflow.queue import create_celery_app


def test_generation_and_reference_maintenance_use_independent_celery_queues() -> None:
    celery_app = create_celery_app("redis://127.0.0.1:1/0")

    assert celery_app.conf.task_default_queue == "generation"
    assert {queue.name for queue in celery_app.conf.task_queues} == {
        "generation",
        "maintenance",
    }
    assert celery_app.conf.task_routes["museflow.execute_task"]["queue"] == "generation"
    assert (
        celery_app.conf.task_routes["museflow.reference_asset_maintenance"]["queue"]
        == "maintenance"
    )
