"""
================================================================================
 app.py -- Logistics-Prod Flask application
================================================================================

 What this adds on top of the original Logistics (ASG+RDS) app:

   1. Cognito JWT middleware  -- before_request hook that validates
      Authorization: Bearer <id_token> for /admin/* and /driver/*.
      Falls back to Flask session for browser users (OAuth code flow).
      JWKS are cached in-process for 1 hour (avoids a JWKS fetch per request).

   2. OAuth callback route  -- /auth/callback exchanges the Cognito auth code
      for tokens, stores the id_token in the Flask session, then redirects
      to the originally requested URL.

   3. Public tracking page  -- GET /track/<tracking_number> renders shipment
      status and the proof-of-delivery photo URL (via CloudFront /media/*).
      No auth required. CloudFront caches this with a 60-second TTL.

   4. Driver photo upload  -- POST /driver/shipments/<id>/photo accepts a
      multipart JPEG/PNG (<=5 MB). boto3 writes to S3 using the EC2 instance
      role (no credentials embedded). Key format: shipments/{id}/{uuid}.jpg
      stored in shipments.proof_photo_key.

   5. Shipment model extended  -- proof_photo_key TEXT column.

 Traffic path (production):
     Browser --HTTPS--> CloudFront --HTTP--> ALB :80 ---> nginx :80
                                                              |
                                                        gunicorn :8000
                                                              |
                                                        this app
                                                         /    \
                                                     RDS PG   S3 (boto3)

 Environment variables (set by systemd unit in userdata):
   DB_SECRET_NAME       -- Secrets Manager secret (RDS credentials)
   AWS_REGION           -- AWS region
   FLASK_SECRET_KEY     -- Flask session signing key
   COGNITO_USER_POOL_ID -- Cognito user pool ID (e.g. us-east-1_Abc123)
   COGNITO_CLIENT_ID    -- Cognito app client ID (no secret -- public client)
   COGNITO_DOMAIN       -- Hosted UI FQDN (e.g. logistics-prod-auth-123456789012.auth.us-east-1.amazoncognito.com)
   S3_MEDIA_BUCKET      -- S3 bucket name for proof-of-delivery photos
   APP_URL              -- Public URL of this app (e.g. https://xxxx.cloudfront.net)
================================================================================
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid

import boto3
from flask import Flask, g, redirect, render_template, render_template_string, request, session, url_for
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_admin.menu import MenuLink
from markupsafe import Markup
from flask_sqlalchemy import SQLAlchemy
from jose import JWTError, jwt
from sqlalchemy import func

# --------------------------------------------------------------------------- #
# 1. Configuration                                                              #
# --------------------------------------------------------------------------- #

SECRET_NAME          = os.environ["DB_SECRET_NAME"]
REGION               = os.environ.get("AWS_REGION", "us-east-1")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID    = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_DOMAIN       = os.environ.get("COGNITO_DOMAIN", "")
S3_MEDIA_BUCKET      = os.environ.get("S3_MEDIA_BUCKET", "")
APP_URL              = os.environ.get("APP_URL", "").rstrip("/")
CF_DISTRIBUTION_ID   = os.environ.get("CF_DISTRIBUTION_ID", "")
MAX_PHOTO_BYTES      = 5 * 1024 * 1024  # 5 MB

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# --------------------------------------------------------------------------- #
# 2. DB credentials from Secrets Manager                                       #
# --------------------------------------------------------------------------- #
# Same pattern as the original Logistics app. The secret JSON has shape:
#   { username, password, engine, host, port, dbname }
# boto3 picks up the EC2 instance role automatically -- no credentials stored.

def _load_db_credentials() -> dict:
    log.info("Fetching DB credentials from Secrets Manager (%s)", SECRET_NAME)
    sm = boto3.client("secretsmanager", region_name=REGION)
    resp = sm.get_secret_value(SecretId=SECRET_NAME)
    return json.loads(resp["SecretString"])


_creds = _load_db_credentials()
_DB_URI = (
    f"postgresql+psycopg2://{_creds['username']}:{_creds['password']}"
    f"@{_creds['host']}:{_creds['port']}/{_creds['dbname']}"
)

# --------------------------------------------------------------------------- #
# 3. Flask + SQLAlchemy                                                         #
# --------------------------------------------------------------------------- #

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"]        = _DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = (
    _creds.get("flask_secret_key")           # shared key from Secrets Manager (all instances)
    or os.environ.get("FLASK_SECRET_KEY", "change-me-in-prod")
)
# Recycle connections before RDS idle-timeout (8h default) drops them.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle":  280,
}
# Reject uploads over 5 MB before they reach route handlers.
app.config["MAX_CONTENT_LENGTH"] = MAX_PHOTO_BYTES + 1024

db = SQLAlchemy(app)

# --------------------------------------------------------------------------- #
# 4. Models                                                                     #
# --------------------------------------------------------------------------- #
# We describe (not create) the schema -- DDL was run separately via schema.sql.
# proof_photo_key is the only addition vs. the original Logistics schema.


class Customer(db.Model):
    __tablename__ = "customers"
    customer_id = db.Column(db.BigInteger, primary_key=True)
    full_name   = db.Column(db.String(120), nullable=False)
    email       = db.Column(db.String(255), unique=True, nullable=False)
    phone       = db.Column(db.String(30))
    city        = db.Column(db.String(80))
    created_at  = db.Column(db.DateTime(timezone=True))

    shipments = db.relationship("Shipment", back_populates="customer", lazy="dynamic")

    def __repr__(self):
        return f"{self.full_name} <{self.email}>"


class Driver(db.Model):
    __tablename__ = "drivers"
    driver_id      = db.Column(db.BigInteger, primary_key=True)
    full_name      = db.Column(db.String(120), nullable=False)
    license_number = db.Column(db.String(40),  unique=True, nullable=False)
    phone          = db.Column(db.String(30))
    vehicle_plate  = db.Column(db.String(20))
    hired_at       = db.Column(db.Date)

    shipments = db.relationship("Shipment", back_populates="driver", lazy="dynamic")

    def __repr__(self):
        return f"{self.full_name} [{self.vehicle_plate}]"


class Shipment(db.Model):
    __tablename__ = "shipments"
    shipment_id     = db.Column(db.BigInteger, primary_key=True)
    tracking_number = db.Column(db.String(30), unique=True, nullable=False)
    customer_id     = db.Column(db.BigInteger,
                                db.ForeignKey("customers.customer_id"),
                                nullable=False)
    driver_id       = db.Column(db.BigInteger,
                                db.ForeignKey("drivers.driver_id"))
    origin          = db.Column(db.String(120))
    destination     = db.Column(db.String(120))
    weight_kg       = db.Column(db.Numeric(8, 2))
    status          = db.Column(db.String(20), nullable=False)
    created_at      = db.Column(db.DateTime(timezone=True))
    delivered_at    = db.Column(db.DateTime(timezone=True))
    proof_photo_key = db.Column(db.Text)

    customer = db.relationship("Customer", back_populates="shipments")
    driver   = db.relationship("Driver",   back_populates="shipments")

    def __repr__(self):
        return f"{self.tracking_number} ({self.status})"


# --------------------------------------------------------------------------- #
# 5. Cognito JWT helpers                                                        #
# --------------------------------------------------------------------------- #
#
# How Cognito JWT validation works:
#   1. Cognito signs JWTs with RS256 (RSA private key).
#   2. The matching public keys are published at the well-known JWKS endpoint.
#   3. We fetch those keys once, cache them for 1 hour, then use python-jose
#      to verify the token's signature, expiry, audience, and issuer.
#   4. The 'cognito:groups' claim in the id_token is a list of group names
#      the user belongs to -- ["Admins"] or ["Drivers"] etc.
#
# JWKS endpoint format:
#   https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json

_jwks_cache: dict = {}
_jwks_fetched_at: float = 0.0


def _get_jwks() -> dict:
    """Fetch and cache Cognito JWKS (public keys). Refreshes every hour."""
    global _jwks_cache, _jwks_fetched_at
    if _jwks_cache and (time.time() - _jwks_fetched_at) < 3600:
        return _jwks_cache
    jwks_url = (
        f"https://cognito-idp.{REGION}.amazonaws.com"
        f"/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"
    )
    log.info("Fetching JWKS from %s", jwks_url)
    with urllib.request.urlopen(jwks_url, timeout=5) as resp:
        _jwks_cache = json.loads(resp.read())
    _jwks_fetched_at = time.time()
    return _jwks_cache


def _verify_token(token: str) -> dict:
    """
    Validate a Cognito id_token.  Returns the decoded claims dict.
    Raises jose.JWTError (or subclass) on any failure.
    """
    if not COGNITO_USER_POOL_ID:
        raise JWTError("COGNITO_USER_POOL_ID not configured")

    jwks = _get_jwks()
    headers = jwt.get_unverified_headers(token)
    kid = headers.get("kid")
    key = next((k for k in jwks["keys"] if k["kid"] == kid), None)
    if key is None:
        raise JWTError(f"Public key kid={kid} not found in JWKS")

    issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
    return jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=COGNITO_CLIENT_ID,
        issuer=issuer,
        options={"verify_at_hash": False},
    )


def _get_bearer_token() -> str | None:
    """Extract Bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else None


