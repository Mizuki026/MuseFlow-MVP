from __future__ import annotations

from museflow.scheduler import run_scheduler_iteration


def test_maintenance_dispatch_failure_happens_after_core_retries_and_lease_recovery() -> None:
    calls: list[str] = []

    class DueRetries:
        def run_once(self) -> int:
            calls.append("retries")
            return 0

    class ExpiredLeases:
        def run_once(self) -> int:
            calls.append("leases")
            return 0

    class CoreOutbox:
        def dispatch_once(self) -> int:
            calls.append("core_outbox")
            return 0

    class MaintenanceSchedule:
        def run_once(self) -> bool:
            calls.append("maintenance_schedule")
            return True

    class MaintenanceOutbox:
        def dispatch_once(self) -> int:
            calls.append("maintenance_outbox")
            raise RuntimeError("maintenance broker is unavailable")

    run_scheduler_iteration(
        DueRetries(),  # type: ignore[arg-type]
        ExpiredLeases(),  # type: ignore[arg-type]
        CoreOutbox(),  # type: ignore[arg-type]
        MaintenanceSchedule(),  # type: ignore[arg-type]
        MaintenanceOutbox(),  # type: ignore[arg-type]
    )

    assert calls == [
        "retries",
        "leases",
        "core_outbox",
        "maintenance_schedule",
        "maintenance_outbox",
    ]
