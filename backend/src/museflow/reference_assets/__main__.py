from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from museflow.db.session import create_session_factory
from museflow.reference_assets.repository import (
    ReferenceAssetRecord,
    ReferenceAssetRepository,
)


def _database_factory() -> sessionmaker[Session]:
    database_url = os.environ.get(
        "MUSEFLOW_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/museflow",
    )
    return create_session_factory(database_url)


def _public_candidate(record: ReferenceAssetRecord, now: datetime) -> dict[str, Any]:
    return {
        "asset_id": str(record.id),
        "age_hours": round((now - record.created_at).total_seconds() / 3600, 2),
        "size_bytes": record.size_bytes,
        "content_type": record.content_type,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m museflow.reference_assets")
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="report old unreferenced READY assets")
    report.add_argument("--minimum-age-hours", type=int, default=24)
    report.add_argument("--limit", type=int, default=50)
    cleanup = commands.add_parser(
        "delete-unreferenced", help="dry-run by default; --execute schedules deletion"
    )
    cleanup.add_argument("--minimum-age-hours", type=int, default=24)
    cleanup.add_argument("--limit", type=int, default=50)
    cleanup.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.minimum_age_hours < 24:
        parser.error("minimum age must be at least 24 hours")
    if not 1 <= args.limit <= 100:
        parser.error("limit must be between 1 and 100")

    now = datetime.now(UTC)
    created_before = now - timedelta(hours=args.minimum_age_hours)
    repository = ReferenceAssetRepository()
    factory = _database_factory()
    if args.command == "report" or not args.execute:
        with factory() as session:
            candidate_count = repository.count_unreferenced_ready(
                session, created_before=created_before
            )
            candidates = repository.report_unreferenced_ready(
                session, created_before=created_before, limit=args.limit
            )
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "candidate_count": candidate_count,
                    "listed_count": len(candidates),
                    "minimum_age_hours": args.minimum_age_hours,
                    "candidates": [_public_candidate(candidate, now) for candidate in candidates],
                },
                ensure_ascii=False,
            )
        )
        return 0

    with factory.begin() as session:
        claimed = repository.claim_unreferenced_ready_for_deletion(
            session,
            now=now,
            created_before=created_before,
            limit=args.limit,
        )
    print(
        json.dumps(
            {
                "dry_run": False,
                "scheduled_count": len(claimed),
                "minimum_age_hours": args.minimum_age_hours,
                "assets": [_public_candidate(asset, now) for asset in claimed],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