def _cognito_login_url() -> str:
    """Build the Cognito Hosted UI authorization URL for the OAuth code flow."""
    callback = urllib.parse.quote(f"{APP_URL}/auth/callback", safe="")
    return (
        f"https://{COGNITO_DOMAIN}/oauth2/authorize"
        f"?response_type=code"
        f"&client_id={COGNITO_CLIENT_ID}"
        f"&redirect_uri={callback}"
        f"&scope=openid+email+profile"
    )


# Routes that need no authentication at all.
_PUBLIC_EXACT    = {"/", "/dashboard", "/health"}
_PUBLIC_PREFIXES = ("/track", "/static", "/auth")


# --------------------------------------------------------------------------- #
# 6. Auth middleware                                                             #
# --------------------------------------------------------------------------- #

@app.before_request
def require_auth():
    """
    Protect /admin/* and /driver/* routes with Cognito JWT.

    Flow for API clients (curl, mobile apps):
      - Must include  Authorization: Bearer <id_token>  header.

    Flow for browser users:
      - No token present -> store intended URL in session, redirect to
        Cognito Hosted UI.
      - Cognito Hosted UI -> redirects to /auth/callback?code=xxx.
      - /auth/callback exchanges code for tokens, saves id_token in session,
        redirects to the originally intended URL.
      - Subsequent requests find the id_token in the session.

    Group authorization:
      - Admins  ->  /admin/* and /driver/* (full access)
      - Drivers ->  /driver/* only
    """
    path = request.path

    # Public routes -- no auth needed.
    if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return

    # Only enforce on admin/driver prefixes; let everything else through.
    protected = path.startswith("/admin") or path.startswith("/driver")

    # Prefer the Authorization header (API clients); fall back to session (browsers).
    token = _get_bearer_token() or session.get("id_token")

    if token is None:
        if protected:
            session["next"] = request.url
            return redirect(_cognito_login_url())
        return  # non-protected path, no token -- let the handler decide

    try:
        claims = _verify_token(token)
    except JWTError as exc:
        log.warning("JWT invalid: %s", exc)
        session.pop("id_token", None)
        if protected:
            session["next"] = request.url
            return redirect(_cognito_login_url())
        return

    g.claims = claims
    # Cognito stores group membership as a JSON array in the id_token.
    g.groups = set(claims.get("cognito:groups") or [])

    # Enforce group-level access control.
    if path.startswith("/admin") and "Admins" not in g.groups:
        return "Forbidden — Admins Cognito group required", 403
    if path.startswith("/driver") and not g.groups.intersection({"Admins", "Drivers"}):
        return "Forbidden — Drivers Cognito group required", 403


