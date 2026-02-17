"""initial

Revision ID: 0001_initial
Revises:
Create Date: 2026-02-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # For this project we bootstrap with SQLAlchemy metadata create_all on startup.
    # Keep migration file as baseline and evolve with explicit revisions next.
    pass


def downgrade() -> None:
    pass
