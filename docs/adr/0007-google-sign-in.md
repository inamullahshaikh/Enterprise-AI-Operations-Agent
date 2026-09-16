# ADR-0007: Google Sign-In as a supported login method

## Status

Accepted

## Context

docs/system-design.md section 18.2 specifies email/password authentication only (Argon2
password hashing, JWT access tokens, rotated opaque refresh tokens). Google OAuth
(`GOOGLE_OAUTH_CLIENT_ID`/`SECRET`) was already present in the design, but scoped
narrowly to the Gmail/Calendar *connector* (a workspace admin authorizing Relay to read/
draft/send on a specific Google account, with tokens stored per `connector_installation`
in `connector_credentials`). The project wants users to also be able to sign in to Relay
itself with their Google account, which is a distinct concern from a connector installation.

## Decision

Add "Sign in with Google" as an additional login method alongside email/password —
not a replacement. It reuses the *same* Google OAuth client (`GOOGLE_OAUTH_CLIENT_ID`/
`GOOGLE_OAUTH_CLIENT_SECRET`) as the Gmail/Calendar connector, but the two flows request
different scopes and store their results in different places:

| | Sign-in flow | Connector install flow |
|---|---|---|
| Scopes requested | `openid email profile` only | Gmail/Calendar API scopes |
| Triggered by | Login page | Admin installing the Gmail/Calendar connector in a workspace |
| Result stored in | `users` (identity only — see below) | `connector_credentials` (per installation, encrypted) |
| Grants tool access? | No | Yes |

This keeps least-privilege intact (section 18.4's principle): logging in with Google never
by itself grants Relay any Gmail/Calendar access — that still requires the separate,
explicit, workspace-scoped connector installation and consent screen.

Users can still register and log in with email/password as before; a user's `users` row
gains a nullable `google_sub` (the stable Google account identifier) and
`auth_provider`/`google_email_verified`-style bookkeeping so an account can be linked to
either or both login methods. Password hash stays nullable for Google-only accounts, as
it already is for "SSO-only" users per the original `users.password_hash` column comment
in section 14.3.

## Consequences

- One more login code path (`/auth/google/start`, `/auth/google/callback` or equivalent)
  alongside `/auth/register` and `/auth/login`, implemented in Phase 1 alongside the rest
  of auth.
- Account linking rules need to be decided in Phase 1: what happens when a Google sign-in's
  email matches an existing email/password account (recommend: link automatically only if
  `email_verified` is true on both sides; otherwise require explicit confirmation).
- No change to the JWT/refresh-token session model in section 18.2 — Google Sign-In only
  changes how the *first* login step establishes identity; Relay still issues its own
  access/refresh tokens afterward.
- `docs/system-design.md` section 14.3's `users` table DDL and section 15.1's auth routes
  are left as originally written; this ADR records the addition rather than mass-editing
  the spec, consistent with ADR-0005 and ADR-0006.

## Alternatives considered

- Separate Google OAuth client for login vs. connector: rejected as unnecessary complexity
  — one client requesting different scopes per flow is the standard pattern and keeps
  Cloudflare/Google console setup to a single app.
- Google-only login (drop email/password): rejected — not requested, and email/password
  remains useful for API-key-issuing admin accounts and for users without a Google account.