# --------------------------------------------------------------------------- #
# 7. OAuth callback                                                             #
# --------------------------------------------------------------------------- #

@app.route("/auth/callback")
def auth_callback():
    """
    Cognito Hosted UI redirects here after login.
    Exchange the one-time auth code for tokens, save id_token in session.

    Teaching note: this is the Authorization Code flow (OAuth 2.0).
    We use a PUBLIC client (no client_secret) -- safe because the token
    endpoint is called server-side, not from the browser.
    """
    code = request.args.get("code")
    if not code:
        log.warning("auth_callback: missing code parameter")
        return "Missing authorization code", 400

    callback_url = f"{APP_URL}/auth/callback"
    token_url    = f"https://{COGNITO_DOMAIN}/oauth2/token"

    body = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": callback_url,
        "client_id":    COGNITO_CLIENT_ID,
    }).encode()

    req = urllib.request.Request(
        token_url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            tokens = json.loads(resp.read())
    except Exception as exc:
        log.error("Token exchange failed: %s", exc)
        return "Authentication failed — could not exchange code for tokens", 502

    session["id_token"] = tokens["id_token"]
    # Redirect to where the user was trying to go, or the dashboard.
    next_url = session.pop("next", url_for("dashboard"))
    return redirect(next_url)


@app.route("/auth/logout")
def auth_logout():
    """Clear local session and redirect to Cognito logout endpoint."""
    session.clear()
    logout_url = (
        f"https://{COGNITO_DOMAIN}/logout"
        f"?client_id={COGNITO_CLIENT_ID}"
        f"&logout_uri={urllib.parse.quote(APP_URL + '/', safe='')}"
    )
    return redirect(logout_url)


# --------------------------------------------------------------------------- #
# 8. Flask-Admin (protected by the before_request middleware)                   #
# --------------------------------------------------------------------------- #

class AdminModelView(ModelView):
    """Adds Flask-Admin native access control on top of the middleware."""
    def is_accessible(self):
        return hasattr(g, "groups") and "Admins" in g.groups

    def inaccessible_callback(self, name, **kwargs):
        session["next"] = request.url
        return redirect(_cognito_login_url())


class CustomerView(AdminModelView):
    column_list             = ("customer_id", "full_name", "email", "city", "shipment_count")
    column_searchable_list  = ("full_name", "email", "city")
    column_filters          = ("city",)
    column_labels           = {"shipment_count": "# Shipments"}

    def _shipment_count(view, context, model, name):
        return model.shipments.count()

    column_formatters = {"shipment_count": _shipment_count}


class DriverView(AdminModelView):
    column_list             = ("driver_id", "full_name", "vehicle_plate",
                               "license_number", "shipment_count")
    column_searchable_list  = ("full_name", "vehicle_plate", "license_number")
    column_labels           = {"shipment_count": "# Shipments"}

    def _shipment_count(view, context, model, name):
        return model.shipments.count()

    column_formatters = {"shipment_count": _shipment_count}


class ShipmentView(AdminModelView):
    column_list             = (
        "tracking_number", "customer", "driver",
        "origin", "destination", "status", "photo_preview", "created_at",
    )
    column_searchable_list  = ("tracking_number", "origin", "destination")
    column_filters          = ("status", "origin", "destination")
    column_default_sort     = ("created_at", True)
    column_labels           = {"photo_preview": "Photo"}
    # proof_photo_key is set by the driver upload form, not manually edited.
    form_excluded_columns   = ("proof_photo_key",)
    form_choices = {
        "status": [
            ("pending",    "Pending"),
            ("in_transit", "In Transit"),
            ("delivered",  "Delivered"),
            ("delayed",    "Delayed"),
            ("canceled",   "Canceled"),
        ]
    }

    def _photo_preview(view, context, model, name):
        if not model.proof_photo_key:
            return ""
        # proof_photo_key already includes the "media/" prefix (e.g. media/shipments/1/uuid.jpg)
        url = f"{APP_URL}/{model.proof_photo_key}"
        track_url = f"{APP_URL}/track/{model.tracking_number}"
        return Markup(
            f'<a href="{url}" target="_blank">'
            f'<img src="{url}" style="height:60px;border-radius:4px;" '
            f'onerror="this.parentElement.innerHTML=\'<a href=&quot;{track_url}&quot;>view tracking</a>\'">'
            f'</a>'
        )

    column_formatters = {"photo_preview": _photo_preview}


admin_ui = Admin(
    app,
    name="Logistics-Prod Admin",
    template_mode="bootstrap4",
    url="/admin",
)
admin_ui.add_view(CustomerView(Customer, db.session, name="Customers"))
admin_ui.add_view(DriverView(Driver,     db.session, name="Drivers"))
admin_ui.add_view(ShipmentView(Shipment, db.session, name="Shipments"))
admin_ui.add_link(MenuLink(name="Dashboard",     url="/dashboard"))
admin_ui.add_link(MenuLink(name="Sign out",      url="/auth/logout"))


# --------------------------------------------------------------------------- #
# 9. Public tracking page                                                       #
# --------------------------------------------------------------------------- #

@app.route("/track/<tracking_number>")
def track_shipment(tracking_number):
    """
    Public shipment status page.
    No auth required.  CloudFront caches this with a 60-second TTL
    (see /track/* cache behavior in CF/02-edge.yaml).

    The proof-of-delivery photo URL is served via CloudFront /media/*,
    not directly from S3 (S3 blocks all public access; only CloudFront
    can read via OAC).
    """
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first()

    photo_url = None
    if shipment and shipment.proof_photo_key:
        # proof_photo_key already includes "media/" prefix
        photo_url = f"{APP_URL}/{shipment.proof_photo_key}"

    return render_template(
        "track.html",
        shipment=shipment,
        tracking_number=tracking_number,
        photo_url=photo_url,
    )


# --------------------------------------------------------------------------- #
# 10. Driver photo upload                                                        #
# --------------------------------------------------------------------------- #

@app.route("/driver/upload")
def driver_upload_select():
    """Landing page — driver picks a shipment from a dropdown then goes to the upload form."""
    shipments = (
        Shipment.query
        .filter(Shipment.status.in_(["pending", "in_transit"]))
        .order_by(Shipment.tracking_number)
        .all()
    )
    return render_template_string(_SELECT_SHIPMENT_HTML, shipments=shipments)


_SELECT_SHIPMENT_HTML = """
<!doctype html><html lang="en"><head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Select Shipment — Logistics-Prod</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>body{background:#f8f9fa;padding:40px 16px;font-family:system-ui,sans-serif;}.card{max-width:480px;margin:0 auto;}</style>
</head><body>
<div class="card shadow-sm p-4">
  <h2 class="mb-1">Upload Delivery Photo</h2>
  <p class="text-muted mb-4">Select the shipment you are delivering</p>
  <form onsubmit="window.location='/driver/shipments/'+document.getElementById('sid').value+'/photo'; return false;">
    <div class="mb-3">
      <label class="form-label fw-semibold">Shipment</label>
      <select id="sid" class="form-select form-select-lg" required>
        <option value="">-- choose shipment --</option>
        {% for s in shipments %}
        <option value="{{ s.shipment_id }}">
          {{ s.tracking_number }} — {{ s.origin }} → {{ s.destination }}
          ({{ s.status.replace('_',' ') }})
        </option>
        {% endfor %}
      </select>
    </div>
    <div class="d-grid">
      <button class="btn btn-primary btn-lg" type="submit">Continue to Upload →</button>
    </div>
  </form>
  <a href="/dashboard" class="btn btn-link mt-2 text-muted">← Back to dashboard</a>
</div>
</body></html>
"""


@app.route("/driver/shipments/<int:shipment_id>/photo", methods=["GET", "POST"])
def driver_photo_upload(shipment_id):
    """
    GET  -- render the upload form (driver_upload.html).
    POST -- accept the JPEG/PNG, write to S3, update shipments.proof_photo_key.

    Teaching note on S3 upload:
      boto3.client('s3').put_object() is called with no explicit credentials.
      The AWS SDK finds the EC2 instance role automatically via the IMDS
      metadata service (http://169.254.169.254/latest/meta-data/iam/...).
      The IAM role (AppInstanceRole in 01-backend.yaml) grants s3:PutObject
      on the media bucket.  No access keys are stored anywhere.

    S3 key format: shipments/{shipment_id}/{uuid}.jpg
    """
    shipment = Shipment.query.get_or_404(shipment_id)

    if request.method == "GET":
        return render_template("driver_upload.html", shipment=shipment)

    # --- POST: validate and upload ---

    if "photo" not in request.files:
        return "No photo file in request (field name must be 'photo')", 400

    photo_file   = request.files["photo"]
    content_type = photo_file.content_type

    if content_type not in ("image/jpeg", "image/png"):
        return (
            f"Unsupported content type: {content_type}. "
            "Only image/jpeg and image/png are accepted.", 415
        )

    # Read with a size guard (MAX_CONTENT_LENGTH catches oversized requests
    # at the WSGI level, but we check again here for safety).
    data = photo_file.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        return "File exceeds 5 MB limit", 413

    ext = "jpg" if content_type == "image/jpeg" else "png"
    # Key must include "media/" prefix so it matches the CloudFront /media/* → S3 path mapping.
    # CloudFront strips the leading "/" but keeps "media/", so the object must live at
    # media/shipments/{id}/{uuid}.ext for the URL /media/shipments/{id}/{uuid}.ext to resolve.
    s3_key = f"media/shipments/{shipment_id}/{uuid.uuid4()}.{ext}"

    s3 = boto3.client("s3", region_name=REGION)
    s3.put_object(
        Bucket=S3_MEDIA_BUCKET,
        Key=s3_key,
        Body=data,
        ContentType=content_type,
    )
    log.info("Uploaded proof photo s3://%s/%s", S3_MEDIA_BUCKET, s3_key)

    shipment.proof_photo_key = s3_key
    db.session.commit()

    # Invalidate the CloudFront cache for this tracking page so the photo
    # appears immediately (the /track/* behavior caches for 60s).
    if CF_DISTRIBUTION_ID:
        try:
            cf = boto3.client("cloudfront", region_name="us-east-1")
            cf.create_invalidation(
                DistributionId=CF_DISTRIBUTION_ID,
                InvalidationBatch={
                    "Paths": {"Quantity": 1, "Items": [f"/track/{shipment.tracking_number}"]},
                    "CallerReference": str(uuid.uuid4()),
                },
            )
            log.info("Invalidated /track/%s", shipment.tracking_number)
        except Exception as exc:
            log.warning("CloudFront invalidation failed (non-fatal): %s", exc)

    return redirect(url_for("track_shipment", tracking_number=shipment.tracking_number))


# --------------------------------------------------------------------------- #
# 11. Dashboard                                                                  #
# --------------------------------------------------------------------------- #

_DASHBOARD_HTML = """
<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Logistics-Prod</title>
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>
    body { background: #f8f9fa; font-family: system-ui, sans-serif; }
    .hero { background: #0d6efd; color: #fff; padding: 48px 24px 40px; }
    .hero h1 { font-size: 2rem; font-weight: 700; }
    .num { font-size: 2rem; font-weight: 600; }
    .user-bar { background: #e9ecef; padding: 8px 24px; font-size: .875rem; }
  </style>
</head><body>

<!-- User bar -->
<div class="user-bar d-flex justify-content-between align-items-center">
  <span>
    {% if current_user %}
      Logged in as <strong>{{ current_user }}</strong>
      {% if is_admin %}<span class="badge bg-primary ms-1">Admin</span>{% endif %}
      {% if is_driver and not is_admin %}<span class="badge bg-secondary ms-1">Driver</span>{% endif %}
    {% else %}
      <span class="text-muted">Not logged in</span>
    {% endif %}
  </span>
  <span>
    {% if current_user %}
      <a href="/auth/logout" class="btn btn-sm btn-outline-danger">Sign out</a>
    {% else %}
      <a href="/admin/" class="btn btn-sm btn-primary">Sign in</a>
    {% endif %}
  </span>
</div>

<!-- Hero -->
<div class="hero">
  <div class="container">
    <h1>Logistics-Prod</h1>
    <p class="mb-4 opacity-75">Production-ready delivery management — CloudFront &middot; Cognito &middot; WAF &middot; RDS Multi-AZ</p>

    <!-- Tracking form -->
    <form action="#" onsubmit="window.location='/track/'+document.getElementById('tn').value.trim(); return false;"
          class="d-flex gap-2" style="max-width:440px">
      <input id="tn" type="text" class="form-control" placeholder="Enter tracking number e.g. TRK-100001" required>
      <button class="btn btn-light fw-semibold px-4" type="submit">Track</button>
    </form>
  </div>
</div>

<div class="container py-4">

  <!-- Action buttons -->
  <div class="mb-4 d-flex gap-2 flex-wrap">
    {% if is_admin %}
      <a class="btn btn-primary" href="/admin/">Admin GUI &rarr;</a>
    {% endif %}
    {% if is_driver or is_admin %}
      <a class="btn btn-outline-secondary" href="/driver/upload">Upload delivery photo</a>
    {% endif %}
    {% if not current_user %}
      <a class="btn btn-outline-primary" href="/admin/">Sign in as Admin</a>
      <a class="btn btn-outline-secondary" href="/driver/upload">Sign in as Driver</a>
    {% endif %}
  </div>

  <!-- Stats -->
  <div class="row g-3 mb-4">
    <div class="col-6 col-md-3"><div class="card p-3 shadow-sm text-center">
      <div class="text-muted small">Customers</div><div class="num">{{ cust }}</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card p-3 shadow-sm text-center">
      <div class="text-muted small">Drivers</div><div class="num">{{ drv }}</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card p-3 shadow-sm text-center">
      <div class="text-muted small">Shipments</div><div class="num">{{ shp }}</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card p-3 shadow-sm text-center">
      <div class="text-muted small">With photo</div><div class="num text-success">{{ with_photo }}</div>
    </div></div>
  </div>

  <div class="row g-3 mb-4">
    <div class="col-md-6"><div class="card p-3 shadow-sm">
      <h6 class="text-muted mb-3">SHIPMENTS BY STATUS</h6>
      <table class="table table-sm mb-0">
        <thead><tr><th>Status</th><th>Count</th></tr></thead>
        <tbody>
        {% for status, n in by_status %}
          <tr><td>{{ status }}</td><td>{{ n }}</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div></div>
    <div class="col-md-6"><div class="card p-3 shadow-sm">
      <h6 class="text-muted mb-3">TOP DRIVERS</h6>
      <table class="table table-sm mb-0">
        <thead><tr><th>Driver</th><th>Shipments</th></tr></thead>
        <tbody>
        {% for name, n in top_drivers %}
          <tr><td>{{ name }}</td><td>{{ n }}</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div></div>
  </div>

  <p class="text-muted small text-center">Instance: <code>{{ host }}</code></p>
</div>
</body></html>
"""


@app.route("/dashboard")
def dashboard():
    # Resolve current user from session token (best-effort, no hard auth).
    current_user = None
    is_admin = is_driver = False
    token = session.get("id_token")
    if token:
        try:
            claims = _verify_token(token)
            current_user = claims.get("email") or claims.get("cognito:username")
            groups = set(claims.get("cognito:groups") or [])
            is_admin  = "Admins"  in groups
            is_driver = "Drivers" in groups or is_admin
        except JWTError:
            session.pop("id_token", None)

    cust = db.session.query(func.count(Customer.customer_id)).scalar()
    drv  = db.session.query(func.count(Driver.driver_id)).scalar()
    shp  = db.session.query(func.count(Shipment.shipment_id)).scalar()

    with_photo = (
        db.session.query(func.count(Shipment.shipment_id))
        .filter(Shipment.proof_photo_key.isnot(None))
        .scalar()
    )
    by_status = (
        db.session.query(Shipment.status, func.count(Shipment.shipment_id))
        .group_by(Shipment.status)
        .order_by(Shipment.status)
        .all()
    )
    top_drivers = (
        db.session.query(Driver.full_name, func.count(Shipment.shipment_id))
        .join(Shipment, Shipment.driver_id == Driver.driver_id)
        .group_by(Driver.full_name)
        .order_by(func.count(Shipment.shipment_id).desc())
        .limit(5)
        .all()
    )

    return render_template_string(
        _DASHBOARD_HTML,
        cust=cust, drv=drv, shp=shp,
        with_photo=with_photo,
        by_status=by_status,
        top_drivers=top_drivers,
        current_user=current_user,
        is_admin=is_admin,
        is_driver=is_driver,
        host=os.uname().nodename,
    )


# --------------------------------------------------------------------------- #
# 12. Plumbing                                                                  #
# --------------------------------------------------------------------------- #

@app.route("/")
def root():
    return redirect(url_for("dashboard"))


@app.route("/health")
def health():
    """
    ALB target-group health check.
    Returns 200 only when the DB is reachable so the ASG replaces
    an unhealthy instance automatically.
    """
    try:
        db.session.execute(db.text("SELECT 1"))
        return "ok", 200
    except Exception as exc:
        log.warning("health check failed: %s", exc)
        return "db unreachable", 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
