# ADR-0006: HubSpot connector dropped from v1 scope

## Status

Accepted

## Context

docs/system-design.md section 10.6 specified a first-party HubSpot CRM connector
(`search_companies`, `search_deals`, `create_note`, `create_task`) to demonstrate a
real third-party OAuth2 integration and to let the demo show the capability resolver
choosing between two competing providers of `customer.read` (HubSpot vs. Postgres).
Section 28's own "Minimum viable version" already treats HubSpot as optional: the
MVP cut lists only `file_upload + postgres + documents + gmail (mock) + MCP`.

## Decision

Drop the HubSpot connector from scope entirely for this project. `customer.read`,
`subscription.read`, and related capabilities are provided by the `postgres` connector
(and `file_upload` as the fallback) only; there is no second competing provider for
those capabilities in this build.

## Consequences

- Simpler scope: no HubSpot developer test account, no HubSpot OAuth app, no
  `crm.note.write` capability or its approval-gated write tools to build or evaluate.
- The "capability resolver picks between two healthy providers" story is no longer
  demonstrated with HubSpot vs. Postgres; if that story is still wanted later, it can
  be shown instead with two Postgres-like sources (e.g., the demo DB vs. an uploaded
  CSV) or a different second CRM/database connector.
- `connector_definitions`/manifests, the capability catalog (Appendix A), the `crm_only`
  eval profile (section 21.2), and any HubSpot-specific env vars are removed rather than
  built. `HUBSPOT_CLIENT_ID`/`HUBSPOT_CLIENT_SECRET` have been removed from
  `.env.example` and `relay_core/config.py`.
- `docs/system-design.md` itself is left as originally written (HubSpot still appears
  there as part of the v1.0 design); this ADR is the record of the concrete descope,
  consistent with how ADR-0005 handled the R2-vs-S3 change.

## Alternatives considered

- Keep HubSpot as a Phase 6+ "nice to have": rejected — not worth the OAuth app setup
  and mock-service work for a capability the MVP cut already treats as optional.
