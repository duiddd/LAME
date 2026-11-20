"""A user's role: USER, or ADMIN for the operators who may see the system dashboard."""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'USER' CHECK (role IN ('USER', 'ADMIN'))")


def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN role")
