"""Sitzungen für die Anmeldung über OpenID Connect.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_zeit = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "sitzung",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("art", sa.String(10), nullable=False),
        sa.Column("nutzer", sa.String(200), nullable=False, server_default=""),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("rollen", sa.JSON, nullable=False),
        sa.Column("daten", sa.JSON, nullable=False),
        sa.Column("erstellt", _zeit, nullable=False),
        sa.Column("zuletzt", _zeit, nullable=False),
        sa.Column("laeuft_ab", _zeit, nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("sitzung")
