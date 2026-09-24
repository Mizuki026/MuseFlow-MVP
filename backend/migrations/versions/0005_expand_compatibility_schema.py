"""expand schema for compatible generation and reference asset storage

Revision ID: 0005_expand_compatibility_schema
Revises: 0004_demo_execution_profiles
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_expand_compatibility_schema"
down_revision: str | Sequence[str] | None = "0004_demo_execution_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reference_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_pending_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('STAGING', 'READY', 'FAILED', 'DELETE_PENDING', 'DELETED')",
            name="ck_reference_asset_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_reference_assets_idempotency_key"),
        sa.UniqueConstraint("object_key", name="uq_reference_assets_object_key"),
    )
    op.create_index(
        "ix_reference_assets_status_created", "reference_assets", ["status", "created_at"]
    )
    op.create_index(
        "ix_reference_assets_status_delete_pending",
        "reference_assets",
        ["status", "delete_pending_at"],
    )

    op.add_column("result_assets", sa.Column("width", sa.Integer(), nullable=True))
    op.add_column("result_assets", sa.Column("height", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_result_asset_dimensions_pair",
        "result_assets",
        "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
    )

    op.add_column(
        "generation_tasks", sa.Column("generation_type", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "generation_tasks",
        sa.Column("reference_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "generation_tasks", sa.Column("reference_sha256", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "generation_tasks", sa.Column("provider_profile", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "generation_tasks", sa.Column("provider_name", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "generation_tasks", sa.Column("model_name", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "generation_tasks", sa.Column("capability_version", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "generation_tasks", sa.Column("policy_snapshot", postgresql.JSONB(), nullable=True)
    )
    op.create_foreign_key(
        "fk_generation_tasks_reference_asset",
        "generation_tasks",
        "reference_assets",
        ["reference_asset_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    raise NotImplementedError(
        "compatibility schema downgrade is unsupported because it can discard task snapshots "
        "and reference asset records"
    )
