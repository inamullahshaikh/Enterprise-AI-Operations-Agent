# Demo data — "Northstar Analytics"

Fictional B2B SaaS company seeded into the `demo-db` service for local dev, demos, and evals.
See docs/system-design.md section 27 for the full schema, planted stories, and demo script.

- `seed/` — SQL schema + a pure-SQL generator (`setseed` + `generate_series`, not Faker — see
  docs/adr/0009-phase3-connector-metadata-in-code.md) for accounts, subscriptions, usage, and
  support tickets; mounted into `demo-db`'s `/docker-entrypoint-initdb.d`. Every date is
  relative to `CURRENT_DATE`, so "renewing this month" and "usage dropped this month" stay
  true no matter when the `demo-db` volume is first created.
- `documents/` — sample policy PDFs / playbooks for the knowledge base (RAG).
- `csv/` — usage exports for the `csv_only` connector profile.

Implemented: schema + seed (Phase 3). Documents land in Phase 4.
