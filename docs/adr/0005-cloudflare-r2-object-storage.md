# ADR-0005: Cloudflare R2 for object storage (supersedes AWS S3 / local MinIO)

## Status

Accepted

## Context

docs/system-design.md section 5.3 and section 23 originally specified "S3 (or Cloudflare R2)"
for uploaded files, sandbox artifacts, and eval reports, with MinIO standing in for S3 in
local development (section 23.1) and real AWS S3 in the cloud tiers (section 23.2/23.3). The
project has since decided to use Cloudflare R2 as the object storage backend directly, in
every environment — local development included — rather than running a local MinIO container
and a separate AWS S3 bucket in the cloud.

## Decision

All blob storage (uploaded files, sandbox artifacts, eval reports, S3-keyed columns like
`documents.blob_key` and `tool_calls.output_blob_key`) is backed by a Cloudflare R2 bucket,
accessed through R2's S3-compatible API via the same `boto3`/`httpx` client code that would
otherwise target AWS S3. This applies uniformly to local dev, the portfolio deployment, and
the reference deployment — there is no MinIO container and no AWS S3 bucket anywhere in the
stack.

Configuration (see `.env.example` and `relay_core/config.py`): `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT_URL`
(`https://<account_id>.r2.cloudflarestorage.com`). Any `boto3` client constructed against this
endpoint must use `region_name="auto"` and `config=Config(s3={"addressing_style": "path"})` —
R2 does not support AWS's virtual-hosted-style addressing or region negotiation.

## Consequences

- Local development requires a Cloudflare account and an R2 API token before running `make up`
  (there is no offline/local storage fallback). Use a separate R2 bucket per developer or a
  shared dev bucket with a workspace-id-prefixed key layout to avoid collisions.
- No egress fees moving data out of R2 (a meaningful cost difference from S3 for a
  demo/portfolio deployment that may be read from outside AWS's network).
- The cloud architecture in section 23.2 no longer provisions an AWS S3 bucket; Terraform's
  `infra/modules` has no `s3` module, and R2 buckets are provisioned via the `cloudflare`
  Terraform provider or created once by hand (see `infra/README.md`).
- Every other part of the design that talks about "S3" (blob keys, presigned URLs, the
  ingestion pipeline, artifact storage) is unchanged in spirit — R2 is a drop-in, S3-API-
  compatible replacement — so `docs/system-design.md` itself is left as originally written
  rather than mass-edited; this ADR is the record of the concrete choice.
- Presigned URLs for user downloads/uploads work the same way against R2 as against S3;
  confirm expiry and signing behavior against the current R2 API docs before relying on
  edge cases (e.g., very long-lived signed URLs).

## Alternatives considered

- AWS S3 (as originally written): rejected — no longer the chosen provider; keeping S3 would
  mean running two different blob stores (MinIO locally, S3 in the cloud) with slightly
  different edge-case behavior.
- Local MinIO for dev + R2 in the cloud: rejected for this project — the team preferred a
  single real backend in every environment over an offline-friendly local stand-in, accepting
  the trade-off that local dev needs live Cloudflare credentials.
