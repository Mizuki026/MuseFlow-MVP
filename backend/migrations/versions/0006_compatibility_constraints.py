"""backfill and constrain compatible task snapshots

Revision ID: 0006_compatibility_constraints
Revises: 0005_expand_compatibility_schema
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_compatibility_constraints"
down_revision: str | Sequence[str] | None = "0005_expand_compatibility_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE generation_tasks SET generation_type = 'TEXT_TO_IMAGE' "
        "WHERE generation_type IS NULL"
    )
    op.execute(
        "UPDATE generation_tasks SET provider_profile = 'legacy-unfrozen-v1' "
        "WHERE provider_profile IS NULL"
    )
    op.execute(
        "UPDATE generation_tasks SET provider_name = 'legacy-unknown' "
        "WHERE provider_name IS NULL"
    )
    op.execute(
        "UPDATE generation_tasks SET model_name = 'legacy-unknown' "
        "WHERE model_name IS NULL"
    )
    op.execute(
        "UPDATE generation_tasks SET capability_version = 'legacy-unknown' "
        "WHERE capability_version IS NULL"
    )
    op.execute(
        "UPDATE generation_tasks SET policy_snapshot = "
        "'{\"version\": \"legacy-unfrozen-v1\", \"frozen\": false}'::jsonb "
        "WHERE policy_snapshot IS NULL"
    )

    op.alter_column(
        "generation_tasks", "generation_type", existing_type=sa.String(32), nullable=False
    )
    op.alter_column(
        "generation_tasks", "provider_profile", existing_type=sa.String(128), nullable=False
    )
    op.alter_column(
        "generation_tasks", "provider_name", existing_type=sa.String(64), nullable=False
    )
    op.alter_column("generation_tasks", "model_name", existing_type=sa.String(128), nullable=False)
    op.alter_column(
        "generation_tasks", "capability_version", existing_type=sa.String(64), nullable=False
    )
    op.alter_column("generation_tasks", "policy_snapshot", existing_type=sa.JSON(), nullable=False)

    op.create_check_constraint(
        "ck_generation_task_type",
        "generation_tasks",
        "generation_type IN ('TEXT_TO_IMAGE', 'IMAGE_TO_IMAGE')",
    )
    op.create_check_constraint(
        "ck_generation_task_reference_presence",
        "generation_tasks",
        "(generation_type = 'TEXT_TO_IMAGE' AND reference_asset_id IS NULL "
        "AND reference_sha256 IS NULL) OR "
        "(generation_type = 'IMAGE_TO_IMAGE' AND reference_asset_id IS NOT NULL "
        "AND reference_sha256 IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_task_policy_snapshot_object",
        "generation_tasks",
        "jsonb_typeof(policy_snapshot) = 'object'",
    )


def downgrade() -> None:
    raise NotImplementedError(
        "compatibility backfill downgrade is unsupported because it cannot restore the "
        "pre-migration task semantics"
    )
