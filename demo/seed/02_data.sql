-- Northstar Analytics demo data (docs/system-design.md section 27.1).
--
-- Pure SQL (generate_series + setseed), not a Faker-based generator as the design doc
-- suggests: this runs once, inside docker-entrypoint-initdb.d, on a bare postgres:16 image
-- with no Python/Faker available — a separate generator script would need a manual run-and-
-- commit-the-output step for no real benefit over `generate_series` doing the same job in SQL
-- (see demo/README.md and docs/adr/0009-phase3-connector-metadata-in-code.md).
--
-- Every date is relative to CURRENT_DATE, not a literal, so "renewing this month" and "usage
-- dropped this month" are still true no matter when this script actually runs (i.e. whenever
-- the demo-db volume is first created) — a hardcoded date would go stale within weeks.
SELECT setseed(0.42);

-- ---------------------------------------------------------------------------------------
-- Four marquee accounts (ids 1-4), matching the worked example in docs/system-design.md
-- section 21.3: three renewing this month with MRR > $500 and a usage drop > 30%, one
-- (Initech) renewing this month too but with MRR too low to qualify.
-- ---------------------------------------------------------------------------------------
INSERT INTO accounts (id, name, industry, country, owner_email, created_at) VALUES
    (1, 'Acme Robotics', 'Manufacturing', 'US', 'jordan@acmerobotics.example', CURRENT_DATE - 640),
    (2, 'Northwind Labs', 'Biotech', 'US', 'sam@northwindlabs.example', CURRENT_DATE - 520),
    (3, 'Globex', 'Logistics', 'DE', 'priya@globex.example', CURRENT_DATE - 900),
    (4, 'Initech', 'Software', 'US', 'morgan@initech.example', CURRENT_DATE - 300);

INSERT INTO subscriptions (account_id, plan, mrr_usd, seats, start_date, end_date, auto_renew, status) VALUES
    (1, 'enterprise', 4200.00, 80, CURRENT_DATE - 640, date_trunc('month', CURRENT_DATE)::date + 9, true, 'active'),
    (2, 'growth',     1800.00, 35, CURRENT_DATE - 520, date_trunc('month', CURRENT_DATE)::date + 14, true, 'active'),
    (3, 'enterprise', 6100.00, 120, CURRENT_DATE - 900, date_trunc('month', CURRENT_DATE)::date + 19, true, 'active'),
    (4, 'starter',    240.00, 5,  CURRENT_DATE - 300, date_trunc('month', CURRENT_DATE)::date + 24, true, 'active');

-- 180 days of usage per marquee account, with active_users down ~40% in the trailing 30 days
-- for the three "at risk" accounts (Acme, Northwind, Globex) — Initech holds steady.
INSERT INTO usage_daily (account_id, day, active_users, api_calls, reports_generated)
SELECT
    a.id,
    day::date,
    GREATEST(
        1,
        round(
            a.base_users
            * CASE WHEN a.has_drop AND day >= CURRENT_DATE - 29 THEN 0.6 ELSE 1.0 END
            * (0.9 + random() * 0.2)
        )
    )::int AS active_users,
    GREATEST(
        1,
        round(
            a.base_users * 18
            * CASE WHEN a.has_drop AND day >= CURRENT_DATE - 29 THEN 0.55 ELSE 1.0 END
            * (0.85 + random() * 0.3)
        )
    )::int AS api_calls,
    round(a.base_users * 0.4 * (0.7 + random() * 0.6))::int AS reports_generated
FROM generate_series(CURRENT_DATE - 179, CURRENT_DATE, interval '1 day') AS day
CROSS JOIN (VALUES (1, 60, true), (2, 30, true), (3, 90, true), (4, 8, false)) AS a(id, base_users, has_drop);

INSERT INTO support_tickets (account_id, opened_at, priority, status, subject) VALUES
    (3, now() - interval '4 days', 'P1', 'open', 'API latency spike on bulk export'),
    (1, now() - interval '20 days', 'P2', 'resolved', 'Seat count reconciliation'),
    (2, now() - interval '55 days', 'P3', 'resolved', 'Question about SSO setup');

