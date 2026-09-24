"""add maintenance leases for reference object operations

Revision ID: 0007_reference_operation_leases
Revises: 0006_compatibility_constraints
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_reference_operation_leases"
down_revision: str | Sequence[str] | None = "0006_compatibility_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reference_assets",
        sa.Column("operation_lease_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "reference_assets",
        sa.Column("operation_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_reference_asset_operation_lease_pair",
        "reference_assets",
        "(operation_lease_token IS NULL AND operation_lease_expires_at IS NULL) OR "
        "(operation_lease_token IS NOT NULL AND operation_lease_expires_at IS NOT NULL)",
    )
    op.create_index(
        "ix_reference_assets_operation_claim",
        "reference_assets",
        ["status", "operation_lease_expires_at", "created_at"],
    )


def downgrade() -> None:
    raise NotImplementedError(
        "reference asset maintenance leases are not safely reversible after workers start"
    )
