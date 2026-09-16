# Infrastructure (Terraform)

- `modules/` — shared modules: vpc, ecs, rds, redis, kms, alb, sandbox. Object storage is
  Cloudflare R2, not AWS S3 — provisioned via the `cloudflare` Terraform provider (or created
  once by hand in the dashboard) rather than an `s3` module; see
  `docs/adr/0005-cloudflare-r2-object-storage.md`.
- `envs/portfolio/` — one EC2 instance running Docker Compose + a small RDS/Postgres, for a
  low-cost live demo.
- `envs/reference/` — full production-like layout (VPC, ECS Fargate, isolated sandbox host,
  RDS, ElastiCache, ALB, CloudWatch alarms), applied once for screenshots/video and torn down.

See docs/system-design.md section 23 for the architecture and the two-tier deployment rationale.

Implemented in Phase 9.
