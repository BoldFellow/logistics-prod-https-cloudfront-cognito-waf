# AWS Console Guide — Logistics-Prod (HTTPS + CloudFront + Cognito + WAF)

Step-by-step walkthrough for building the full production-ready stack through
the AWS Management Console.  Every section pairs **ClickOps** (console steps)
with the equivalent **CloudFormation** resource so you know exactly what the
template automates.

---

## What you will build

```
Visitors (worldwide)
    ↓  HTTPS
Route 53  (optional A-alias)
    ↓
CloudFront  (ACM cert in us-east-1 · WAF attached)
  ├── /track/*         60s TTL ──────────────→ ALB origin
  ├── /static/*  /media/*  long TTL  ─────────→ S3 origin (OAC)
  └── /admin/*  /driver/*  no cache  ─────────→ ALB origin
    ↓  HTTP (internal)
ALB (HTTPS:443 · HTTP:80 forward for CloudFront)
    ↓
ASG  (private subnets · Flask validates Cognito JWT)
    ↓
RDS Multi-AZ PostgreSQL   +   S3 media bucket (private, OAC-only)
    ↑
Secrets Manager (rotation 30 days)  +  Cognito User Pool (Hosted UI)
```

**This project is self-contained** — `cfn/template.yaml` creates its own VPC, subnets, IGW, route tables, and NAT Gateway. No existing VPC or cross-stack dependencies required.

**Optional:** A custom domain registered in Route 53 (or any registrar).
If you don't have one, the app works fine on the `*.cloudfront.net` default URL.

---

## Overview of what you will create

| # | Service | Resource | Notes |
|---|---|---|---|
| 0 | — | Prerequisites | Upload app.zip to artifact bucket |
| 1 | VPC | VPC + subnets + IGW + route tables + NAT | Self-contained network — 2 public, 2 app (private), 2 DB (isolated) |
| 2 | S3 | Media bucket (private) | No bucket policy yet — applied in §11 once CloudFront ARN is known |
| 3 | RDS | Multi-AZ PostgreSQL + DB subnet group | 7-day backups; master password typed manually |
| 4 | Secrets Manager | RDS secret + `flask_secret_key` + rotation | Full config in one shot — RDS exists so instance can be selected and rotation enabled |
| 5 | Cognito | User Pool + Hosted UI | Groups: Admins, Drivers — callback URL set in §11 after CloudFront domain is known |
| 6 | ACM | Cert in **us-east-1** | For CloudFront — requested early so DNS validation runs in parallel |
| 7 | ACM | Cert in **app region** | For ALB HTTPS:443 — skip if app region is us-east-1 |
| 8 | EC2 | ALB + target group + HTTP/HTTPS listeners | CloudFront origin — target group can be empty at creation |
| 9 | IAM | Instance role + instance profile | Needed by Launch Template in §13 |
| 10 | WAF | Web ACL (CLOUDFRONT scope, us-east-1) | Attached when CloudFront is created in §12 |
| 11 | CloudFront | Distribution + OAC + S3 bucket policy + Cognito callback | All final-URL wiring in one block |
| 12 | EC2 | Launch Template + ASG | userdata uses real `APP_URL` from §11 — no instance refresh needed |
| 13 | SSM | Schema bootstrap | Run schema.sql via Run Command once instances are healthy |
| 14 | Route 53 | A-alias record | Optional — custom domain only |
| 15 | — | End-to-end smoke test | Single pass through CloudFront |

---

---

