# Infrastructure (Terraform)

- `modules/` — shared modules: vpc, ecs, rds, redis, s3, kms, alb, sandbox.
- `envs/portfolio/` — one EC2 instance running Docker Compose + a small RDS/Postgres, for a
  low-cost live demo.
- `envs/reference/` — full production-like layout (VPC, ECS Fargate, isolated sandbox host,
  RDS, ElastiCache, ALB, CloudWatch alarms), applied once for screenshots/video and torn down.

See docs/system-design.md section 23 for the architecture and the two-tier deployment rationale.

Implemented in Phase 9.
