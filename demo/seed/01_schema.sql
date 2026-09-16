-- Northstar Analytics demo schema (docs/system-design.md section 27.1). Mounted into the
-- `demo-db` service's /docker-entrypoint-initdb.d, so this runs once, the first time that
-- container's volume is created.

CREATE TABLE accounts (
    id serial PRIMARY KEY,
    name text,
    industry text,
    country text,
    owner_email text,
    created_at date
);

CREATE TABLE subscriptions (
    id serial PRIMARY KEY,
    account_id int REFERENCES accounts(id),
    plan text CHECK (plan IN ('starter', 'growth', 'enterprise')),
    mrr_usd numeric(10, 2),
    seats int,
    start_date date,
    end_date date,
    auto_renew boolean,
    status text
);

CREATE TABLE usage_daily (
    account_id int REFERENCES accounts(id),
    day date,
    active_users int,
    api_calls int,
    reports_generated int,
    PRIMARY KEY (account_id, day)
);

CREATE TABLE support_tickets (
    id serial PRIMARY KEY,
    account_id int REFERENCES accounts(id),
    opened_at timestamptz,
    priority text,
    status text,
    subject text
);

COMMENT ON TABLE subscriptions IS 'One row per contract. end_date = renewal date.';
COMMENT ON COLUMN subscriptions.mrr_usd IS 'Monthly recurring revenue in USD';
