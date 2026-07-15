--liquibase formatted sql
--property name=schema value=public

--changeset ewen:1
CREATE TABLE ${schema}.orders (
    id bigint PRIMARY KEY,
    customer_id bigint NOT NULL,
    status text NOT NULL
);
--rollback DROP TABLE ${schema}.orders;

--changeset ewen:2
--comment deliberately risky: a plain CREATE INDEX blocks writes on PostgreSQL
CREATE INDEX idx_orders_status ON ${schema}.orders (status);
--rollback DROP INDEX ${schema}.idx_orders_status;
