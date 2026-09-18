"""add retry lineage and durable result assets

Revision ID: 0003_retries_and_result_assets
Revises: 0002_generation_attempts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_retries_and_result_assets"
down_revision: str | Sequence[str] | None = "0002_generation_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        sa.Column("retried_from_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_generation_tasks_retried_from",
        "generation_tasks",
        "generation_tasks",
        ["retried_from_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_generation_tasks_retried_from", "generation_tasks", ["retried_from_task_id"]
    )
    op.drop_constraint("uq_outbox_aggregate_message", "outbox_messages", type_="unique")
    op.create_table(
        "result_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["generation_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["generation_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key", name="uq_result_assets_object_key"),
        sa.UniqueConstraint("task_id", "role", name="uq_result_assets_task_role"),
    )
    op.create_index("ix_result_assets_task", "result_assets", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_result_assets_task", table_name="result_assets")
    op.drop_table("result_assets")
    op.create_unique_constraint(
        "uq_outbox_aggregate_message", "outbox_messages", ["aggregate_id", "message_type"]
    )
    op.drop_constraint("uq_generation_tasks_retried_from", "generation_tasks", type_="unique")
    op.drop_constraint("fk_generation_tasks_retried_from", "generation_tasks", type_="foreignkey")
    op.drop_column("generation_tasks", "retried_from_task_id")
