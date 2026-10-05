-- Run as the database owner/superuser after applying Alembic migrations.
-- Replace both literals before use; do not commit production credentials.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trade_agent_analytics') THEN
        CREATE ROLE trade_agent_analytics LOGIN PASSWORD 'replace-me';
    ELSE
        ALTER ROLE trade_agent_analytics LOGIN PASSWORD 'replace-me';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA trade_agent TO trade_agent_analytics;
GRANT SELECT ON trade_agent.analytics_rfq_facts,
               trade_agent.analytics_quote_facts,
               trade_agent.analytics_run_facts
TO trade_agent_analytics;

-- Keep the reporting identity unable to read or mutate source tables directly.
REVOKE ALL ON trade_agent.rfqs,
             trade_agent.rfq_revisions,
             trade_agent.quote_revisions,
             trade_agent.runs
FROM trade_agent_analytics;
