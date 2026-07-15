-- Undo companion for V2: attached to V2's rollback audit, never analyzed as a
-- forward change.
DROP INDEX ${tenant_schema}.idx_orders_status;
