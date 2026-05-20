# Logistics-Prod — HTTPS + CloudFront + Cognito + WAF

> **Architecture diagram:** Export `architecture.drawio` to `architecture.png` using [draw.io](https://app.diagrams.net/) before deploying, then the diagram will render here.

> Harden the Logistics app for production: add HTTPS everywhere, a CDN, user authentication, and a managed firewall — without changing a line of application code.

## What you'll deploy

Two CloudFormation stacks (regional split required for CloudFront):

**Stack 1 — `cfn/01-backend.yaml`** (any region, e.g. `us-east-1`):
- **Cognito User Pool** — Hosted UI with `Admins` and `Drivers` groups; Cognito sends temporary passwords on first login
- **Secrets Manager** — RDS credentials + `flask_secret_key`; auto-rotation every 30 days
- **RDS PostgreSQL Multi-AZ** — shipment records; 7-day automated backups
- **S3 media bucket** — private; accessible only via CloudFront OAC
- **ALB** — HTTPS:443 listener (ACM cert) + HTTP:80 forward for CloudFront health checks
- **EC2 Auto Scaling Group** — Flask app in private subnets; validates Cognito JWTs

**Stack 2 — `cfn/02-edge.yaml`** (must be `us-east-1`):
- **ACM certificate** — for CloudFront (must be in us-east-1)
- **WAF Web ACL** — AWS Managed Rules (Common Rule Set + Known Bad Inputs)
- **CloudFront distribution** — path-based caching: `/track/*` at 60s TTL, `/static/*` and `/media/*` long TTL, `/admin/*` and `/driver/*` no cache
- **Route 53 A-alias** (optional) — custom domain

## What you'll learn

- Why CloudFront + WAF resources must live in `us-east-1` and how to split a stack across regions
- CloudFront Origin Access Control (OAC) for private S3 buckets
- Cognito Hosted UI integration with a Flask backend (JWT validation)
- ALB + CloudFront together: CloudFront as the public HTTPS entry point, ALB as the internal origin
- RDS Secrets Manager rotation with `SecretTargetAttachment`
- Flask `SECRET_KEY` in Secrets Manager: why all ASG instances must share the same key

## Quick start

1. **Prerequisites:** AWS account, VPC from [aws-cfn-snippets](https://github.com/BoldFellow/aws-cfn-snippets) (`vpc-cidr-getaz-outputs-db-subnets.yaml`), optionally a custom domain in Route 53
2. **Deploy the backend stack first:**
   ```bash
   aws cloudformation deploy \
     --template-file cfn/01-backend.yaml \
     --stack-name logistics-prod-backend \
     --capabilities CAPABILITY_IAM \
     --parameter-overrides VpcStackName=VPCs
   ```
3. **Bootstrap the schema** — run `scripts/ssm-run-schema-from-artifact.sh` once instances are healthy
4. **Deploy the edge stack** (must be `us-east-1`):
   ```bash
   aws cloudformation deploy \
     --template-file cfn/02-edge.yaml \
     --stack-name logistics-prod-edge \
     --region us-east-1 \
     --capabilities CAPABILITY_IAM \
     --parameter-overrides BackendStack=logistics-prod-backend
   ```
5. **Post-deploy wiring** — update the Cognito callback URL and S3 bucket policy with the CloudFront domain (see guide.md §10)

See [guide.md](guide.md) for the full console walkthrough of all 14 sections.

## What you'll destroy at cleanup

```bash
aws cloudformation delete-stack --stack-name logistics-prod-edge --region us-east-1
# Wait for edge stack to complete, then:
aws cloudformation delete-stack --stack-name logistics-prod-backend
```

**Manual cleanup required:**
- Empty the S3 media bucket before deleting the backend stack
- Delete the NAT Gateway and release its Elastic IP

**Estimated cost while running:** ~$5–$7/day (RDS Multi-AZ `db.t3.micro` ~$2.80/day + NAT Gateway ~$1.10/day + CloudFront ~$0.01/day + WAF ~$1.00/month)

**After cleanup:** zero ongoing cost (disable CloudFront before deleting to avoid transfer charges)

## Files

| File | Purpose |
|---|---|
| `architecture.drawio` | Architecture source (export to `architecture.png` with draw.io) |
| `cfn/01-backend.yaml` | CloudFormation — backend stack (Cognito, RDS, ALB, ASG, Secrets Manager) |
| `cfn/02-edge.yaml` | CloudFormation — edge stack (ACM, WAF, CloudFront, Route 53) |
| `guide.md` | Full console walkthrough — 14 sections |
| `app/app.py` | Flask application with Cognito JWT validation |
| `app/schema.sql` | PostgreSQL schema — shipments + drivers |
| `app/requirements.txt` | Python dependencies |
| `app/app.zip` | Application deployment package |
| `scripts/ssm-run-schema-from-artifact.sh` | SSM Run Command schema bootstrap script |
| `templates/driver_upload.html` | Driver shipment upload form |
| `templates/track.html` | Public shipment tracking page |

## License

MIT — see [LICENSE](LICENSE).
