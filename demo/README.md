# Demo data — "Northstar Analytics"

Fictional B2B SaaS company seeded into the `demo-db` service for local dev, demos, and evals.
See docs/system-design.md section 27 for the full schema, planted stories, and demo script.

- `seed/` — SQL schema + Faker-based generator (fixed seed) for accounts, subscriptions,
  usage, and support tickets; mounted into `demo-db`'s `/docker-entrypoint-initdb.d`.
- `documents/` — sample policy PDFs / playbooks for the knowledge base (RAG).
- `csv/` — usage exports for the `csv_only` connector profile.

Implemented starting Phase 3 (schema + seed) and Phase 4 (documents).
