"""Grundschema: Projektbaum, Aufgaben, Läufe, Erschließung, Audit.

Revision ID: 0001
Revises:
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "knoten",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("parent_id", sa.String(32), sa.ForeignKey("knoten.id", ondelete="CASCADE"), index=True),
        sa.Column("typ", sa.String(10), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("offen", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "aufgabe",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("einstellungen", sa.JSON, nullable=False),
        sa.Column("quelle_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("quelle", sa.LargeBinary),
        sa.Column("bundle", sa.LargeBinary),
        sa.Column("geaendert", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "lauf",
        sa.Column("id", _id, primary_key=True, autoincrement=True),
        sa.Column("zeit", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("kanal", sa.String(20), nullable=False),
        sa.Column("aufgabe_id", sa.String(32), index=True),
        sa.Column("quelle", sa.String(120), nullable=False, server_default=""),
        sa.Column("art", sa.String(10), nullable=False, server_default=""),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("fehler", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pruefen", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dauer_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("codes", sa.JSON, nullable=False),
    )
    op.create_table(
        "erschliessung",
        sa.Column("db", sa.String(500), primary_key=True),
        sa.Column("entscheidungen", sa.JSON, nullable=False),
    )
    op.create_table(
        "audit",
        sa.Column("id", _id, primary_key=True, autoincrement=True),
        sa.Column("zeit", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("wer", sa.String(200), nullable=False),
        sa.Column("aktion", sa.String(60), nullable=False),
        sa.Column("ziel", sa.String(200), nullable=False, server_default=""),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    for t in ("audit", "erschliessung", "lauf", "aufgabe", "knoten"):
        op.drop_table(t)