-- ---------------------------------------------------------------------------------------
-- Bulk accounts (ids 100+) for volume — a mix of plans, renewal dates spread across the
-- next two quarters (so "this month" is a meaningful filter, not everything), and 180 days
-- of unremarkable usage each.
-- ---------------------------------------------------------------------------------------
INSERT INTO accounts (id, name, industry, country, owner_email, created_at)
SELECT
    99 + gs,
    (ARRAY['Sterling', 'Vertex', 'Bluepeak', 'Ironwood', 'Coral', 'Summit', 'Lakeside', 'Redshift',
           'Harbor', 'Meridian', 'Cobalt', 'Fenwick', 'Alderly', 'Brightside', 'Quarry'])[1 + floor(random() * 15)]
        || ' ' || (ARRAY['Systems', 'Group', 'Labs', 'Partners', 'Holdings', 'Works', 'Analytics', 'Networks'])[1 + floor(random() * 8)],
    (ARRAY['Manufacturing', 'Biotech', 'Logistics', 'Software', 'Retail', 'Finance', 'Healthcare', 'Media'])[1 + floor(random() * 8)],
    (ARRAY['US', 'US', 'US', 'DE', 'GB', 'CA', 'AU', 'FR'])[1 + floor(random() * 8)],
    'owner' || gs || '@example.com',
    CURRENT_DATE - (30 + floor(random() * 1000))::int
FROM generate_series(1, 90) AS gs;

-- `accounts.id` got explicit values above (both the marquee 1-4 and bulk 100-189), which
-- doesn't advance `accounts_id_seq` — fix it up so any future plain INSERT relying on the
-- default nextval() doesn't collide with an existing row.
SELECT setval('accounts_id_seq', (SELECT max(id) FROM accounts));

INSERT INTO subscriptions (account_id, plan, mrr_usd, seats, start_date, end_date, auto_renew, status)
SELECT
    a.id,
    (ARRAY['starter', 'growth', 'enterprise'])[1 + floor(random() * 3)],
    round((100 + random() * 5000)::numeric, 2),
    5 + floor(random() * 150)::int,
    a.created_at,
    -- ~12% of contracts renew this month; the rest are spread across the surrounding year.
    CASE
        WHEN random() < 0.12
            THEN date_trunc('month', CURRENT_DATE)::date + floor(random() * 27)::int
        ELSE CURRENT_DATE + (floor(random() * 300) - 60)::int
    END,
    random() < 0.85,
    'active'
FROM accounts a
WHERE a.id >= 100;

INSERT INTO usage_daily (account_id, day, active_users, api_calls, reports_generated)
SELECT
    a.id,
    day::date,
    GREATEST(1, round(a.base_users * (0.85 + random() * 0.3)))::int,
    GREATEST(1, round(a.base_users * 15 * (0.8 + random() * 0.4)))::int,
    round(a.base_users * 0.3 * random())::int
FROM generate_series(CURRENT_DATE - 179, CURRENT_DATE, interval '1 day') AS day
CROSS JOIN (SELECT id, 5 + floor(random() * 200)::int AS base_users FROM accounts WHERE id >= 100) AS a;

INSERT INTO support_tickets (account_id, opened_at, priority, status, subject)
SELECT
    a.id,
    now() - (random() * 180 || ' days')::interval,
    (ARRAY['P1', 'P2', 'P3'])[1 + floor(random() * 3)],
    (ARRAY['open', 'resolved', 'resolved', 'resolved'])[1 + floor(random() * 4)],
    (ARRAY['Login issue', 'Billing question', 'Feature request', 'Data export error',
           'Onboarding help', 'Performance complaint'])[1 + floor(random() * 6)]
FROM accounts a
CROSS JOIN generate_series(1, 3 + floor(random() * 4)::int) AS ticket_no
WHERE a.id >= 100;