> **CloudFormation shortcut:** If you want to deploy the full stack with a single CLI command instead of clicking through the console, see [Appendix C](#appendix-c--deploy-via-cloudformation-shortcut) at the end of this guide.

---

## 0. Prerequisites

This project creates its own VPC and all networking resources (§1). The only prerequisite before you start clicking is uploading the app bundle.

### 0a. Upload the app zip to S3

The zip must preserve directory structure (`templates/` and `static/` as subdirs).

```bash
cd "Logistics-Prod - HTTPS + CloudFront + Cognito + WAF"
zip -r app.zip app.py requirements.txt schema.sql templates/ static/
aws s3 cp app.zip s3://YOUR_ARTIFACT_BUCKET/logistics-prod/app.zip --region YOUR_REGION
```

> **Do not use `zip -j`** — it flattens all files into the root and the app will fail to find its templates.

---

## 1. VPC + Subnets + Internet Gateway + Route Tables

> **Console:** VPC → **Your VPCs** → **Create VPC**

**CloudFormation:** `Vpc`, `InternetGateway`, `VpcGatewayAttachment`, `PublicSubnetA/B`, `AppSubnetA/B`, `DbSubnetA/B`, `PublicRouteTable`, `PublicRoute`, `PrivateRouteTable`, subnet associations

This project creates a dedicated VPC with three tiers per Availability Zone — public, app (private), and DB (isolated).

### Step 1a — Create the VPC

> VPC → **Your VPCs** → **Create VPC**

1. Resources to create: **VPC only**
2. Name tag: `logistics-prod-vpc`
3. IPv4 CIDR block: `10.20.0.0/16`
4. Tenancy: **Default**
5. Click **Create VPC**
6. Select the new VPC → **Actions** → **Edit VPC settings** → enable both:
   - **DNS resolution** (EnableDnsSupport)
   - **DNS hostnames** (EnableDnsHostnames)

> Both DNS settings are required for VPC interface endpoints to resolve via private DNS.

### Step 1b — Create the Internet Gateway

> VPC → **Internet Gateways** → **Create internet gateway**

1. Name tag: `logistics-prod-igw`
2. Click **Create internet gateway**
3. Select the new IGW → **Actions** → **Attach to VPC** → select `logistics-prod-vpc`

### Step 1c — Create six subnets

> VPC → **Subnets** → **Create subnet**

Create all six subnets in `logistics-prod-vpc` (you can add all in one form using **Add new subnet**):

| Name | AZ | CIDR | Tier |
|---|---|---|---|
| `logistics-prod-public-a` | AZ 1 (e.g. `us-east-1a`) | `10.20.0.0/24` | Public — NAT GW, ALB |
| `logistics-prod-public-b` | AZ 2 (e.g. `us-east-1b`) | `10.20.1.0/24` | Public — ALB |
| `logistics-prod-app-a` | AZ 1 | `10.20.10.0/24` | App (private) — EC2 |
| `logistics-prod-app-b` | AZ 2 | `10.20.11.0/24` | App (private) — EC2 |
| `logistics-prod-db-a` | AZ 1 | `10.20.20.0/24` | DB (isolated) — RDS primary |
| `logistics-prod-db-b` | AZ 2 | `10.20.21.0/24` | DB (isolated) — RDS standby |

After creating: select each **public** subnet → **Actions** → **Edit subnet settings** → check **Enable auto-assign public IPv4 address**. (Not needed for app or DB subnets.)

### Step 1d — Public route table

> VPC → **Route Tables** → **Create route table**

1. Name: `logistics-prod-public-rt`, VPC: `logistics-prod-vpc` → **Create**
2. Select it → **Routes** tab → **Edit routes** → **Add route**:
   - Destination: `0.0.0.0/0`, Target: **Internet Gateway** → `logistics-prod-igw`
3. **Subnet associations** tab → **Edit subnet associations** → select both `logistics-prod-public-a` and `logistics-prod-public-b`

### Step 1e — Private route table

> VPC → **Route Tables** → **Create route table**

1. Name: `logistics-prod-private-rt`, VPC: `logistics-prod-vpc` → **Create**
2. **Subnet associations** tab → select both `logistics-prod-app-a` and `logistics-prod-app-b`

> DB subnets need no route table — they are fully isolated (local VPC routing only). The NAT Gateway default route is added to this table after you create the NAT Gateway in the next step.

### Step 1f — NAT Gateway

> VPC → **NAT Gateways** → **Create NAT gateway**

1. Name: `logistics-prod-nat`
2. Subnet: `logistics-prod-public-a`
3. Connectivity type: **Public**
4. Elastic IP: click **Allocate Elastic IP**
5. Click **Create NAT gateway** — wait ~1 minute for state **Available**

**Add the NAT route to the private route table:**
- Route Tables → select `logistics-prod-private-rt` → **Routes** → **Edit routes** → **Add route**:
  - Destination: `0.0.0.0/0`, Target: **NAT Gateway** → `logistics-prod-nat`

> **Why single-AZ NAT?** A production setup uses one NAT Gateway per AZ. For this teaching demo, a single NAT in AZ 1 is sufficient — AZ-b instances route outbound through AZ-a.

---

## 2. S3 Media Bucket

> **Console:** S3 → **Create bucket**

**CloudFormation:** `MediaBucket` (in cfn/template.yaml)

1. Bucket name: `logistics-prod-media-<YOUR_ACCOUNT_ID>` (must be globally unique)
2. Region: same as your app region (can be any region — CloudFront will serve from all edges)
3. **Block all public access: enabled** ← all four checkboxes
4. Versioning: disabled (optional for a teaching demo)
5. Encryption: SSE-S3 (AES-256)
6. Click **Create bucket**

> The bucket is empty now.  Drivers upload photos via the Flask app.
> The bucket policy (allowing CloudFront via OAC) is applied in §11 once the
> CloudFront distribution ARN is known.
> Static assets (Flask-Admin CSS/JS) can be uploaded to `s3://bucket/static/`.

### 2a — Optional: upload Flask-Admin static assets

Flask-Admin's bundled static files (Bootstrap 4 CSS/JS) are normally served
from CDN.  Vendoring them into S3 means CloudFront can cache them with a 1-year
TTL and no CDN dependency.

```bash
# Find where Flask-Admin is installed:
pip show flask-admin
# Copy static files to S3:
STATIC_PATH=$(pip show flask-admin | grep Location | awk '{print $2}')
aws s3 sync "$STATIC_PATH/flask_admin/static/" \
  "s3://logistics-prod-media-ACCOUNT_ID/static/flask-admin/" \
  --cache-control "max-age=31536000,public"
```

You also need to configure Flask-Admin to use the S3 URL for its static files.
For the teaching demo, the app serves Flask-Admin's static files directly from
the EC2 instance (via the default ALB behavior). The vendoring step is optional.

---

## 3. RDS PostgreSQL — Multi-AZ

> **Console:** RDS → **Create database**

**CloudFormation:** `DBSubnetGroup`, `DBInstance`

> **Before starting:** Generate a strong master password now — you will enter it
> into RDS in §3b and store the same value in Secrets Manager in §4.
> ```bash
> python3 -c "import secrets; print(secrets.token_hex(32))"
> ```
> Copy this value somewhere safe (e.g. a local scratch file). Do not lose it —
> you will type it twice.

### Step 3a — Create the DB subnet group

> RDS → **Subnet groups** → **Create DB subnet group**

1. Name: `logistics-prod-db-subnet-group`
2. VPC: select your VPC
3. Add subnets: select both **DB subnets** (one per AZ)
4. Click **Create**

### Step 3b — Create the RDS instance

> RDS → **Create database**

1. Engine: **PostgreSQL**
2. Template: **Production** (enables Multi-AZ by default)
3. DB instance identifier: `logistics-prod-pg`
4. Master username: `appadmin`
5. Master password: **type the strong password you generated above**
   > Using the same password here and in §4 (Secrets Manager) keeps a single secret
   > as the source of truth from day one — no post-creation sync required.
   >
   > **CLI note:** For the CLI path, use `--master-password <value>` with the password
   > you generated. In §4 you will store this same password in Secrets Manager
   > (`rds/logistics-prod`), so both the RDS instance and the secret share the same
   > credentials from the start.
6. DB instance class: `db.t4g.small` (**not** `db.t4g.micro` — micro does not support Multi-AZ)
7. Storage: **gp3**, 20 GiB, enable **storage encryption**
8. **Multi-AZ deployment: Create a standby instance** ← critical for this project
9. VPC: select your VPC
10. DB subnet group: `logistics-prod-db-subnet-group`
11. Public access: **No**
12. Security group: create new — `logistics-prod-rds-sg`
    - Inbound: port 5432 from the app SG (you'll add this later)
13. **Backup retention period: 7 days** (original Logistics used 1 day)
14. Click **Create database** — wait ~10 minutes for "Available"

> **Teaching point — Multi-AZ vs. the original Logistics:**
> The original Logistics project has a single RDS instance (`MultiAZ: false`).
> Here we add a standby replica in a second AZ.  If the primary fails, RDS
> automatically updates the DNS endpoint to point to the standby in ~15–60
> seconds.  The app connects to the endpoint DNS name (not the IP), so
> failover is transparent — the Flask app reconnects after a brief blip.

---

## 4. Secrets Manager — DB credentials with rotation

> **Console:** Secrets Manager → **Store a new secret**

**CloudFormation:** `DBSecret`, `DBSecretAttachment`, `DBSecretRotation`

RDS is now available, so the secret can be fully configured in one pass:
credentials stored, the RDS instance linked (so the secret type resolves
correctly), and rotation enabled — all without leaving this section.

### Step 4a — Store the secret

1. Secret type: **Credentials for Amazon RDS database**
2. Credentials:
   - Username: `appadmin`
   - Password: **enter the same strong password you used in §3b**
3. Select database: **`logistics-prod-pg`** ← select the instance you just created
4. Secret name: `rds/logistics-prod`
5. **Add an additional key:** `flask_secret_key` with a random 64-character hex value
   *(All ASG instances must share the same Flask session signing key — storing it here
   ensures every instance reads the same value from Secrets Manager at startup.
   If this field is missing, each instance uses a random per-instance key, breaking
   sessions across ALB instances.)*
   ```bash
   # Generate a value to paste into the secret:
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```
6. On the **Configure rotation** step of the wizard:
   - Automatic rotation: **Enable**
   - Rotation schedule: every **30 days**
   - Rotation function: **AWS Secrets Manager** → **Amazon RDS** → **PostgreSQL Single user**
   - Database: select **`logistics-prod-pg`**
7. Click **Store**

> A Lambda rotation function is created automatically.  Every 30 days it
> generates a new password, updates RDS, and updates the secret.  The next
> time an ASG instance starts, it fetches the new credentials from Secrets
> Manager.  Instances currently running use the cached credentials until
> they restart (or you trigger an ASG instance refresh).

### Step 4b — Note the Secret ARN

Copy the ARN — you'll need it for the IAM role in §9.

> Teaching note: Secrets Manager eliminates hardcoded passwords.
> The EC2 instance role grants `secretsmanager:GetSecretValue` on this one
> secret ARN. boto3 finds the role via the EC2 metadata service and fetches
> the credentials at app startup. No password is ever on disk.

---

## 5. Cognito User Pool + Hosted UI

> **Console:** Cognito → **User pools** → **Create user pool**

**CloudFormation:** `CognitoUserPool`, `CognitoUserPoolDomain`,
`CognitoUserPoolClient`, `CognitoAdminsGroup`, `CognitoDriversGroup`

### Step 5a — Create the user pool

1. **Sign-in experience** tab:
   - Sign-in options: check **Email**
   - Leave everything else at defaults
2. **Security requirements** tab:
   - Password policy: **Cognito defaults** (or keep Minimum 8, require uppercase/lowercase/numbers)
   - MFA: **No MFA** (add later in production)
3. **Sign-up experience** tab:
   - Self-registration: **enabled** (or disable and create users manually)
   - Required attributes: **email** already required
4. **Message delivery** tab: leave defaults (Cognito sends emails)
5. **Integrate your app** tab:
   - User pool name: `logistics-prod-users`
   - Hosted UI: **do not enable Hosted UI here** — skip the "Use the Cognito Hosted UI"
     toggle entirely. The OAuth callback URL can only be set once the CloudFront domain
     is known. You will enable Hosted UI and configure the callback in §11d.
   - App type: **Public client**
   - App client name: `logistics-prod-web-client`
   - Client secret: **Don't generate** (public client — no secret)
6. Click **Create user pool**

> **Why defer Hosted UI?** The Authorization Code Grant flow requires an exact-match
> callback URL registered in Cognito — `https://<CF_DOMAIN>/auth/callback`. That domain
> is only known after CloudFront is deployed in §11. Configuring it now would require a
> placeholder URL that silently mismatches and causes login loops. Instead, §11d sets up
> Hosted UI and OAuth immediately after the CloudFront domain is available.

**After the pool is created — set the Cognito domain:**

> Cognito → your user pool → **App integration** tab → **Domain** → **Create Cognito domain**

- Cognito domain prefix: `logistics-prod-auth` (globally unique)
  > The full domain becomes: `logistics-prod-auth-<AccountId>.auth.<Region>.amazoncognito.com`
- Click **Create Cognito domain**

Note the full domain — you'll paste it into the userdata in §12.

### Step 5b — Create user groups

> Select the user pool → **Groups** tab → **Create group**

Create two groups:

| Group name | Description |
|---|---|
| `Admins` | Full access to Flask-Admin GUI and all routes |
| `Drivers` | Access to `/driver/` photo upload routes only |

### Step 5c — Create test users

> **Groups** tab → **Users** tab → **Create user**

**Admin user:**
- Email: your email address
- Temporary password: set one (user changes it on first login)
- After creating: go to the user → **Add user to group** → `Admins`

**Driver user:**
- Email: another email (or alias like `driver+test@yourdomain.com`)
- After creating: add to group `Drivers`

---

## 6. ACM Certificate — us-east-1 (for CloudFront)

> **Console:** Switch region to **US East (N. Virginia)** → Certificate Manager → **Request a certificate**

**CloudFormation:** `CloudFrontCertificate` (in cfn/template.yaml, conditional)

> **This is the most important regional constraint in this project.**
> Request this certificate first so DNS validation runs in parallel while you
> complete §7–§10.

### Why must this certificate be in us-east-1?

CloudFront is a global service, but its control plane lives in `us-east-1`.
When you attach an ACM certificate to a CloudFront distribution, CloudFront
replicates the certificate's private key to every edge location worldwide.
This replication only works for certificates issued in `us-east-1`.

A certificate issued in `eu-west-1` (for example) is invisible to CloudFront
even if it covers the same domain.  CloudFront simply won't offer it in the
certificate dropdown.

> **Skip this section if you don't have a custom domain.**
> CloudFront will use its default `*.cloudfront.net` TLS certificate at no cost.

1. **While in us-east-1**, request a public certificate for `logistics.example.com`
2. Validation method: **DNS validation**
3. Click **Request**
4. Expand the certificate → click **Create records in Route 53**
   (or copy the CNAME and add it manually to your DNS provider)
5. DNS validation takes ~5 minutes. You don't need to wait — continue to §7.
   CloudFront will only need this cert to be **Issued** by the time you create
   the distribution in §11.
6. Copy the **Certificate ARN** for use in §11.

> **Exception — when app region IS us-east-1:** Both ALB and CloudFront are in
> us-east-1, so ACM will deduplicate the certificate request. You can use the same
> ARN for both the ALB listener in §8 and for CloudFront in §11 — no second cert
> needed. If you request a second cert for the same domain in the same region, ACM
> returns the existing one (not a duplicate).

---

## 7. ACM Certificate — App Region (for ALB)

> **Console:** Certificate Manager → **Request a certificate**

**CloudFormation:** `AlbCertificate` (conditional on `DomainName` parameter)

> **Skip this section if you don't have a custom domain.**
> The ALB works on HTTP:80. CloudFront enforces HTTPS for public users.
>
> **Skip this section if your app region is us-east-1** — use the cert ARN from §6
> for the ALB HTTPS listener. Both certificates cover the same domain, and ACM
> deduplicates them in the same region.

1. Certificate type: **Request a public certificate**
2. Fully qualified domain name: `logistics.example.com` (your domain)
3. Validation method: **DNS validation**
4. Click **Request**
5. Expand the certificate → click **Create records in Route 53**
   (or copy the CNAME — it is the same CNAME as §6, already added, so validation
   is instant)
6. Wait for status to change from **Pending validation** to **Issued** (~5 minutes)
7. Copy the **Certificate ARN** — you'll need it for the ALB HTTPS listener in §8

---

## 8. ALB with HTTPS Listener

> **Console:** EC2 → **Load Balancers** → **Create load balancer** → **Application Load Balancer**

**CloudFormation:** `ApplicationLoadBalancer`, `TargetGroup`, `HttpListener`, `HttpsListener`

### Step 8a — Create the ALB

1. Name: `logistics-prod-alb`
2. Scheme: **Internet-facing**
3. VPC: your VPC
4. Availability Zones: select both **public subnets**
5. Security groups: create new — `logistics-prod-alb-sg`
   - Inbound port 80 from `0.0.0.0/0` (CloudFront uses this)
   - Inbound port 443 from `0.0.0.0/0` (direct HTTPS access)

### Step 8b — Create the target group

1. Target type: **Instances**
2. Name: `logistics-prod-tg`
3. Protocol/Port: HTTP / 80
4. Health check path: `/health`
5. Interval: 15 seconds
6. Healthy threshold: 2, Unhealthy threshold: 3

> The target group is empty at this point — the ASG instances are created in §12,
> after CloudFront is set up. CloudFront accepts an ALB origin whose target group
> has zero healthy instances at creation time.

### Step 8c — Listeners

**HTTP:80 listener** (forward — CloudFront uses this):
- Protocol: HTTP, Port: 80
- Default action: **Forward** to `logistics-prod-tg`

> Teaching note: This is different from the original Logistics setup.
> In this project, HTTP:80 on the ALB **forwards** (does not redirect).
> Why? CloudFront connects to the ALB over HTTP:80.
> If port 80 were configured to redirect to HTTPS, CloudFront would receive
> a 301, pass it to the browser, the browser would make an HTTPS request to
> CloudFront (which is correct), CloudFront would then try HTTP:80 on the ALB
> again — and get another redirect.  This creates an infinite loop.
> The HTTP→HTTPS enforcement happens at the **CloudFront viewer protocol policy**
> (§11), not at the ALB.

**HTTPS:443 listener** (skip if you don't have an ACM cert from §7):
- Protocol: HTTPS, Port: 443
- Certificate: select the cert you created in §7 (app region cert; or §6 if app region is us-east-1)
- Security policy: **ELBSecurityPolicy-TLS13-1-2-2021-06**
- Default action: **Forward** to `logistics-prod-tg`

---

## 9. IAM Instance Role

> **Console:** IAM → **Roles** → **Create role**

**CloudFormation:** `AppInstanceRole`, `AppInstanceProfile` (in cfn/template.yaml)

1. Trusted entity: **EC2**
2. Name: `logistics-prod-app-role`
3. Attach managed policy: `AmazonSSMManagedInstanceCore`
4. Add inline policy: `logistics-prod-app-policy`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue","secretsmanager:DescribeSecret"],
      "Resource": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:rds/logistics-prod*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::ARTIFACT_BUCKET/logistics-prod/app.zip"
    },
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::logistics-prod-media-ACCOUNT/media/*"
    }
  ]
}
```

5. Create **Instance Profile** (same name as role — console does this automatically)

> The Secret ARN you noted in §4b goes into the `Resource` field above. Using the
> full ARN (rather than `*`) follows least-privilege: only this role can read this
> one secret.

---

## 10. WAF Web ACL

> **Console:** Switch to region **us-east-1** → WAF & Shield → **Create web ACL**

**CloudFormation:** `WafWebAcl` (in cfn/template.yaml, always in us-east-1)

> WAF Web ACLs with `Scope: CLOUDFRONT` must be in `us-east-1`.
> This is the same constraint as the CloudFront ACM certificate.

1. Resource type: **Amazon CloudFront distributions**
2. Name: `logistics-prod-waf`
3. Add managed rule groups:
   - **AWS managed rules** → **Core rule set** (blocks OWASP Top 10)
   - **AWS managed rules** → **Amazon IP reputation list**
4. Add your own rule:
   - Rule type: **Rate-based rule**
   - Name: `RateLimit`
   - Rate limit: **100** requests per **5 minutes**
   - IP address to use: **IP address in request** (source IP)
   - Action: **Block**
5. Default action: **Allow**
6. Click **Create web ACL** — do NOT associate with any resource yet (you'll
   attach it to CloudFront in §11)

---

## 11. CloudFront Distribution

> **Console:** CloudFront → **Create a CloudFront distribution**

**CloudFormation:** `CloudFrontDistribution`, `OriginAccessControl`,
`MediaBucketPolicy` (in cfn/template.yaml)

### Step 11a — Origin Access Control (for S3)

> CloudFront → **Origin access** → **Create control setting**

1. Name: `logistics-prod-oac`
2. Signing behavior: **Sign requests**
3. Origin type: **S3**
4. Click **Create**

### Step 11b — Create the distribution

**ALB Origin:**
1. Origin domain: `logistics-prod-alb-xxxxxxx.us-east-1.elb.amazonaws.com`
   (copy from EC2 → Load Balancers)
2. Protocol: **HTTP only** (CloudFront → ALB uses port 80)
3. Name: `alb-origin`
4. Connection attempts: 3, timeout: 10s

**S3 Origin (add second origin):**
1. Origin domain: `logistics-prod-media-ACCOUNT.s3.REGION.amazonaws.com`
2. Origin access: **Origin access control settings** → select `logistics-prod-oac`
3. Name: `s3-origin`

**Default cache behavior (/*→ ALB):**
- Cache policy: **CachingDisabled**
- Origin request policy: **AllViewer**
- Viewer protocol policy: **Redirect HTTP to HTTPS**
- Allowed methods: GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE

**Cache behaviors (add in this order):**

| Path pattern | Origin | Cache policy | Origin request policy | Allowed methods |
|---|---|---|---|---|
| `/admin/*` | alb-origin | CachingDisabled | AllViewer | All |
| `/driver/*` | alb-origin | CachingDisabled | AllViewer | All |
| `/track/*` | alb-origin | Custom 60s TTL† | AllViewerExceptHostHeader | GET, HEAD |
| `/static/*` | s3-origin | CachingOptimized | CORS-S3Origin | GET, HEAD |
| `/media/*` | s3-origin | CachingOptimized | CORS-S3Origin | GET, HEAD |

† For `/track/*`: create a custom cache policy with DefaultTTL=60, MinTTL=0,
MaxTTL=300, no cookies, no headers in the cache key.

**Settings:**
- Price class: **Use only North America and Europe** (demo — cheaper)
- WAF: select `logistics-prod-waf` (created in §10)
- Custom domain (if you have one): add `logistics.example.com`
- Custom certificate (if domain set): select the us-east-1 ACM cert from §6
- Default root object: leave blank (Flask handles `/`)
- IPv6: enabled

Click **Create distribution** — takes **10–15 minutes** to deploy globally.

**Note the CloudFront domain** (e.g. `xxxx.cloudfront.net`) from the distribution
detail page — you'll use it in §11c, §11d, and §12.

### Step 11c — Add S3 bucket policy for OAC

After the distribution is created, the CloudFront console shows a banner:
"You must update the S3 bucket policy to allow CloudFront to access it."
Click **Copy policy** → then go to:

> S3 → `logistics-prod-media-ACCOUNT` → **Permissions** → **Bucket policy**

Paste the copied policy.  It looks like:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowCloudFrontServicePrincipal",
      "Effect": "Allow",
      "Principal": {"Service": "cloudfront.amazonaws.com"},
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::logistics-prod-media-ACCOUNT/*",
      "Condition": {
        "StringEquals": {
          "aws:SourceArn": "arn:aws:cloudfront::ACCOUNT:distribution/DIST_ID"
        }
      }
    }
  ]
}
```

> **Teaching point — OAC vs. making the bucket public:**
> The `aws:SourceArn` condition ensures that ONLY this specific CloudFront
> distribution can read the bucket.  Even someone who knows the S3 URL cannot
> access the photos directly — they get a 403 (verified in smoke test §15).

### Step 11d — Enable Hosted UI and set Cognito callback URLs

> **CloudFormation note:** If you deployed via `cfn/template.yaml`, the callback and logout URLs are wired automatically at deploy time — `CognitoUserPoolClient` references `CloudFrontDistribution.DomainName` directly. You can skip this step for CFN deployments and verify with `aws cognito-idp describe-user-pool-client ...` that the URLs already contain your CloudFront domain.

Now that you have the CloudFront domain (e.g. `xxxx.cloudfront.net`):

> Cognito → your user pool → **App integration** tab →
> **App clients and analytics** → **`logistics-prod-web-client`** → **Edit**

1. Enable **Hosted UI** (toggle or checkbox — label varies by console version)
2. Allowed callback URLs: `https://xxxx.cloudfront.net/auth/callback`
3. Allowed sign-out URLs: `https://xxxx.cloudfront.net/`
   (Add both with and without trailing slash to avoid redirect mismatches.)
4. OAuth 2.0 grant types: **Authorization code grant**
5. OpenID Connect scopes: **openid**, **email**, **profile**
6. Click **Save changes**

The Cognito Hosted UI is now live and pointing at the real CloudFront domain —
no placeholder was ever registered.

---

## 12. Launch Template + Auto Scaling Group

> **Console:** EC2 → **Launch Templates** → **Create launch template**

**CloudFormation:** `AppLaunchTemplate`, `AppAutoScalingGroup`

> **Before starting:** collect the following values from earlier sections —
> you will paste them directly into the userdata below:
> - CloudFront domain from §11b (e.g. `xxxx.cloudfront.net`)
> - Cognito User Pool ID from §5a
> - Cognito App Client ID from §5a
> - Cognito Hosted Domain from §5a (e.g. `logistics-prod-auth-ACCOUNT.auth.REGION.amazoncognito.com`)
> - Media bucket name from §2 (S3 Media Bucket)

### Step 12a — Launch Template

1. Name: `logistics-prod-lt`
2. AMI: use the SSM-resolved path for Amazon Linux 2023 ARM64:
   `{{resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64}}`
   Or search for `al2023-ami-kernel-default-arm64` in AMI catalog
3. Instance type: `t4g.small`
4. IAM instance profile: `logistics-prod-app-role` (created in §9)
5. Security group: `logistics-prod-app-sg` (allows inbound 80 from ALB SG)
6. Metadata options: IMDSv2 **Required**
7. User data: paste the script below, replacing placeholders with the values
   collected above:

```bash
#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

REGION="YOUR_REGION"
S3_BUCKET="YOUR_ARTIFACT_BUCKET"
APP_KEY="logistics-prod/app.zip"
SECRET_NAME="rds/logistics-prod"
APP_DIR="/opt/app"
COGNITO_POOL_ID="YOUR_USER_POOL_ID"
COGNITO_CLIENT="YOUR_CLIENT_ID"
COGNITO_HOSTED_DOMAIN="YOUR_PREFIX-YOUR_ACCOUNT_ID.auth.YOUR_REGION.amazoncognito.com"
MEDIA_BUCKET="logistics-prod-media-YOUR_ACCOUNT_ID"
APP_URL="https://YOUR_CLOUDFRONT_DOMAIN"   # From §11b — e.g. https://xxxx.cloudfront.net

# Base packages
dnf update -y
dnf install -y python3.11 python3.11-pip nginx unzip awscli-2 postgresql15

# Fetch and extract app
mkdir -p "$APP_DIR"
aws s3 cp "s3://$S3_BUCKET/$APP_KEY" /tmp/app.zip --region "$REGION"
unzip -o /tmp/app.zip -d "$APP_DIR"
rm -f /tmp/app.zip

# Python venv
python3.11 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R ec2-user:ec2-user "$APP_DIR"

# NOTE: Do NOT generate a random per-instance FLASK_SECRET_KEY here.
# The app reads flask_secret_key from the Secrets Manager secret (DB_SECRET_NAME).
# All instances share the same value — generating a random key per instance would
# break session cookies across ALB instances. The env var fallback is only for
# local dev without Secrets Manager.

cat > /etc/systemd/system/flask-admin.service << EOF
[Unit]
Description=Logistics-Prod Flask App
After=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=$APP_DIR
Environment="DB_SECRET_NAME=$SECRET_NAME"
Environment="AWS_REGION=$REGION"
Environment="COGNITO_USER_POOL_ID=$COGNITO_POOL_ID"
Environment="COGNITO_CLIENT_ID=$COGNITO_CLIENT"
Environment="COGNITO_DOMAIN=$COGNITO_HOSTED_DOMAIN"
Environment="S3_MEDIA_BUCKET=$MEDIA_BUCKET"
Environment="APP_URL=$APP_URL"
ExecStart=$APP_DIR/venv/bin/gunicorn \
  --workers 2 --bind 127.0.0.1:8000 \
  --access-logfile - --error-logfile - \
  --timeout 60 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/nginx/conf.d/flask.conf << 'NGINX_EOF'
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 6M;
    location = /health { access_log off; proxy_pass http://127.0.0.1:8000/health; }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
NGINX_EOF

systemctl daemon-reload
systemctl enable --now flask-admin nginx
echo "user-data OK"
```

### Step 12b — Auto Scaling Group

> EC2 → **Auto Scaling Groups** → **Create Auto Scaling group**

1. Name: `logistics-prod-asg`
2. Launch template: `logistics-prod-lt`
3. VPC: your VPC
4. Subnets: both **private subnets**
5. Load balancing: attach to `logistics-prod-tg`
6. Health check type: **ELB**
7. Health check grace period: **600 seconds**
8. Desired/Min/Max: 2 / 2 / 4
9. Scaling policies: add target tracking (CPU 40%) or step policies from
   the original Logistics project

---

## 13. Schema Bootstrap via SSM Run Command

**Wait until at least one ASG instance shows "healthy" in the target group.**

```bash
# Option A: use the provided script (recommended — handles credentials automatically)
BACKEND_STACK=logistics-prod APP_REGION=us-east-1 \
  ./ssm-run-schema-from-artifact.sh

# Option B: manual SSM send-command
# Step 1: get the DB password from Secrets Manager
SECRET_ARN=$(aws cloudformation describe-stacks \
  --stack-name logistics-prod --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='SecretArn'].OutputValue | [0]" \
  --output text)

DB_PASS=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_ARN" --region us-east-1 \
  --query SecretString --output text | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

DB_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name logistics-prod --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='DbEndpoint'].OutputValue | [0]" \
  --output text)

# Note: use the ASG autoscaling group name tag, not the launch template Name tag.
# The ASG overrides the EC2 Name tag on instances — filter by the ASG groupName attribute.
INSTANCE_ID=$(aws ec2 describe-instances --region us-east-1 \
  --filters "Name=tag:aws:autoscaling:groupName,Values=logistics-prod-asg" \
            "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" --output text)

# Step 2: run schema.sql via SSM (PGPASSWORD avoids interactive prompt)
aws ssm send-command \
  --region us-east-1 \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters "commands=[\"PGPASSWORD='$DB_PASS' psql -h $DB_ENDPOINT -U appadmin -d logistics -f /opt/app/schema.sql\"]" \
  --query "Command.CommandId" --output text
```

> **Why PGPASSWORD?** psql looks for a `.pgpass` file or the `PGPASSWORD`
> environment variable when connecting non-interactively via SSM Run Command.
> Without it, the command fails with "fe_sendauth: no password supplied".

Verify: visit `http://<ALB-DNS>/dashboard` — you should see 10 customers,
10 drivers, 10 shipments.

---

## 14. Route 53 Alias (Optional)

> **Console:** Route 53 → **Hosted zones** → select your zone → **Create record**

**CloudFormation:** `Route53Record` (conditional in cfn/template.yaml)

1. Record name: `logistics` (for `logistics.example.com`)
2. Record type: **A**
3. Alias: **Yes**
4. Route traffic to: **Alias to CloudFront distribution**
5. Choose distribution: select `logistics-prod` from the dropdown
6. Click **Create records**

> Wait 1–5 minutes for DNS propagation, then visit `https://logistics.example.com/`.

---

## 15. End-to-End Smoke Test

Wait for all resources to be healthy before testing:
- ALB target group: all instances `healthy`
- CloudFront distribution: `Status = Deployed`

### Quick health checks

```bash
# Get your CloudFront domain from the stack output:
CF_DOMAIN=$(aws cloudformation describe-stacks --stack-name logistics-prod \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionDomain'].OutputValue | [0]" \
  --output text | sed 's|https://||')

MEDIA_BUCKET=$(aws cloudformation describe-stacks --stack-name logistics-prod \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='MediaBucketName'].OutputValue | [0]" \
  --output text)

# 1. HTTPS redirect works
curl -sI "http://$CF_DOMAIN/" | grep -i location

# 2. Public tracking page loads (Miss on 1st, Hit on 2nd)
curl -sI "https://$CF_DOMAIN/track/TRK-100001" | grep -i x-cache   # Miss from cloudfront
curl -sI "https://$CF_DOMAIN/track/TRK-100001" | grep -i x-cache   # Hit from cloudfront

# 3. S3 direct access is blocked (OAC enforcement)
curl -sI "https://$MEDIA_BUCKET.s3.us-east-1.amazonaws.com/shipments/1/test.jpg"
# Expect: HTTP/1.1 403 Forbidden

# 4. Open /admin/ in browser — should redirect to Cognito Hosted UI
open "https://$CF_DOMAIN/admin/"
```

### Browser flow

1. Open `https://$CF_DOMAIN/admin/` in a browser
2. Redirected to Cognito Hosted UI login page
3. Sign in as the Admin user (created in §5c)
4. Redirected back to Flask-Admin — you see Customers, Drivers, Shipments tables
5. Sign out (menu link in Flask-Admin)
6. Sign in as Driver user — you should see 403 on `/admin/` (Drivers group only allows `/driver/`)
7. Navigate to `https://$CF_DOMAIN/driver/shipments/2/photo`
8. Upload a small JPEG photo
9. After upload, redirected to `/track/TRK-100002` — photo appears on the page
10. Run `aws s3 ls s3://$MEDIA_BUCKET/shipments/2/` — confirm the object is there

---

## 16. Cleanup

**Order matters** — CloudFront must be disabled before deletion, and the
distribution takes ~15 minutes to disable globally.

### 16a — Disable CloudFront first

> CloudFront → select distribution → **Disable** → wait for `Status = Deployed`

### 16b — Delete resources in reverse order

```bash
# 1. Delete CloudFront distribution (after it's disabled)
aws cloudfront delete-distribution \
  --id DIST_ID \
  --if-match $(aws cloudfront get-distribution-config --id DIST_ID \
               --query 'ETag' --output text)

# 2. Delete WAF Web ACL (us-east-1)
aws wafv2 delete-web-acl \
  --region us-east-1 \
  --scope CLOUDFRONT \
  --name logistics-prod-waf \
  --id $(aws wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 \
         --query "WebACLs[?Name=='logistics-prod-waf'].Id | [0]" --output text) \
  --lock-token $(aws wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 \
                 --query "WebACLs[?Name=='logistics-prod-waf'].LockToken | [0]" --output text)

# 3. Delete ACM certs (both regions)
# 4. Empty and delete S3 media bucket
aws s3 rm s3://logistics-prod-media-ACCOUNT --recursive
aws s3 rb s3://logistics-prod-media-ACCOUNT

# 5. Delete CloudFormation stack
aws cloudformation delete-stack --stack-name logistics-prod --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name logistics-prod --region us-east-1

# 6. Delete Cognito User Pool (if not in CloudFormation)
# 7. Delete Secrets Manager secret
```

**Rough cleanup time:** 20–30 minutes (CloudFront distribution dominates).

---

## 17. Troubleshooting

### Cert region mismatch (CloudFront shows "No certificate available")

**Symptom:** After enabling custom domain in CloudFront, the ACM cert dropdown
is empty or your cert doesn't appear.

**Cause:** You created the cert in the wrong region.  CloudFront requires the
cert to be in `us-east-1`.

**Fix:** Go to `us-east-1` → Certificate Manager → request the cert again.
Validate with the same CNAME (already in DNS from §6 or §7 — it validates instantly).

---

### OAC 403 Forbidden on /media/* URLs

**Symptom:** `https://xxxx.cloudfront.net/media/shipments/...` returns 403.

**Possible causes:**

1. **S3 key prefix mismatch (most common)** — CloudFront forwards the full request
   path to S3. A request for `/media/shipments/1/uuid.jpg` looks up S3 key
   `media/shipments/1/uuid.jpg`. If the app uploaded to `shipments/1/uuid.jpg`
   (missing the `media/` prefix), CloudFront can't find the object and returns 403.
   **Fix:** Ensure the app uploads with the `media/` prefix in the S3 key, e.g.:
   ```python
   s3_key = f"media/shipments/{shipment_id}/{uuid}.jpg"
   ```
   And the IAM PutObject resource must match:
   ```
   arn:aws:s3:::bucket/media/*
   ```
   Migrate existing objects: `aws s3 cp s3://bucket/shipments/ s3://bucket/media/shipments/ --recursive`
   Update DB records: `UPDATE shipments SET proof_photo_key = 'media/' || proof_photo_key WHERE proof_photo_key NOT LIKE 'media/%';`

2. **Bucket policy not applied** — Go to S3 → bucket → Permissions → Bucket policy.
   If empty, paste the policy from §11c.

3. **Wrong S3 origin format** — The S3 origin domain must be in path-style format:
   `bucket-name.s3.region.amazonaws.com`
   NOT `s3.amazonaws.com/bucket-name` (old style).

4. **OAC not attached to origin** — In CloudFront, edit the S3 origin and verify
   that Origin access is set to "Origin access control settings" and the OAC is selected.

5. **Object doesn't exist** — Check `aws s3 ls s3://bucket/media/shipments/` to confirm
   the key exists.

---

### CloudFront cache stickiness (old content served after update)

**Symptom:** You updated the app but CloudFront still serves the old version.

**Fix:**
```bash
aws cloudfront create-invalidation \
  --distribution-id DIST_ID \
  --paths "/*"
# Or invalidate a specific path:
  --paths "/track/TRK-100001"
```

Invalidations take ~60 seconds.  For the teaching demo, the `/track/*` 60s TTL
means content auto-refreshes anyway.

---

### JWT clock skew causing 401 errors

**Symptom:** Login succeeds at Cognito, but Flask returns 401 immediately after redirect.

**Cause:** The EC2 instance's system clock is drifted.  JWT validation checks `exp`
(expiry) and `nbf` (not before) timestamps.  If the clock is off by more than 60
seconds, `python-jose` rejects valid tokens.

**Fix:**
```bash
# SSH into an instance via SSM Session Manager:
aws ssm start-session --target INSTANCE_ID

# Check time:
date
timedatectl

# Force NTP sync:
chronyc makestep
```

AL2023 uses `chrony` for NTP by default.  The EC2 time is synced from Amazon
Time Sync Service (`169.254.169.123`).  Rebooting the instance usually fixes drift.

---

### WAF false positives blocking legitimate requests

**Symptom:** Some admin actions return 403 even after successful login.

**Cause:** The AWSManagedRulesCommonRuleSet sometimes matches legitimate HTTP
bodies (e.g. JSON with SQL-like strings).

**Fix — Check WAF logs:**
> WAF → `logistics-prod-waf` → **Logging and metrics** → enable logging to S3 or CloudWatch
```bash
aws wafv2 get-sampled-requests \
  --web-acl-arn WAF_ARN \
  --rule-metric-name logistics-prod-common-rules \
  --scope CLOUDFRONT \
  --time-window StartTime=2026-05-08T00:00:00Z,EndTime=2026-05-08T23:59:59Z \
  --max-items 100
```

**Temporary fix — override to Count mode:**
> WAF → edit the rule group → change action from **Block** to **Count** for the
> specific rule causing false positives.

---

### Cognito login loop (redirect back to Hosted UI immediately)

**Symptom:** Login at Hosted UI succeeds, redirected back to the app, then
immediately redirected to Hosted UI again.

**Cause:** Usually a callback URL mismatch.  Cognito's allowed callback URLs must
exactly match the URL the app sends in the authorization request.

**Fix:**
1. Check what URL the app constructs:
   - `APP_URL` env var on the EC2 instance
   - The callback is `$APP_URL/auth/callback`
2. Check Cognito app client → Allowed callback URLs
3. Make sure they match exactly (including trailing slash, http vs https)

---

### CFN: `HostedRotationLambda` requires a transform

**Symptom:** `DBSecretRotation CREATE_FAILED: To use the HostedRotationLambda property, you must use the AWS::SecretsManager transform`

**Fix:** Add `Transform: AWS::SecretsManager-2020-07-23` at the top of `cfn/template.yaml` (alongside `AWSTemplateFormatVersion`) and add `CAPABILITY_AUTO_EXPAND` to the deploy command:

```bash
aws cloudformation create-stack ... \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
```

This is a Secrets Manager-specific macro transform — distinct from the SAM transform.

---

### CFN: Security Group `GroupDescription` rejects non-ASCII

**Symptom:** `AlbSecurityGroup CREATE_FAILED: Value (...) for parameter GroupDescription is invalid. Character sets beyond ASCII are not supported.`

**Fix:** The EC2 API only accepts ASCII in `GroupDescription`. Remove em dashes (`—`), curly quotes, or any other Unicode and replace with plain hyphens.

---

### CFN: retained S3 bucket blocks re-deploy

**Symptom:** After a failed stack (ROLLBACK_COMPLETE) and delete, the next `create-stack` fails with `logistics-prod-media-ACCOUNT already exists`.

**Cause:** `DeletionPolicy: Retain` on the media bucket means it survives stack deletion. The next deploy tries to create the same bucket name and collides.

**Fix:** Delete the orphaned bucket first (`aws s3 rb s3://BUCKET_NAME`) — check it's empty first (`aws s3 ls s3://BUCKET_NAME`). For teaching demos, set `DeletionPolicy: Delete` on the bucket to avoid this entirely.

---

## Appendix A — What comes next (not covered here)

| Feature | Approach |
|---|---|
| Multi-region active-active | ECS Fargate + DynamoDB Global Tables + Route 53 latency routing |
| Read replicas | RDS read replica → Flask-SQLAlchemy SQLALCHEMY_BINDS |
| Session caching | ElastiCache Redis for Flask sessions |
| CI/CD pipeline | CodePipeline → CodeBuild → S3 artifact → ASG instance refresh |
| Containerization | ECS Fargate (separate project path) |
| Full HTTPS backend | Origin domain cert + CloudFront OriginProtocolPolicy: https-only |

## Appendix B — AWS CLI Verification Checklist

See the Verification section in the project plan for the full A1–A12 + B1–B9
verification script.  Run the (A) config checks first — if any fail, don't
proceed to functional (B) tests.

```bash
export APP_REGION=us-east-1
export STACK=logistics-prod

# A1. Template validates
aws cloudformation validate-template \
  --template-body file://cfn/template.yaml --region "$APP_REGION"

# A4. RDS is Multi-AZ
aws rds describe-db-instances \
  --db-instance-identifier logistics-prod-pg \
  --region "$APP_REGION" \
  --query 'DBInstances[0].[MultiAZ,BackupRetentionPeriod,StorageEncrypted]' \
  --output table
# Expect: True | 7 | True

# A11. WAF rules
WAF_ID=$(aws wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 \
  --query "WebACLs[?Name=='logistics-prod-waf'].Id | [0]" --output text)
aws wafv2 get-web-acl --scope CLOUDFRONT --region us-east-1 \
  --name logistics-prod-waf --id "$WAF_ID" \
  --query 'WebACL.Rules[].Name' --output text
# Expect: AWS-AWSManagedRulesCommonRuleSet
#         AWS-AWSManagedRulesAmazonIpReputationList
#         RateLimit

# A12. Cognito callback URLs wired to real CloudFront domain (not a placeholder)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --region "$APP_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoClientId'].OutputValue | [0]" \
  --output text)
POOL_ID=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --region "$APP_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue | [0]" \
  --output text)
aws cognito-idp describe-user-pool-client \
  --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
  --region "$APP_REGION" \
  --query 'UserPoolClient.CallbackURLs'
# Expect: URLs containing your actual CloudFront domain (no placeholder)
```

---

## Appendix C — Deploy via CloudFormation (shortcut)

> **When to use this:** If you've already walked through the console once and understand what each resource does, this shortcut deploys the entire stack from scratch with a single CLI command — useful for classroom resets, teardown-and-rebuild, or CI testing.

**Prerequisites:**
- AWS CLI configured (`aws configure` or environment credentials)
- App zip uploaded to your artifact bucket (§0a)
- A unique Cognito domain prefix chosen (e.g. `logistics-prod-auth-<AccountId>`)
- Deploy to **us-east-1** (CloudFront/WAF/ACM are control-plane-locked to us-east-1; `cfn/template.yaml` is a merged single-stack template that assumes this region)

> **Note for non-us-east-1 backends:** If you need the backend in a different region, the original two-stack layout (`01-backend.yaml` + `02-edge.yaml`) is preserved in git history. The merged single template is the right path for standard classroom deployments.

### Step C1 — Deploy the stack

```bash
export APP_REGION=us-east-1
export ARTIFACT_BUCKET=logistics-prod-artifacts-YOUR_ACCOUNT_ID
export COGNITO_PREFIX=logistics-prod-auth-YOUR_ACCOUNT_ID   # must be globally unique

aws cloudformation create-stack \
  --stack-name logistics-prod \
  --template-body file://cfn/template.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --region "$APP_REGION" \
  --parameters \
    ParameterKey=ArtifactBucket,ParameterValue="$ARTIFACT_BUCKET" \
    ParameterKey=CognitoDomainPrefix,ParameterValue="$COGNITO_PREFIX"
```

**Optional overrides** (all have defaults — omit to accept defaults):

| Parameter | Default | Override example |
|---|---|---|
| `ArtifactKey` | `logistics-prod/app.zip` | `logistics-prod/v2/app.zip` |
| `AppInstanceType` | `t4g.small` | `t4g.medium` |
| `DbInstanceClass` | `db.t4g.small` | `db.t4g.medium` |
| `DomainName` | *(empty — CloudFront default domain)* | `logistics.example.com` |
| `HostedZoneId` | *(empty — no Route 53 alias)* | `Z1234567890` |
| `AdminInitialEmail` | *(empty — create users manually)* | `admin@example.com` |
| `VpcCidr` | `10.20.0.0/16` | `10.30.0.0/16` |

Wait for `CREATE_COMPLETE` (~15 minutes — RDS Multi-AZ and CloudFront dominate):

```bash
aws cloudformation wait stack-create-complete \
  --stack-name logistics-prod \
  --region "$APP_REGION"
```

### Step C2 — Get your CloudFront URL

```bash
aws cloudformation describe-stacks \
  --stack-name logistics-prod \
  --region "$APP_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`PublicUrl`].OutputValue' \
  --output text
```

Cognito callback URLs are wired automatically — no manual update required. Verify:

```bash
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name logistics-prod \
  --region "$APP_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoClientId'].OutputValue | [0]" \
  --output text)
POOL_ID=$(aws cloudformation describe-stacks --stack-name logistics-prod \
  --region "$APP_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue | [0]" \
  --output text)
aws cognito-idp describe-user-pool-client \
  --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
  --region "$APP_REGION" \
  --query 'UserPoolClient.CallbackURLs'
```

### Step C3 — Bootstrap the database schema

Once at least one ASG instance is healthy, run the schema bootstrap via SSM (same as §13):

```bash
BACKEND_STACK=logistics-prod APP_REGION="$APP_REGION" \
  ./scripts/ssm-run-schema-from-artifact.sh
```

### Cleanup

```bash
# CloudFront distribution takes 10-15 min to disable/delete — single wait covers all
aws cloudformation delete-stack --stack-name logistics-prod --region "$APP_REGION"
aws cloudformation wait stack-delete-complete --stack-name logistics-prod --region "$APP_REGION"
```

> **Note:** The media S3 bucket has `DeletionPolicy: Retain` — it survives stack deletion. Delete it manually for a full teardown: `aws s3 rb s3://logistics-prod-media-ACCOUNT_ID --force`.
