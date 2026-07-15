-- Repeatable migration: re-runs when this file changes. No rollback audit
-- (the rollback is the previous version of this file).
CREATE OR REPLACE VIEW order_totals AS
SELECT customer_id, sum(total) AS total
FROM orders
GROUP BY customer_id;
