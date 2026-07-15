CREATE TABLE orders (
    id bigint PRIMARY KEY,
    customer_id bigint NOT NULL,
    status text NOT NULL,
    total numeric(12, 2) NOT NULL DEFAULT 0
);
