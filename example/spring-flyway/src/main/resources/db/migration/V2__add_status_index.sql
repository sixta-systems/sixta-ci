-- Deliberately risky: a plain CREATE INDEX blocks writes on PostgreSQL.
-- SIXTA suggests CONCURRENTLY. The ${tenant_schema} placeholder is resolved
-- from application.properties before analysis, exactly as Flyway would.
CREATE INDEX idx_orders_status ON ${tenant_schema}.orders (status);
