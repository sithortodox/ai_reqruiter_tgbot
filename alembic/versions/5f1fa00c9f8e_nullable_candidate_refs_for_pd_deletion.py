"""Ссылки на кандидата допускают NULL: требуется для удаления ПД.

При удалении персональных данных кандидата сессии и резюме остаются
обезличенными (candidate_id = NULL), а не удаляются каскадом.

Revision ID: 5f1fa00c9f8e
Revises: 33f11a7944a5
Create Date: 2025-01-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "5f1fa00c9f8e"
down_revision = "33f11a7944a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table на SQLite пересобирает таблицу, на PostgreSQL
    # выполняет обычный ALTER TABLE
    with op.batch_alter_table("interview_sessions") as batch_op:
        batch_op.alter_column(
            "candidate_id",
            existing_type=sa.String(length=32),
            existing_nullable=False,
            nullable=True,
        )
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.alter_column(
            "candidate_id",
            existing_type=sa.String(length=32),
            existing_nullable=False,
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.alter_column(
            "candidate_id",
            existing_type=sa.String(length=32),
            existing_nullable=True,
            nullable=False,
        )
    with op.batch_alter_table("interview_sessions") as batch_op:
        batch_op.alter_column(
            "candidate_id",
            existing_type=sa.String(length=32),
            existing_nullable=True,
            nullable=False,
        )