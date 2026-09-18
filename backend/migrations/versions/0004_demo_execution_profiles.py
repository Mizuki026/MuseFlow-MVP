"""add internal demo execution profiles

Revision ID: 0004_demo_execution_profiles
Revises: 0003_retries_and_result_assets
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_demo_execution_profiles"
down_revision: str | None = "0003_retries_and_result_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        sa.Column("execution_profile", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generation_tasks", "execution_profile")
