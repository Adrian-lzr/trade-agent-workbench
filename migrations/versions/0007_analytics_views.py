"""Expose least-privilege reporting views for read-only analytics."""

from alembic import op
import sqlalchemy as sa

revision = "0007_analytics_views"
down_revision = "0006_quote_approvals_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE VIEW trade_agent.analytics_rfq_facts
            WITH (security_barrier = true) AS
            SELECT org_id, rfq_id, created_at::date AS event_date
            FROM trade_agent.rfqs
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE VIEW trade_agent.analytics_quote_facts
            WITH (security_barrier = true) AS
            SELECT org_id, quotation_id, revision_id, revision_no,
                   created_at::date AS event_date
            FROM trade_agent.quote_revisions
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE VIEW trade_agent.analytics_run_facts
            WITH (security_barrier = true) AS
            SELECT org_id, run_id, status, created_at::date AS event_date
            FROM trade_agent.runs
            """
        )
    )
    # Migrations may run under a managed database account without CREATEROLE.
    # In that case provisioning is intentionally left to the deployment script;
    # view creation still succeeds and never weakens table permissions.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                CREATE ROLE trade_agent_analytics NOLOGIN;
            EXCEPTION
                WHEN duplicate_object THEN NULL;
                WHEN insufficient_privilege THEN NULL;
            END $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trade_agent_analytics') THEN
                    EXECUTE 'GRANT USAGE ON SCHEMA trade_agent TO trade_agent_analytics';
                    EXECUTE 'GRANT SELECT ON trade_agent.analytics_rfq_facts, '
                            'trade_agent.analytics_quote_facts, '
                            'trade_agent.analytics_run_facts TO trade_agent_analytics';
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS trade_agent.analytics_run_facts"))
    op.execute(sa.text("DROP VIEW IF EXISTS trade_agent.analytics_quote_facts"))
    op.execute(sa.text("DROP VIEW IF EXISTS trade_agent.analytics_rfq_facts"))
