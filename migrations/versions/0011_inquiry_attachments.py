"""Store immutable attachment references on inquiry and reply snapshots."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_inquiry_attachments"
down_revision = "0010_qualification_screening"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    attachment_type = postgresql.JSONB(astext_type=sa.Text())
    op.add_column(
        "inquiry_cases",
        sa.Column(
            "attachments",
            attachment_type,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "inquiry_reply_revisions",
        sa.Column(
            "attachments",
            attachment_type,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("inquiry_reply_revisions", "attachments", schema=SCHEMA)
    op.drop_column("inquiry_cases", "attachments", schema=SCHEMA)
