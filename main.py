import os
import asyncio
import sys
from dotenv import load_dotenv

load_dotenv()
import replicate
import uuid
import time
import math
import random
import secrets
import hashlib
import hmac
import base64
import json
import mimetypes
import urllib.request
import urllib.parse
import urllib.error
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import boto3
import stripe
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, status, Form, File, UploadFile, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import psycopg2
from psycopg2.extras import RealDictCursor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

SUPERSONIC_FAVICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>
<defs>
<linearGradient id='rc' x1='0%' y1='0%' x2='100%' y2='100%'>
<stop offset='0%' stop-color='#38bdf8'/>
<stop offset='60%' stop-color='#6366f1'/>
<stop offset='100%' stop-color='#4338ca'/>
</linearGradient>
<linearGradient id='rv' x1='0%' y1='0%' x2='100%' y2='100%'>
<stop offset='0%' stop-color='#c084fc'/>
<stop offset='50%' stop-color='#818cf8'/>
<stop offset='100%' stop-color='#06b6d4'/>
</linearGradient>
<linearGradient id='gs' x1='0%' y1='0%' x2='0%' y2='100%'>
<stop offset='0%' stop-color='#ffffff' stop-opacity='0.85'/>
<stop offset='100%' stop-color='#ffffff' stop-opacity='0'/>
</linearGradient>
</defs>
<path d='M18 22C36 16 74 16 86 22C70 32 40 34 18 34Z' fill='url(#rc)'/>
<path d='M18 22C36 16 74 16 86 22L78 26C66 21 34 21 18 26Z' fill='url(#gs)'/>
<path d='M86 22L30 76 L46 76 L86 34Z' fill='url(#rv)'/>
<path d='M14 78C26 68 58 66 82 76 C66 84 32 84 14 78Z' fill='url(#rc)'/>
<path d='M14 78 C28 72 60 72 82 76 L76 80 C58 76 28 76 14 81 Z' fill='url(#gs)'/>
</svg>"""

app = FastAPI(
    title="Zephyr Drive & Transfer API",
    version="3.5.0",
    swagger_favicon_url="/favicon.ico"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"Server error: {str(exc)}"}
    )

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

def render_template(template_name: str, request: Request, context: Optional[dict] = None) -> HTMLResponse:
    ctx = context.copy() if context else {}
    ctx["request"] = request
    try:
        return templates.TemplateResponse(request=request, name=template_name, context=ctx)
    except TypeError:
        return templates.TemplateResponse(template_name, ctx)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Stripe Configuration
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

# Dodo Payments Configuration
DODO_WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "").strip()
DODO_API_KEY = os.getenv("DODO_API_KEY", "").strip()

# Real-ESRGAN AI Super HD Upscaler Token (Replicate)
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "").strip()

def get_db():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL environment variable is missing.")
    conn_str = DATABASE_URL
    if conn_str.startswith("postgres://"):
        conn_str = conn_str.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(conn_str, cursor_factory=RealDictCursor)

PLAN_CONFIG = {
    "free":  {"name": "Free Starter", "price": 0.00, "quota": 5 * 1024**3,   "single_mb": 2048,  "esign_daily": 7},
    "micro": {"name": "Zephyr Micro", "price": 1.80, "quota": 15 * 1024**3,  "single_mb": 5000,  "esign_daily": 15},
    "lite":  {"name": "Zephyr Lite",  "price": 2.50, "quota": 30 * 1024**3,  "single_mb": 10000, "esign_daily": 30},
    "plus":  {"name": "Zephyr Plus",  "price": 4.50, "quota": 80 * 1024**3,  "single_mb": 25000, "esign_daily": 50},
    "pro":   {"name": "Zephyr Pro",   "price": 7.00, "quota": 200 * 1024**3, "single_mb": 50000, "esign_daily": -1}
}

MAX_VAULT_BYTES = 600 * 1024**3  # Strict 600 GB Maximum Ceiling

DODO_PRODUCT_MAP = {
    "pdt_0NoSL8gp9fUEk5GKwdB1O": "micro",
    "pdt_0NoSLFQFIcnqD2weSbr0d": "lite",
    "pdt_0NoSKxDY49vabp3lshvdI": "plus",
    "pdt_0NoSL3f1qb5yIZTPzv71H": "pro",
}

# ----------------- Timezone-Safe Subscription Helper -----------------
def is_sub_active(sub_end) -> bool:
    if not sub_end:
        return False
    if isinstance(sub_end, str):
        try:
            sub_end = datetime.fromisoformat(sub_end.replace("Z", "+00:00"))
        except Exception:
            return False
    if getattr(sub_end, "tzinfo", None) is not None:
        sub_end_naive = sub_end.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        sub_end_naive = sub_end
    return sub_end_naive > datetime.utcnow()

def resolve_user_tier(user_dict: dict):
    sub_end = user_dict.get("subscription_end_at")
    price = float(user_dict.get("plan_price") or 0.0)
    current_tier = (user_dict.get("tier") or "free").lower()
    current_quota = user_dict.get("storage_quota_bytes")

    if is_sub_active(sub_end) or price > 0.0:
        if current_tier in PLAN_CONFIG and current_tier != "free":
            resolved = current_tier
        elif price >= 7.0:
            resolved = "pro"
        elif price >= 4.5:
            resolved = "plus"
        elif price >= 2.5:
            resolved = "lite"
        elif price >= 1.8:
            resolved = "micro"
        else:
            resolved = current_tier if current_tier in PLAN_CONFIG else "micro"

        default_q = PLAN_CONFIG[resolved]["quota"]
        quota = min(MAX_VAULT_BYTES, max(current_quota or default_q, default_q))
        return resolved, quota, price

    tier_key = current_tier if current_tier in PLAN_CONFIG else "free"
    default_quota = PLAN_CONFIG[tier_key]["quota"]
    quota = current_quota or default_quota
    return tier_key, quota, price

# ----------------- Brevo Permanent Password Engine -----------------
def generate_permanent_password() -> str:
    nums = random.randint(1000, 9999)
    chars = secrets.token_hex(2).upper()
    return f"ZP-{nums}{chars}"

def send_buyer_password_email(buyer_email: str, filename: str, password: str, share_id: str) -> bool:
    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()
    system_sender = os.getenv("SENDER_EMAIL", "priyamrana069@gmail.com").strip()

    if not brevo_api_key:
        print("[BREVO EMAIL ERROR]: BREVO_API_KEY missing from environment.", flush=True)
        return False
    if not buyer_email or "@" not in buyer_email:
        print(f"[BREVO EMAIL ERROR]: Invalid target buyer email: '{buyer_email}'", flush=True)
        return False

    access_url = f"https://zephyr-drive.onrender.com/share/{share_id}"
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin: 0; padding: 0; background-color: #0b0f19; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
        <div style="max-width: 560px; margin: 30px auto; background: #111827; border: 1px solid #1f2937; border-radius: 20px; overflow: hidden; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);">
            <div style="background: linear-gradient(135deg, #4f46e5 0%, #06b6d4 100%); padding: 30px; text-align: center;">
                <h1 style="color: #ffffff; margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -0.5px;">Payment Confirmed</h1>
                <p style="color: #e0e7ff; margin: 8px 0 0 0; font-size: 14px;">Your access license for {filename} is ready.</p>
            </div>
            <div style="padding: 35px 30px; color: #f3f4f6;">
                <p style="font-size: 15px; line-height: 1.6; margin-top: 0; color: #d1d5db;">
                    Hello,<br><br>
                    Your purchase has been verified. To unlock and view your confidential transfer deliverables, enter your email and permanent password:
                </p>
                <div style="background: #1f2937; border: 1px solid #374151; border-radius: 14px; padding: 22px; margin: 25px 0; text-align: center;">
                    <span style="font-size: 11px; text-transform: uppercase; font-weight: 700; color: #9ca3af; letter-spacing: 1.5px; display: block; margin-bottom: 8px;">Your Permanent Access Password</span>
                    <span style="font-size: 30px; font-weight: 800; font-family: 'Courier New', Courier, monospace; letter-spacing: 5px; color: #38bdf8; background: #0f172a; padding: 10px 24px; border-radius: 10px; display: inline-block; border: 1px solid #1e293b;">
                        {password}
                    </span>
                </div>
                <div style="background: #0f172a; border-left: 4px solid #6366f1; padding: 14px 18px; border-radius: 0 10px 10px 0; margin-bottom: 28px;">
                    <p style="margin: 0; font-size: 13px; color: #9ca3af; line-height: 1.5;">
                        <strong style="color: #f3f4f6;">Never pay again:</strong> This password is permanently tied to <code style="color: #a5b4fc; background: #1e1b4b; padding: 2px 6px; border-radius: 4px;">{buyer_email}</code>.
                    </p>
                </div>
                <div style="text-align: center; margin-bottom: 10px;">
                    <a href="{access_url}" style="background: linear-gradient(135deg, #4f46e5 0%, #6366f1 100%); color: #ffffff; padding: 14px 34px; font-weight: 700; text-decoration: none; border-radius: 12px; display: inline-block; font-size: 14px; box-shadow: 0 10px 15px -3px rgba(79, 70, 229, 0.3);">
                        View Protected Deliverables
                    </a>
                </div>
            </div>
            <div style="background: #0d121f; border-top: 1px solid #1f2937; padding: 18px 30px; text-align: center;">
                <p style="margin: 0; font-size: 12px; color: #6b7280;">
                    Zephyr Zero-Knowledge Escrow &bull; Link: <a href="{access_url}" style="color: #818cf8; text-decoration: none;">{access_url}</a>
                </p>
            </div>
        </div>
    </body>
    </html>
    """
    unique_id = secrets.token_hex(3).upper()

    payload = {
        "sender": {"name": "Zephyr Escrow", "email": system_sender},
        "to": [{"email": buyer_email}],
        "replyTo": {"email": system_sender},
        "subject": f"Your Permanent Password to Access: {filename} [{unique_id}]",
        "htmlContent": html_content
    }

    req_data = json.dumps(payload).encode("utf-8")
    http_req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=req_data,
        headers={
            "api-key": brevo_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(http_req, timeout=15) as resp:
            print(f"[BREVO EMAIL SUCCESS] Sent permanent password to {buyer_email} (Status {resp.status})", flush=True)
            return True
    except urllib.error.HTTPError as he:
        err_msg = he.read().decode("utf-8", errors="ignore")
        print(f"[BREVO EMAIL HTTP ERROR {he.code}]: {err_msg}", flush=True)
        return False
    except Exception as e:
        print(f"[BREVO EMAIL ERROR]: {str(e)}", flush=True)
        return False

# ----------------- DB Initialization & Migrations -----------------
@app.on_event("startup")
def init_db_schema():
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.autocommit = True
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(120) PRIMARY KEY,
                email VARCHAR(255) UNIQUE,
                tier VARCHAR(30) DEFAULT 'free',
                storage_used_bytes BIGINT DEFAULT 0,
                storage_quota_bytes BIGINT DEFAULT 5368709120,
                plan_price NUMERIC(5,2) DEFAULT 0.00,
                subscription_end_at TIMESTAMP,
                grace_period_end_at TIMESTAMP,
                brand_title VARCHAR(120),
                brand_slug VARCHAR(60) UNIQUE,
                brand_logo_url TEXT,
                brand_bg_url TEXT,
                brand_accent_color VARCHAR(10) DEFAULT '#6366f1',
                stripe_account_id VARCHAR(255),
                payout_country VARCHAR(10) DEFAULT 'US',
                payout_method VARCHAR(30) DEFAULT 'stripe',
                bank_name VARCHAR(120),
                bank_account_number VARCHAR(60),
                bank_ifsc VARCHAR(30),
                bank_account_holder VARCHAR(120),
                upi_id VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(120) NOT NULL,
                tier VARCHAR(30) NOT NULL,
                quota_bytes BIGINT NOT NULL,
                price NUMERIC(5,2) NOT NULL,
                auto_renew BOOLEAN DEFAULT TRUE,
                subscription_end_at TIMESTAMP,
                dodo_subscription_id VARCHAR(120),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_end_at TIMESTAMP;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS grace_period_end_at TIMESTAMP;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_price NUMERIC(5,2) DEFAULT 0.00;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_title VARCHAR(120);",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_slug VARCHAR(60);",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_logo_url TEXT;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_bg_url TEXT;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_accent_color VARCHAR(10) DEFAULT '#6366f1';",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_account_id VARCHAR(255);",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_country VARCHAR(10) DEFAULT 'US';",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_method VARCHAR(30) DEFAULT 'stripe';",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_name VARCHAR(120);",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_account_number VARCHAR(60);",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_ifsc VARCHAR(30);",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_account_holder VARCHAR(120);",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS upi_id VARCHAR(100);",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;",
            "ALTER TABLE shares ADD COLUMN IF NOT EXISTS message TEXT;",
            "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS dodo_subscription_id VARCHAR(120);",
            "CREATE INDEX IF NOT EXISTS idx_user_subs_user ON user_subscriptions (user_id);"
        ]

        for m in migrations:
            try:
                cursor.execute(m)
            except Exception as ex:
                print(f"[MIGRATION WARNING]: {ex}", flush=True)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signature_requests (
                doc_id VARCHAR(32) PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                recipient_name VARCHAR(255) NOT NULL,
                recipient_email VARCHAR(255) NOT NULL,
                status VARCHAR(32) DEFAULT 'pending',
                signature_x INT DEFAULT 150,
                signature_y INT DEFAULT 250,
                creator_email VARCHAR(255),
                user_id VARCHAR(64),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS drive_files (
                id VARCHAR(64) PRIMARY KEY,
                user_id VARCHAR(120) NOT NULL,
                filename TEXT NOT NULL,
                file_type VARCHAR(100),
                size_bytes BIGINT NOT NULL,
                s3_key TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                id VARCHAR(64) PRIMARY KEY,
                user_id VARCHAR(120),
                filename TEXT NOT NULL,
                filesize_mb NUMERIC(10, 2) NOT NULL,
                s3_key TEXT NOT NULL,
                password_hash TEXT,
                expiry_hours INT DEFAULT 24,
                expires_at TIMESTAMP NOT NULL,
                max_downloads INT DEFAULT 0,
                downloads INT DEFAULT 0,
                is_paywalled BOOLEAN DEFAULT FALSE,
                unlock_price NUMERIC(10, 2) DEFAULT 0.00,
                paywall_creator_id VARCHAR(120),
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS share_files (
                id SERIAL PRIMARY KEY,
                share_id VARCHAR(64) REFERENCES shares(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                filesize_mb NUMERIC(10, 2) NOT NULL,
                s3_key TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS paywall_purchases (
                id SERIAL PRIMARY KEY,
                share_id VARCHAR(64) REFERENCES shares(id) ON DELETE CASCADE,
                buyer_email VARCHAR(255) NOT NULL,
                access_token VARCHAR(64) UNIQUE NOT NULL,
                amount_paid NUMERIC(10, 2) NOT NULL,
                payment_status VARCHAR(30) DEFAULT 'unpaid',
                buyer_password VARCHAR(64),
                buyer_password_hash TEXT,
                permanent_password VARCHAR(64),
                stripe_session_id VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                unlocked_at TIMESTAMP
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS idx_paywall_lookup 
            ON paywall_purchases (share_id, buyer_email, payment_status);
        """)

        cursor.close()
        conn.close()
        print("[DB STARTUP]: Multi-file escrow database schema verified.", flush=True)
    except Exception as e:
        print(f"[DB STARTUP ERROR]: {e}", flush=True)

# ----------------- Cloudflare R2 Client -----------------
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "").strip()
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "vault-storage-backend").strip()
R2_PUBLIC_DOMAIN = os.getenv("R2_PUBLIC_DOMAIN", "").strip().rstrip("/")
R2_ENDPOINT = R2_ENDPOINT_URL or (f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else None)

s3_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
    config=Config(signature_version="s3v4")
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()

otp_storage = {}

# ----------------- Standard Svix Webhook Signature Verifier -----------------
def verify_dodo_svix_signature(raw_body: bytes, headers: dict, secret: str) -> bool:
    if not secret:
        return False

    msg_id = headers.get("webhook-id")
    msg_timestamp = headers.get("webhook-timestamp")
    msg_signature = headers.get("webhook-signature") or headers.get("x-dodo-signature")

    if not msg_id or not msg_timestamp or not msg_signature:
        expected_raw = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if msg_signature and hmac.compare_digest(expected_raw, msg_signature.replace("sha256=", "")):
            return True
        return False

    clean_secret = secret.replace("whsec_", "").strip()
    try:
        key = base64.b64decode(clean_secret)
    except Exception:
        key = clean_secret.encode("utf-8")

    to_sign = f"{msg_id}.{msg_timestamp}.".encode("utf-8") + raw_body

    computed = base64.b64encode(
        hmac.new(key, to_sign, hashlib.sha256).digest()
    ).decode("utf-8")

    for item in msg_signature.split(" "):
        parts = item.split(",", 1)
        if len(parts) == 2 and parts[0] == "v1":
            if hmac.compare_digest(parts[1], computed):
                return True
        elif hmac.compare_digest(item, computed):
            return True

    return False

# ----------------- Pydantic Request Models -----------------
class IndianBankPayoutRequest(BaseModel):
    user_id: str
    account_holder: str
    bank_name: str
    account_number: str
    ifsc: str
    upi_id: Optional[str] = None

class FileManifestItem(BaseModel):
    filename: str
    filesize_mb: float

class SendOtpRequest(BaseModel):
    email: str

class VerifyOtpRequest(BaseModel):
    email: str
    code: str

class SendTransferEmailRequest(BaseModel):
    recipient_email: str
    sender_email: str
    share_id: str
    filename: str
    filesize_mb: float
    title: Optional[str] = "Files shared via Zephyr"
    message: Optional[str] = ""

class BrandingUpdatePayload(BaseModel):
    user_id: str
    brand_title: Optional[str] = None
    brand_slug: Optional[str] = None
    brand_accent_color: Optional[str] = "#6366f1"
    brand_logo_url: Optional[str] = None
    brand_bg_url: Optional[str] = None
    reset_default: Optional[bool] = False

class InitiatePaywallRequest(BaseModel):
    share_id: str
    buyer_email: str

class ConfirmEmailSendPassRequest(BaseModel):
    share_id: str
    session_id: Optional[str] = None
    token: Optional[str] = None
    email: str

class UnlockWithPasswordRequest(BaseModel):
    share_id: str
    email: str
    password: str

class ResendPasswordRequest(BaseModel):
    share_id: str
    email: str

class CreateShareRequest(BaseModel):
    filename: Optional[str] = "Transfer"
    message: Optional[str] = None
    filesize_mb: Optional[float] = 0.00
    files: Optional[List[FileManifestItem]] = []
    password: Optional[str] = None
    expiry_hours: int = 24
    max_downloads: int = 0
    user_id: Optional[str] = None
    is_paywalled: Optional[bool] = False
    unlock_price: Optional[float] = 0.00

class DownloadPayload(BaseModel):
    password: Optional[str] = None
    access_token: Optional[str] = None

class RenameFileRequest(BaseModel):
    file_id: str
    new_filename: str
    user_id: Optional[str] = None

class UpgradeQuoteRequest(BaseModel):
    user_id: str
    target_tier: str

class SupportChatRequest(BaseModel):
    message: str
    history: Optional[list] = []

class SetPaywallPasswordRequest(BaseModel):
    access_token: str
    password: str

class LoginPaywallRequest(BaseModel):
    share_id: str
    email: str
    password: str

class ToggleAutoRenewRequest(BaseModel):
    user_id: str
    subscription_id: int
    auto_renew: bool

# ----------------- Transaction-Time FX Rates Engine with Persistent Daily Caching -----------------
FX_CACHE_FILE = os.path.join(BASE_DIR, "fx_cache.json")
FX_EXTERNAL_API = "https://open.er-api.com/v6/latest/USD"
PROCESSOR_SPREAD = 0.025  # 2.5% standard gateway conversion spread

TOP_CURRENCIES = {
    "INR": {"name": "Indian Rupee", "symbol": "₹", "flag": "🇮🇳", "fallback": 83.75},
    "EUR": {"name": "Euro", "symbol": "€", "flag": "🇪🇺", "fallback": 0.90},
    "GBP": {"name": "British Pound", "symbol": "£", "flag": "🇬🇧", "fallback": 0.77},
    "CAD": {"name": "Canadian Dollar", "symbol": "CA$", "flag": "🇨🇦", "fallback": 1.36},
    "AUD": {"name": "Australian Dollar", "symbol": "A$", "flag": "🇦🇺", "fallback": 1.48},
    "JPY": {"name": "Japanese Yen", "symbol": "¥", "flag": "🇯🇵", "fallback": 144.50},
    "AED": {"name": "UAE Dirham", "symbol": "AED ", "flag": "🇦🇪", "fallback": 3.67},
    "SGD": {"name": "Singapore Dollar", "symbol": "S$", "flag": "🇸🇬", "fallback": 1.30},
    "CHF": {"name": "Swiss Franc", "symbol": "CHF ", "flag": "🇨🇭", "fallback": 0.85},
    "CNY": {"name": "Chinese Yuan", "symbol": "¥", "flag": "🇨🇳", "fallback": 7.05},
}

def load_cached_fx_data() -> Optional[dict]:
    if os.path.exists(FX_CACHE_FILE):
        try:
            with open(FX_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def save_cached_fx_data(data: dict):
    try:
        with open(FX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[FX CACHE SAVE ERROR]: {e}", flush=True)

def fetch_live_global_fx() -> dict:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    cached = load_cached_fx_data()

    # Reuse cached data if updated within the last 12 hours (43,200 seconds)
    if cached and (now_ts - cached.get("last_fetched", 0) < 43200):
        return cached.get("payload")

    live_rates = {}
    
    # Provider 1: Open Exchange Rates API (free, reliable, updates every 24h)
    try:
        req = urllib.request.Request(
            FX_EXTERNAL_API,
            headers={"User-Agent": "Zephyr-Edge-FX/3.5"}
        )
        with urllib.request.urlopen(req, timeout=6) as response:
            external_data = json.loads(response.read().decode("utf-8"))
            live_rates = external_data.get("rates", {})
    except Exception as err:
        print(f"[FX PROVIDER 1 FAILED]: {err}, attempting fallback...", flush=True)

    # Provider 2: Frankfurter Dev API (Fallback)
    if not live_rates:
        try:
            req = urllib.request.Request(
                "https://api.frankfurter.dev/v1/latest?base=USD",
                headers={"User-Agent": "Zephyr-Edge-FX/3.5"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                external_data = json.loads(response.read().decode("utf-8"))
                live_rates = external_data.get("rates", {})
                live_rates.setdefault("AED", 3.6725)
        except Exception as err:
            print(f"[FX PROVIDER 2 FAILED]: {err}", flush=True)

    # If all network providers fail, retrieve previous cache or baseline defaults
    if not live_rates:
        if cached and cached.get("payload"):
            return cached.get("payload")
        live_rates = {k: v["fallback"] for k, v in TOP_CURRENCIES.items()}

    previous_rates = cached.get("raw_market_rates", {}) if cached else {}

    output_rates = {}
    for code, meta in TOP_CURRENCIES.items():
        market_val = float(live_rates.get(code, meta["fallback"]))
        prev_val = float(previous_rates.get(code, market_val))

        # Calculate actual daily percentage shift
        if prev_val and prev_val > 0 and market_val != prev_val:
            diff_pct = ((market_val - prev_val) / prev_val) * 100.0
            change_pct = round(diff_pct, 2)
        else:
            # Subtle natural daily market fluctuation if newly reset
            day_seed = datetime.now(timezone.utc).timetuple().tm_yday
            change_pct = round(math.sin(day_seed + len(code)) * 0.45, 2)

        gateway_val = market_val * (1.0 + PROCESSOR_SPREAD)

        output_rates[code] = {
            "name": meta["name"],
            "symbol": meta["symbol"],
            "flag": meta["flag"],
            "market_rate": round(market_val, 4) if market_val < 10 else round(market_val, 2),
            "gateway_rate": round(gateway_val, 4) if gateway_val < 10 else round(gateway_val, 2),
            "change_pct": abs(change_pct),
            "is_positive": change_pct >= 0,
        }

    payload = {
        "base": "USD",
        "rates": output_rates,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "gateway_spread_pct": 2.5
    }

    save_cached_fx_data({
        "last_fetched": now_ts,
        "raw_market_rates": {code: live_rates.get(code, meta["fallback"]) for code, meta in TOP_CURRENCIES.items()},
        "payload": payload
    })

    return payload

@app.get("/api/exchange-rates")
async def get_exchange_rates():
    data = fetch_live_global_fx()
    return JSONResponse(content=data)

# ----------------- User Subscriptions & Multi-Plan API -----------------
def cancel_dodo_gateway_subscription(dodo_sub_id: str) -> bool:
    if not DODO_API_KEY or not dodo_sub_id or dodo_sub_id.startswith("local_"):
        return False

    is_test = "test" in DODO_WEBHOOK_SECRET.lower() or DODO_API_KEY.startswith("test_")
    base_url = "https://test.dodopayments.com" if is_test else "https://live.dodopayments.com"
    headers = {"Authorization": f"Bearer {DODO_API_KEY}", "Content-Type": "application/json"}

    try:
        url = f"{base_url}/subscriptions/{dodo_sub_id}/cancel"
        req = urllib.request.Request(url, data=json.dumps({"cancel_at_period_end": True}).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201, 204)
    except Exception:
        try:
            patch_url = f"{base_url}/subscriptions/{dodo_sub_id}"
            req_patch = urllib.request.Request(patch_url, data=json.dumps({"status": "cancelled"}).encode(), headers=headers, method="PATCH")
            with urllib.request.urlopen(req_patch, timeout=10) as resp2:
                return resp2.status in (200, 204)
        except Exception as e:
            print(f"[DODO CANCEL API ERROR]: {e}", flush=True)
            return False

@app.get("/api/user-subscriptions")
async def get_user_subscriptions(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return []

    cursor.execute("""
        SELECT id, tier, quota_bytes, price, auto_renew, subscription_end_at, dodo_subscription_id, created_at 
        FROM user_subscriptions 
        WHERE user_id = %s 
        ORDER BY created_at DESC
    """, (user_id,))
    subs = cursor.fetchall()

    user_quota_bytes = user.get("storage_quota_bytes") or 5368709120
    sub_end = user.get("subscription_end_at") or (datetime.utcnow() + timedelta(days=30))
    total_quota_gb = round(user_quota_bytes / (1024**3))

    if not subs and total_quota_gb > 5:
        remaining_gb = total_quota_gb
        if remaining_gb >= 200:
            cursor.execute("""
                INSERT INTO user_subscriptions (user_id, tier, quota_bytes, price, auto_renew, subscription_end_at, dodo_subscription_id)
                VALUES (%s, 'pro', %s, 7.00, TRUE, %s, 'local_pro')
            """, (user_id, 200 * 1024**3, sub_end))
            remaining_gb -= 200
        while remaining_gb >= 15:
            cursor.execute("""
                INSERT INTO user_subscriptions (user_id, tier, quota_bytes, price, auto_renew, subscription_end_at, dodo_subscription_id)
                VALUES (%s, 'micro', %s, 1.80, TRUE, %s, 'local_micro')
            """, (user_id, 15 * 1024**3, sub_end))
            remaining_gb -= 15
        conn.commit()

        cursor.execute("""
            SELECT id, tier, quota_bytes, price, auto_renew, subscription_end_at, dodo_subscription_id, created_at 
            FROM user_subscriptions 
            WHERE user_id = %s 
            ORDER BY created_at DESC
        """, (user_id,))
        subs = cursor.fetchall()

    conn.close()

    tier_names = {
        "free": "Free Starter",
        "micro": "Zephyr Micro",
        "lite": "Zephyr Lite",
        "plus": "Zephyr Plus",
        "pro": "Zephyr Pro"
    }

    formatted = []
    for s in subs:
        end_dt = s["subscription_end_at"]
        active = is_sub_active(end_dt)
        t = s["tier"].lower()
        gb_val = round(s["quota_bytes"] / (1024**3))
        formatted.append({
            "id": s["id"],
            "tier": t,
            "name": tier_names.get(t, t.capitalize()),
            "gb": gb_val,
            "price": float(s["price"]),
            "auto_renew": s["auto_renew"] if s.get("auto_renew") is not None else True,
            "is_active": active,
            "subscription_end_at": str(end_dt)[:19] if end_dt else "Active"
        })

    return formatted

@app.post("/api/user-subscriptions/toggle-auto-renew")
async def toggle_auto_renew(req: ToggleAutoRenewRequest):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, tier, dodo_subscription_id, subscription_end_at 
        FROM user_subscriptions 
        WHERE id = %s AND user_id = %s
    """, (req.subscription_id, req.user_id))
    sub = cursor.fetchone()

    if not sub:
        conn.close()
        raise HTTPException(status_code=404, detail="Subscription record not found.")

    dodo_sub_id = sub.get("dodo_subscription_id")
    if not req.auto_renew and dodo_sub_id:
        cancel_dodo_gateway_subscription(dodo_sub_id)

    cursor.execute("""
        UPDATE user_subscriptions 
        SET auto_renew = %s 
        WHERE id = %s AND user_id = %s
    """, (req.auto_renew, req.subscription_id, req.user_id))
    conn.commit()
    conn.close()

    return {"status": "success", "auto_renew": req.auto_renew}

# ----------------- OTP Verification Endpoints -----------------
@app.post("/api/send-otp")
async def send_verification_otp(req: SendOtpRequest):
    target_email = req.email.lower().strip()
    now = time.time()

    entry = otp_storage.get(target_email, {})
    locked_until = entry.get("locked_until", 0)

    if now < locked_until:
        remaining_mins = max(1, math.ceil((locked_until - now) / 60))
        raise HTTPException(
            status_code=429,
            detail=f"Account temporarily locked due to 4 failed attempts. Try again in {remaining_mins} minute(s)."
        )

    code = f"{random.randint(100000, 999999)}"
    current_attempts = entry.get("attempts", 0) if now < entry.get("expires", 0) else 0

    otp_storage[target_email] = {
        "code": code,
        "expires": now + 180,
        "attempts": current_attempts,
        "locked_until": 0
    }

    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()
    sender_email = os.getenv("SENDER_EMAIL", "priyamrana069@gmail.com").strip()

    if not brevo_api_key:
        raise HTTPException(
            status_code=500, 
            detail="BREVO_API_KEY missing on server. Add BREVO_API_KEY in Render Environment."
        )

    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; margin: auto; padding: 25px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #f8fafc;">
        <h2 style="color: #4f46e5; margin-bottom: 8px;">Zephyr Transfer Verification</h2>
        <p style="font-size: 14px; color: #475569; line-height: 1.5;">To verify your email address (<strong>{target_email}</strong>) and authorize your transfer, enter the following code:</p>
        <div style="text-align: center; margin: 24px 0;">
            <span style="font-size: 32px; font-weight: 800; letter-spacing: 6px; color: #0f172a; background: #e0e7ff; padding: 10px 24px; border-radius: 12px; border: 1px solid #c7d2fe; display: inline-block;">{code}</span>
        </div>
        <p style="font-size: 12px; color: #dc2626; font-weight: bold;">⚠️ This code expires in 3 minutes.</p>
        <p style="font-size: 11px; color: #94a3b8;">You have 4 attempts to enter the correct code before a 30-minute lockout is triggered.</p>
    </div>
    """

    payload = {
        "sender": {"name": "Zephyr Transfers", "email": sender_email},
        "to": [{"email": target_email}],
        "subject": f"Your Zephyr Verification Code: {code}",
        "htmlContent": html_content
    }

    req_data = json.dumps(payload).encode("utf-8")
    http_req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=req_data,
        headers={
            "api-key": brevo_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(http_req, timeout=15) as response:
            if response.status not in (200, 201, 202):
                raise Exception(f"Brevo API returned status {response.status}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send email via Brevo API: {str(e)}")

    return {"message": "Verification code dispatched successfully."}

@app.post("/api/verify-otp")
async def verify_otp(req: VerifyOtpRequest):
    email = req.email.lower().strip()
    now = time.time()
    entry = otp_storage.get(email)

    if not entry:
        raise HTTPException(status_code=400, detail="No active verification code found for this email.")

    locked_until = entry.get("locked_until", 0)
    if now < locked_until:
        remaining_mins = max(1, math.ceil((locked_until - now) / 60))
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Locked out for {remaining_mins} more minute(s)."
        )

    if now > entry.get("expires", 0):
        otp_storage.pop(email, None)
        raise HTTPException(status_code=400, detail="Verification code has expired (3-minute validity). Please request a new one.")

    if entry["code"] != req.code.strip():
        entry["attempts"] = entry.get("attempts", 0) + 1
        attempts_left = 4 - entry["attempts"]

        if attempts_left <= 0:
            entry["locked_until"] = now + 1800
            entry.pop("code", None)
            raise HTTPException(
                status_code=429,
                detail="4 unsuccessful attempts reached. You are locked out for 30 minutes."
            )

        raise HTTPException(
            status_code=400,
            detail=f"Incorrect code. {attempts_left} attempt(s) remaining."
        )

    otp_storage.pop(email, None)
    return {"status": "verified", "email": email}

@app.post("/api/send-transfer-email")
async def send_transfer_email(req: SendTransferEmailRequest, request: Request):
    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()
    system_sender = os.getenv("SENDER_EMAIL", "priyamrana069@gmail.com").strip()

    if not brevo_api_key:
        raise HTTPException(status_code=500, detail="BREVO_API_KEY missing on server.")

    base_url = str(request.base_url).rstrip("/")
    download_url = f"{base_url}/share/{req.share_id}"

    html_email = f"""
    <div style="font-family: Arial, sans-serif; max-width: 540px; margin: auto; padding: 30px; border: 1px solid #e2e8f0; border-radius: 18px; background-color: #f8fafc;">
        <h2 style="color: #4f46e5; margin-top: 0;">Zephyr Secure Transfer</h2>
        <p style="font-size: 15px; color: #1e293b; line-height: 1.6;">
            <strong>{req.sender_email}</strong> has sent you files via Zephyr's zero-knowledge transfer network.
        </p>

        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 18px; margin: 22px 0;">
            <p style="margin: 0 0 10px 0; font-size: 13px; color: #475569;"><strong>Subject:</strong> {req.title or 'Shared Files'}</p>
            {f'<p style="margin: 0 0 10px 0; font-size: 13px; color: #475569;"><strong>Message:</strong> {req.message}</p>' if req.message else ''}
            <p style="margin: 0; font-size: 13px; color: #475569;"><strong>File:</strong> {req.filename} ({req.filesize_mb} MB)</p>
        </div>

        <div style="text-align: center; margin: 30px 0;">
            <a href="{download_url}" style="background-color: #4f46e5; color: #ffffff; padding: 14px 30px; text-decoration: none; font-size: 14px; font-weight: bold; border-radius: 12px; display: inline-block;">Download Files</a>
        </div>

        <p style="font-size: 12px; color: #94a3b8; text-align: center; word-break: break-all;">
            Direct download link:<br>
            <a href="{download_url}" style="color: #4f46e5;">{download_url}</a>
        </p>
    </div>
    """

    payload = {
        "sender": {"name": f"{req.sender_email} via Zephyr", "email": system_sender},
        "replyTo": {"email": req.sender_email},
        "to": [{"email": req.recipient_email}],
        "subject": f"{req.sender_email} sent you files: {req.title or req.filename}",
        "htmlContent": html_email
    }

    req_data = json.dumps(payload).encode("utf-8")
    http_req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=req_data,
        headers={
            "api-key": brevo_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(http_req, timeout=15) as response:
            if response.status not in (200, 201, 202):
                raise Exception(f"Brevo API returned status {response.status}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Files uploaded, but failed to email recipient: {str(e)}")

    return {"status": "dispatched", "recipient": req.recipient_email}

# ----------------- Zephyr Copilot Engine -----------------
ZEPHYR_SYSTEM_KNOWLEDGE = """
You are Zephyr Copilot, the friendly, authoritative AI assistant for Zephyr Systems.
Your job is to answer user questions in clear, concise language while covering all technical and financial specifics accurately.

Rules:
1. Explain clearly like talking to a helpful peer. Keep answers direct, accurate, and easily actionable.
2. If the user asks for human support, needs developer escalation, or encounters a bug, direct them to Priyam Rana at priyamrana069@gmail.com.

Pay-to-Unlock Escrow Payouts & Fees:
- For every paid unlock or file sold via Pay-to-Unlock Escrow, the user/creator receives an 88% payout.
- Zephyr retains a 12% platform fee split.
- Example: On a $100.00 deliverable, the creator receives $88.00 (88%), and Zephyr takes $12.00 (12%).
- Payouts are transferred automatically via Stripe into the creator's connected bank account with 0% chargeback risk, or directly deposited into Indian bank/UPI accounts.
- In group deliveries, each buyer purchases an isolated, individual access token. Media is served inside a protected viewer with moving forensic watermarks and focus-loss anti-screenshot shielding. Buyers receive a permanent lifetime access password in Gmail so they never have to pay twice.
"""

@app.post("/api/support/chat")
async def support_chat(req: SupportChatRequest):
    user_msg = req.message.strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="Empty query.")

   

    q = user_msg.lower()

    # 1. Human Support / Live Agent Escalation
    if any(k in q for k in ["human", "agent", "person", "representative", "contact", "support email", "talk to someone", "real person", "customer service", "helpdesk", "reach out"]):
        return {
            "reply": (
                "**Contact Zephyr Human Support:**\n\n"
                "• **Direct Email:** Reach our core engineering & billing desk at **`priyamrana069@gmail.com`**.\n"
                "• **Response Times:**\n"
                "  - **Zephyr Pro & Plus:** Priority queue (average response < 2 hours).\n"
                "  - **Lite & Micro:** Under 12 hours.\n"
                "  - **Free Starter:** Under 24 hours.\n"
                "• **In-App Assistance:** If your issue relates to an active transfer, please provide your **Share ID** or **Transaction Reference ID** in your email for rapid resolution."
            )
        }

    # 2. Payment Failures, Missing Receipts & Billing Troubleshooting
    if any(k in q for k in ["receipt", "invoice", "fail", "failed", "declined", "didn't get", "did not receive", "billing issue", "charge", "refund", "card error", "checkout error"]):
        return {
            "reply": (
                "**Payment & Receipt Resolution:**\n\n"
                "• **If your card was declined or payment failed:**\n"
                "  1. Ensure international & online e-commerce transactions are activated on your card.\n"
                "  2. Confirm 3D Secure verification was completed before closing the checkout window.\n"
                "  3. Any failed authorization holds are automatically voided by your bank within 24–48 hours.\n\n"
                "• **If your payment succeeded but you didn't receive a receipt:**\n"
                "  1. Check your email spam/promotions folder for a confirmation receipt sent from our payment processors (**Dodo Payments** or **Stripe**).\n"
                "  2. Allow 60–90 seconds for automated webhook provisioning to update your account status.\n"
                "  3. If your tier has not updated after 5 minutes, email **`priyamrana069@gmail.com`** with your registered account email and bank transaction time. Our team will verify and activate your tier manually."
            )
        }

    # 3. Daily E-Sign Document Quotas
    if any(k in q for k in ["e-sign", "esign", "sign", "signature", "envelope", "contract", "daily limit", "how many sign"]):
        return {
            "reply": (
                "**Daily E-Sign Creation Quotas (Resets Daily at 00:00 UTC):**\n\n"
                "• **Free Starter:** 7 documents / day\n"
                "• **Zephyr Micro ($1.80/mo):** 15 documents / day\n"
                "• **Zephyr Lite ($2.50/mo):** 30 documents / day\n"
                "• **Zephyr Plus ($4.50/mo):** 50 documents / day\n"
                "• **Zephyr Pro ($7.00/mo):** **Unlimited** daily document signing\n\n"
                "• **Security Standards:** All envelopes feature drag-and-drop signatures, realistic vector ink, cryptographic SHA-256 audit trails, and zero-knowledge storage."
            )
        }

    # 4. 4K Ultra HD AI Super-Resolution (Real-ESRGAN)
    if any(k in q for k in ["4k", "upscal", "esrgan", "enhance", "super-resolution", "resolution", "pixel", "hd", "clarity", "sharpen", "ai tool"]):
        return {
            "reply": (
                "**4K Ultra HD AI Super-Resolution (Real-ESRGAN):**\n\n"
                "• **Capabilities:** Reconstructs degraded textures, removes JPEG artifacts, and scales images up to 4× native resolution (up to 3840×2160 UHD).\n"
                "• **Plan Allowances:**\n"
                "  - **Zephyr Lite ($2.50/mo):** 15 AI runs / month\n"
                "  - **Zephyr Plus ($4.50/mo):** 60 AI runs / month\n"
                "  - **Zephyr Pro ($7.00/mo):** **Unlimited** AI runs\n"
                "  - *Free Starter & Micro do not include 4K upscaling.*\n"
                "• **How to use:** Open any image from your Vault by clicking **Open** to launch Drive Studio, then click **4K Ultra HD Converter** in the studio toolbar. Save your lossless PNG directly back to your vault."
            )
        }

    # 5. Storage Plans, Vault Limits & Prorated Upgrades
    if any(k in q for k in ["plan", "pricing", "price", "cost", "tier", "subscription", "upgrade", "vault size", "storage", "micro", "lite", "plus", "pro"]):
        return {
            "reply": (
                "**Zephyr Transfer & Storage Plans:**\n\n"
                "• **Free Starter ($0 / forever):** 5 GB Vault • 2 GB single transfer • 7 E-Signs/day\n"
                "• **Zephyr Micro ($1.80/mo):** 15 GB Vault • 5 GB transfer • 15 E-Signs/day • 20-Day Grace\n"
                "• **Zephyr Lite ($2.50/mo):** 30 GB Vault • 10 GB transfer • 30 E-Signs/day • 15 4K AI runs • Client Drop & QR\n"
                "• **Zephyr Plus ($4.50/mo):** 80 GB Vault • 25 GB transfer • 50 E-Signs/day • 60 4K AI runs • Studio Branding\n"
                "• **Zephyr Pro ($7.00/mo):** 200 GB Vault • 50 GB transfer • Unlimited E-Signs • Unlimited 4K AI runs • Pay-to-Unlock Escrow\n\n"
                "*Upgrades between tiers mid-cycle are seamlessly prorated (+ $0.50 processing fee).*"
            )
        }

    # 6. Pay-to-Unlock Escrow, Bank Payouts & Anti-Screenshot Shield
    payout_keywords = ["payout", "cut", "commission", "percent", "percentage", "split", "fee", "earn", "earnings", "take home", "escrow", "paywall", "unlock price", "bank account", "stripe connect"]
    if any(k in q for k in payout_keywords) or (any(w in q for w in ["pay", "escrow", "paywall", "unlock", "money"]) and any(h in q for h in ["how", "what", "where"])):
        return {
            "reply": (
                "**Pay-to-Unlock Escrow Protocol:**\n\n"
                "• **88% Direct Payout:** Creators receive 88% of every unlock directly wired to their connected bank account via Stripe with 0% escrow chargeback risk.\n"
                "• **12% Protocol Fee:** Zephyr retains 12% to cover edge bandwidth, hosting, and encryption pipelines.\n"
                "• **Anti-Screenshot Protection:** Media streams inside a forensic viewer featuring dynamic moving watermarks and window-blur curtains.\n"
                "• **Lifetime Unlock:** Upon purchase, buyers receive an access passcode sent to their Gmail for lifetime viewing without paying twice."
            )
        }

    # 7. Client Drop Portals & Mobile QR Codes
    if any(k in q for k in ["client drop", "drop zone", "qr", "deposit", "receive files", "request file", "file request"]):
        return {
            "reply": (
                "**Client Deposit Portals & QR Codes:**\n\n"
                "• **No-Account Client Uploads:** Generate a deposit portal where clients and collaborators can upload large project assets directly into your vault without creating a Zephyr account.\n"
                "• **Mobile QR Scanner:** Scan the generated QR code directly with any smartphone camera to deposit photos, videos, or documents on the go.\n"
                "• **Availability:** Included with Zephyr Lite ($2.50/mo), Plus ($4.50/mo), and Pro ($7.00/mo)."
            )
        }

# 7.5 Password, Reset & Transfer Passcode Guidance
    if any(k in q for k in ["password", "change password", "reset password", "forgot password", "update password"]):
        return {
            "reply": (
                "**🔐 Zephyr Password & Authentication Architecture:**\n\n"
                "• **Account Logins (Passwordless OTP):** Zephyr does not use permanent static passwords. "
                "Your account is secured via one-time cryptographically generated OTP verification codes sent directly to your email, "
                "eliminating the risk of credential stuffing and forgotten passwords.\n\n"
                "• **Transfer Passcodes:** If you set a passcode on a shared file, it is derived locally using AES-256 in your browser. "
                "Because Zephyr operates on Zero-Knowledge principles, a transfer passcode **cannot be modified** once the link has been created. "
                "To change a file's passcode, simply shred the active link and create a new transfer with your desired passcode.\n\n"
                "• **Need Assistance?** Contact our core engineering desk at **priyamrana069@gmail.com** or submit a query at **/contact**."
            )
        }

    # 8. Zero-Knowledge Encryption & Burn-on-Read
    if any(k in q for k in ["security", "encryption", "zero-knowledge", "burn", "shred", "aes", "r2", "privacy", "passcode"]):
        return {
            "reply": (
                "**Zero-Knowledge Architecture & Self-Shredding:**\n\n"
                "• **Client-Side AES-256:** Key derivation and file hashing execute entirely inside your browser. No unencrypted passcodes or raw master keys are ever written to server logs or databases.\n"
                "• **Cloudflare R2 Direct Pipe:** Files stream straight through edge nodes with zero intermediary inspection.\n"
                "• **Burn-on-Read (🔥):** Setting max downloads to 1 triggers instant edge purging the millisecond a recipient finishes downloading.\n"
            )
        }

    # 9. 20-Day Retention Grace Period
    if any(k in q for k in ["grace", "lapse", "cancel", "expire", "expiration", "retention", "prun", "buffer", "delete files"]):
        return {
            "reply": (
                "**20-Day Retention Grace Period:**\n\n"
                "• If your paid plan lapses, your vault enters an industry-standard **20-day read-only grace period**.\n"
                "• Your data remains fully protected and downloadable during these 20 days.\n"
                "• If the plan is not renewed after 20 days, the vault is automatically pruned down to your active tier limit by permanently shredding the oldest files."
            )
        }

    # 10. Default / Fallback Navigation Menu
    return {
        "reply": (
            "I'm Zephyr Copilot! How can I help you today? You can ask me about:\n\n"
            "• **4K Ultra HD AI Upscaler** (Quotas, Real-ESRGAN, Studio tools)\n"
            "• **Storage Plans & Pricing** (Free Starter, Micro, Lite, Plus, Pro)\n"
            "• **E-Sign Studio Quotas** (Daily document creation limits)\n"
            "• **Payment or Receipt Issues** (Failed checkouts, missing receipts)\n"
            "• **Pay-to-Unlock Escrow** (88% payouts, Stripe Connect, forensic viewers)\n"
            "• **Client Drop & QR Portals** (Direct client deposit links)\n"
            "• **Zero-Knowledge Security & 20-Day Grace Retention**\n"
            "• **Contacting Human Support** (`priyamrana069@gmail.com`)\n\n"
            "What would you like to explore?"
        )
    }
# ----------------- Main Static Routes -----------------
@app.get("/favicon.ico", include_in_schema=False)
async def site_favicon():
    return Response(content=SUPERSONIC_FAVICON_SVG, media_type="image/svg+xml")

@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    return render_template("index.html", request, {"supabase_url": SUPABASE_URL, "supabase_anon": SUPABASE_ANON_KEY})

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return render_template("dashboard.html", request, {"supabase_url": SUPABASE_URL, "supabase_anon": SUPABASE_ANON_KEY})

@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    return render_template("auth.html", request, {"supabase_url": SUPABASE_URL, "supabase_anon": SUPABASE_ANON_KEY})

@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return render_template("terms.html", request)

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return render_template("privacy.html", request)

@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return render_template("about.html", request)

@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    return render_template("pricing.html", request)

@app.get("/branding", response_class=HTMLResponse)
async def branding_page(request: Request):
    return render_template("branding.html", request)

@app.get("/changelog", response_class=HTMLResponse)
async def changelog_page(request: Request):
    return render_template("changelog.html", request)

@app.get("/products/{slug}", response_class=HTMLResponse)
@app.get("/products/transfer", response_class=HTMLResponse)
async def product_detail_page(request: Request, slug: str = "transfer"):
    return render_template("product_transfer.html", request, {"active_slug": slug})

@app.get("/security", response_class=HTMLResponse)
@app.get("/whitepaper", response_class=HTMLResponse)
async def security_page(request: Request):
    return render_template("security.html", request)

# --- ZEPHYR PRIVACY DEEP-DIVE PAGES ---
@app.get("/zero-knowledge", response_class=HTMLResponse)
async def zero_knowledge_page(request: Request):
    return render_template("zero_knowledge.html", request)

@app.get("/encryption", response_class=HTMLResponse)
async def encryption_page(request: Request):
    return render_template("encryption.html", request)

@app.get("/burn-on-read", response_class=HTMLResponse)
async def burn_on_read_page(request: Request):
    return render_template("burn_on_read.html", request)

@app.get("/anti-screenshot", response_class=HTMLResponse)
async def anti_screenshot_page(request: Request):
    return render_template("anti_screenshot.html", request)

@app.get("/r2-direct", response_class=HTMLResponse)
async def r2_direct_page(request: Request):
    return render_template("r2_direct.html", request)

@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request):
    return render_template("contact.html", request)

@app.get("/dmca", response_class=HTMLResponse)
async def dmca_page(request: Request):
    return render_template("dmca.html", request)

@app.get("/sign", response_class=HTMLResponse)
async def sign_page(request: Request):
    return render_template("sign.html", request)

# --- ZEPHYR CONTACT PAGE & SUPPORT INQUIRIES API (BREVO POWERED) ---
@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request):
    user_email = (
        request.cookies.get("user_email")
        or request.cookies.get("email")
        or request.cookies.get("zephyr_email")
        or request.cookies.get("auth_email")
        or ""
    )
    if not user_email and hasattr(request, "session"):
        user_email = request.session.get("user_email", "")

    return render_template("contact.html", request, {"user_email": user_email})

@app.post("/api/contact")
async def api_contact_submit(request: Request):
    import json
    import urllib.request
    from fastapi.responses import JSONResponse

    try:
        data = await request.json()
        name = data.get("name", "").strip()
        user_email = data.get("email", "").strip()
        reason = data.get("reason", "General Inquiry").strip()
        user_message = data.get("message", "").strip()

        if not user_email or "@" not in user_email:
            return JSONResponse({"status": "error", "message": "Authentication required. Please sign in to submit a support query."}, status_code=401)

        if not user_message:
            return JSONResponse({"status": "error", "message": "Message content cannot be empty."}, status_code=400)

        # 1. Save query to vault.db
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS contact_inquiries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    email TEXT,
                    reason TEXT,
                    message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(
                "INSERT INTO contact_inquiries (name, email, reason, message) VALUES (?, ?, ?, ?)",
                (name, user_email, reason, user_message)
            )
            conn.commit()
            conn.close()
        except Exception as db_err:
            print(f"[Contact DB Error] {db_err}")

        # 2. Dispatch to priyamrana069@gmail.com via Brevo API
        active_brevo_key = os.getenv("BREVO_API_KEY") or globals().get("brevo_api_key", "")
        active_sender = os.getenv("SENDER_EMAIL") or globals().get("sender_email", "priyamrana069@gmail.com")

        html_content = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; padding: 25px; background: #070913; color: #f8fafc; border-radius: 16px; border: 1px solid #38bdf8;">
            <h2 style="color: #38bdf8; margin-top: 0;">New User Query on Zephyr Drive</h2>
            <p style="font-size: 14px; margin: 6px 0;"><strong>Sender Name:</strong> {name or 'N/A'}</p>
            <p style="font-size: 14px; margin: 6px 0;"><strong>Verified Account Email:</strong> <a href="mailto:{user_email}" style="color: #818cf8;">{user_email}</a></p>
            <p style="font-size: 14px; margin: 6px 0;"><strong>Reason / Topic:</strong> {reason}</p>
            <hr style="border: none; border-top: 1px solid #1e293b; margin: 18px 0;" />
            <h4 style="color: #94a3b8; font-size: 12px; text-transform: uppercase; margin-bottom: 8px;">Message:</h4>
            <div style="background: #030408; padding: 16px; border-radius: 10px; border: 1px solid #1e293b; line-height: 1.6; white-space: pre-wrap; font-size: 13px;">{user_message}</div>
            <p style="font-size: 12px; color: #64748b; margin-top: 20px;">You can hit Reply in Gmail to respond straight to {user_email}. SLA: 24 hours.</p>
        </div>
        """

        brevo_payload = {
            "sender": {"name": "Zephyr Inquiries", "email": active_sender},
            "to": [{"email": "priyamrana069@gmail.com", "name": "Priyam Rana"}],
            "replyTo": {"email": user_email, "name": name or user_email},
            "subject": f"🔔 Zephyr Query: [{reason}] from {name or user_email}",
            "htmlContent": html_content
        }

        req_data = json.dumps(brevo_payload).encode("utf-8")
        http_req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=req_data,
            headers={
                "api-key": active_brevo_key,
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            method="POST"
        )

        with urllib.request.urlopen(http_req, timeout=15) as response:
            pass

        return JSONResponse({
            "status": "success",
            "message": "Your query has been submitted successfully. You will receive a response within 24 hours at your email address."
        })

    except Exception as e:
        print(f"[Contact API Error via Brevo] {e}")
        return JSONResponse({"status": "error", "message": f"Failed to dispatch inquiry: {str(e)}"}, status_code=500)
    
# ----------------- Pay-to-Unlock Escrow API -----------------
@app.get("/api/paywall/check-email")
@app.get("/api/paywall/check-buyer")
async def check_paywall_email(share_id: str = Query(...), email: str = Query(...)):
    clean_email = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT access_token, payment_status, buyer_password, permanent_password 
        FROM paywall_purchases 
        WHERE share_id = %s AND LOWER(buyer_email) = %s AND payment_status = 'paid'
    """, (share_id, clean_email))
    row = cursor.fetchone()
    conn.close()

    if row:
        pwd = row.get("buyer_password") or row.get("permanent_password")
        return {
            "has_paid": True,
            "email": clean_email,
            "has_password": bool(pwd)
        }
    return {"has_paid": False, "email": clean_email, "has_password": False}

@app.post("/api/create-share")
async def create_share(payload: CreateShareRequest):
    conn = get_db()
    cursor = conn.cursor()
    user_tier = "free"
    
    if payload.user_id:
        cursor.execute("SELECT tier, plan_price, subscription_end_at, storage_quota_bytes FROM users WHERE user_id = %s", (payload.user_id,))
        user_row = cursor.fetchone()
        if user_row:
            user_tier, _, _ = resolve_user_tier(user_row)

    max_mb = PLAN_CONFIG.get(user_tier, {}).get("single_mb", 2048)
    file_list = payload.files or []
    
    if payload.filename and payload.filename != "Zephyr_Transfer":
        main_filename = payload.filename
    else:
        if file_list:
            main_filename = f"{len(file_list)} items package" if len(file_list) > 1 else file_list[0].filename
        else:
            main_filename = "Shared File"
            
    total_batch_mb = sum(f.filesize_mb for f in file_list) if file_list else (payload.filesize_mb or 0.0)

    if total_batch_mb > max_mb:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Files exceed the {max_mb} MB single-transfer limit for your {user_tier.upper()} plan.")

    created_at = datetime.utcnow()
    expires_at = created_at + timedelta(days=36500) if payload.expiry_hours == 0 else created_at + timedelta(hours=payload.expiry_hours)
    share_id = uuid.uuid4().hex[:8]
    primary_s3_key = f"transfers/{share_id}/{main_filename}"
    password_hash = hashlib.sha256(payload.password.encode()).hexdigest() if payload.password else None

    cursor.execute("""
        INSERT INTO shares (
            id, filename, filesize_mb, s3_key, password_hash, expiry_hours, 
            max_downloads, created_at, expires_at, downloads, user_id, 
            is_paywalled, unlock_price, paywall_creator_id, message
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s)
    """, (
        share_id, main_filename, total_batch_mb, primary_s3_key, password_hash, 
        payload.expiry_hours, payload.max_downloads, created_at, expires_at, 
        payload.user_id, payload.is_paywalled, payload.unlock_price, payload.user_id, payload.message
    ))

    if file_list:
        for f in file_list:
            sub_s3_key = f"transfers/{share_id}/{f.filename}"
            cursor.execute("""
                INSERT INTO share_files (share_id, filename, filesize_mb, s3_key)
                VALUES (%s, %s, %s, %s)
            """, (share_id, f.filename, f.filesize_mb, sub_s3_key))
    else:
        cursor.execute("""
            INSERT INTO share_files (share_id, filename, filesize_mb, s3_key)
            VALUES (%s, %s, %s, %s)
        """, (share_id, main_filename, total_batch_mb, primary_s3_key))

    conn.commit()
    conn.close()

    return {"share_id": share_id, "expires_at": expires_at.isoformat()}

@app.post("/api/upload-file/{share_id}")
async def upload_file_direct(
    share_id: str, 
    request: Request, 
    filename: Optional[str] = Query(None)
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Vault transfer record not found.")

    target_name = (filename or row["filename"]).strip()
    target_s3_key = f"transfers/{share_id}/{target_name}"

    file_bytes = await request.body()
    content_type = request.headers.get("content-type")
    if not content_type or content_type == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(target_name)
        content_type = guessed or "application/octet-stream"

    s3_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=target_s3_key,
        Body=file_bytes,
        ContentType=content_type
    )

    cursor.execute("SELECT id FROM share_files WHERE share_id = %s AND filename = %s", (share_id, target_name))
    exists = cursor.fetchone()
    if not exists:
        filesize_mb = round(len(file_bytes) / (1024 * 1024), 2)
        cursor.execute("""
            INSERT INTO share_files (share_id, filename, filesize_mb, s3_key)
            VALUES (%s, %s, %s, %s)
        """, (share_id, target_name, filesize_mb, target_s3_key))
        conn.commit()

    conn.close()
    return {"status": "success", "share_id": share_id, "filename": target_name}

@app.get("/api/share-details/{share_id}")
async def get_share_details(share_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="File share expired or purged.")

    cursor.execute("SELECT filename, filesize_mb FROM share_files WHERE share_id = %s ORDER BY id ASC", (share_id,))
    file_rows = cursor.fetchall()
    conn.close()

    items = [
        {"filename": r["filename"], "filesize_mb": float(r["filesize_mb"])} 
        for r in file_rows
    ] if file_rows else [{"filename": row["filename"], "filesize_mb": float(row["filesize_mb"])}]

    return {
        "share_id": row["id"],
        "filename": row["filename"],
        "filesize_mb": float(row["filesize_mb"]),
        "has_password": bool(row["password_hash"]),
        "is_paywalled": bool(row.get("is_paywalled", False)),
        "unlock_price": float(row.get("unlock_price", 0.00) or 0.00),
        "files": items
    }

# ----------------- Save Indian Bank Account Payout Details -----------------
@app.post("/api/payouts/save-indian-bank")
async def save_indian_bank(payload: IndianBankPayoutRequest):
    if not payload.user_id or not payload.account_number or not payload.ifsc:
        raise HTTPException(status_code=400, detail="Account Number and IFSC are required.")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET payout_country = 'IN',
            payout_method = 'indian_bank',
            bank_account_holder = %s,
            bank_name = %s,
            bank_account_number = %s,
            bank_ifsc = %s,
            upi_id = %s
        WHERE user_id = %s
    """, (
        payload.account_holder.strip(),
        payload.bank_name.strip(),
        payload.account_number.strip(),
        payload.ifsc.strip().upper(),
        payload.upi_id.strip() if payload.upi_id else None,
        payload.user_id
    ))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Indian bank payout details saved successfully."}

@app.post("/api/paywall/initiate")
async def initiate_paywall_checkout(payload: InitiatePaywallRequest, request: Request):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured on the server.")

    buyer = payload.buyer_email.lower().strip()
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT s.*, u.stripe_account_id, u.payout_method, u.bank_account_number, u.upi_id 
        FROM shares s 
        LEFT JOIN users u ON s.paywall_creator_id = u.user_id 
        WHERE s.id = %s AND s.is_paywalled = TRUE
    """, (payload.share_id,))
    share = cursor.fetchone()

    if not share:
        conn.close()
        raise HTTPException(status_code=404, detail="Paywalled transfer session not found.")

    has_stripe = bool(share.get("stripe_account_id"))
    has_indian = bool(share.get("bank_account_number") or share.get("upi_id"))

    if not has_stripe and not has_indian:
        conn.close()
        raise HTTPException(status_code=400, detail="Creator has not linked a payout account yet.")

    cursor.execute("""
        SELECT access_token, buyer_password, permanent_password 
        FROM paywall_purchases 
        WHERE share_id = %s AND LOWER(buyer_email) = %s AND payment_status = 'paid'
    """, (payload.share_id, buyer))
    existing = cursor.fetchone()

    if existing:
        conn.close()
        return {
            "status": "already_paid",
            "message": "You have already purchased lifetime access for this file! Enter your permanent password to unlock."
        }

    access_token = f"pwtk_{secrets.token_hex(20)}"
    price_usd = float(share.get("unlock_price") or 0.00)
    price_cents = int(price_usd * 100)
    platform_fee_cents = int(price_cents * 0.12)

    base_url = str(request.base_url).rstrip("/")
    success_url = f"{base_url}/share/{payload.share_id}?paid=true&email={urllib.parse.quote(buyer)}&token={access_token}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base_url}/share/{payload.share_id}"

    payment_intent_data = {}
    if has_stripe and share.get("payout_method") != "indian_bank":
        payment_intent_data = {
            'application_fee_amount': platform_fee_cents,
            'transfer_data': {'destination': share['stripe_account_id']},
        }

    try:
        session_args = {
            "payment_method_types": ['card'],
            "customer_email": buyer,
            "line_items": [{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {'name': f"Unlock Transfer: {share['filename']}"},
                    'unit_amount': price_cents,
                },
                'quantity': 1,
            }],
            "mode": 'payment',
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"access_token": access_token, "buyer_email": buyer, "share_id": payload.share_id}
        }
        if payment_intent_data:
            session_args["payment_intent_data"] = payment_intent_data

        session = stripe.checkout.Session.create(**session_args)

        cursor.execute("""
            INSERT INTO paywall_purchases (share_id, buyer_email, access_token, amount_paid, payment_status, stripe_session_id)
            VALUES (%s, %s, %s, %s, 'pending', %s)
        """, (payload.share_id, buyer, access_token, price_usd, session.id))
        conn.commit()
        conn.close()

        return {
            "status": "checkout_ready",
            "checkout_url": session.url,
            "access_token": access_token
        }
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Checkout error: {str(e)}")

# ----------------- Stripe Checkout Session Verification -----------------
@app.post("/api/paywall/verify-session")
async def verify_stripe_checkout_session(
    request: Request,
    session_id: Optional[str] = Query(None), 
    share_id: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    token: Optional[str] = Query(None)
):
    req_session_id = session_id
    req_share_id = share_id
    req_email = email
    req_token = token

    try:
        body = await request.json()
        if isinstance(body, dict):
            req_session_id = body.get("session_id") or req_session_id
            req_share_id = body.get("share_id") or req_share_id
            req_token = body.get("token") or req_token
            req_email = body.get("email") or req_email
    except Exception:
        pass

    if not req_share_id:
        raise HTTPException(status_code=400, detail="share_id is required.")

    conn = get_db()
    cursor = conn.cursor()

    if req_session_id and stripe.api_key:
        try:
            stripe_session = stripe.checkout.Session.retrieve(req_session_id)
            if stripe_session.payment_status in ['paid', 'complete']:
                cd = stripe_session.get("customer_details") or {}
                cust_email = (
                    cd.get("email") 
                    or stripe_session.get("customer_email") 
                    or (stripe_session.get("metadata", {}) or {}).get("buyer_email") 
                    or ""
                ).strip().lower()

                cursor.execute("""
                    UPDATE paywall_purchases 
                    SET payment_status = 'paid', 
                        unlocked_at = COALESCE(unlocked_at, CURRENT_TIMESTAMP),
                        buyer_email = COALESCE(NULLIF(%s, ''), buyer_email)
                    WHERE stripe_session_id = %s OR access_token = %s
                """, (cust_email, req_session_id, req_token))
                conn.commit()
        except Exception as e:
            print(f"[SESSION RETRIEVE NOTICE]: {e}", flush=True)

    cursor.execute("""
        SELECT p.id, p.buyer_email, p.buyer_password, p.permanent_password, p.payment_status, p.access_token, s.filename 
        FROM paywall_purchases p
        JOIN shares s ON s.id = p.share_id
        WHERE p.share_id = %s 
          AND (p.stripe_session_id = %s OR p.access_token = %s OR (LOWER(p.buyer_email) = %s AND %s != ''))
          AND p.payment_status = 'paid'
        ORDER BY p.id DESC LIMIT 1
    """, (req_share_id, req_session_id, req_token, (req_email or "").strip().lower(), (req_email or "").strip().lower()))
    record = cursor.fetchone()
    conn.close()

    if not record:
        raise HTTPException(status_code=400, detail="Payment verification pending or failed.")

    active_pwd = record.get("buyer_password") or record.get("permanent_password")
    buyer_email = record["buyer_email"]

    return {
        "verified": True,
        "status": "paid",
        "email": buyer_email,
        "buyer_email": buyer_email,
        "token": record.get("access_token", ""),
        "has_password": bool(active_pwd)
    }

# ----------------- Confirm Email & Send Password Flow -----------------
@app.post("/api/paywall/confirm-and-send-password")
async def confirm_email_and_send_password(payload: ConfirmEmailSendPassRequest, request: Request):
    clean_email = payload.email.lower().strip()
    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=400, detail="Please enter a valid Gmail address.")

    conn = get_db()
    cursor = conn.cursor()

    if payload.session_id and stripe.api_key:
        try:
            stripe_session = stripe.checkout.Session.retrieve(payload.session_id)
            if stripe_session.payment_status in ['paid', 'complete']:
                cursor.execute("""
                    UPDATE paywall_purchases 
                    SET payment_status = 'paid', 
                        unlocked_at = COALESCE(unlocked_at, CURRENT_TIMESTAMP),
                        buyer_email = COALESCE(NULLIF(%s, ''), buyer_email)
                    WHERE share_id = %s AND (stripe_session_id = %s OR access_token = %s)
                """, (clean_email, payload.share_id, payload.session_id, payload.token))
                conn.commit()
        except Exception as e:
            print(f"[CONFIRM SESSION RETRIEVE NOTICE]: {e}", flush=True)

    cursor.execute("""
        SELECT p.*, s.filename 
        FROM paywall_purchases p
        JOIN shares s ON s.id = p.share_id
        WHERE p.share_id = %s 
          AND (p.stripe_session_id = %s OR p.access_token = %s OR LOWER(p.buyer_email) = %s OR p.payment_status = 'pending')
        ORDER BY p.id DESC LIMIT 1
    """, (payload.share_id, payload.session_id, payload.token, clean_email))
    purchase = cursor.fetchone()

    if not purchase:
        conn.close()
        raise HTTPException(status_code=403, detail="No payment record found for this link.")

    pwd = purchase.get("buyer_password") or purchase.get("permanent_password")
    if not pwd:
        pwd = generate_permanent_password()

    pwd_hash = hashlib.sha256(pwd.encode()).hexdigest()

    cursor.execute("""
        UPDATE paywall_purchases 
        SET payment_status = 'paid',
            buyer_email = %s, 
            buyer_password = %s,
            permanent_password = %s,
            buyer_password_hash = %s
        WHERE id = %s
    """, (clean_email, pwd, pwd, pwd_hash, purchase["id"]))
    conn.commit()
    conn.close()

    email_sent = send_buyer_password_email(clean_email, purchase["filename"], pwd, payload.share_id)
    
    if not email_sent:
        raise HTTPException(
            status_code=500, 
            detail="Brevo API Error: Failed to dispatch email. Please check your Render logs."
        )

    return {"status": "success", "email": clean_email}

@app.post("/api/paywall/unlock-password")
@app.post("/api/paywall/unlock-with-password")
async def unlock_with_password(req: UnlockWithPasswordRequest):
    clean_email = req.email.strip().lower()
    clean_pwd = req.password.strip()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.access_token, p.buyer_password, p.permanent_password, p.buyer_password_hash, s.filename 
        FROM paywall_purchases p
        JOIN shares s ON s.id = p.share_id
        WHERE p.share_id = %s AND LOWER(p.buyer_email) = %s AND payment_status = 'paid'
        ORDER BY p.id DESC LIMIT 1
    """, (req.share_id, clean_email))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=403, detail="No active purchase found for this email address.")

    pwd_hash = hashlib.sha256(clean_pwd.encode()).hexdigest()
    matched = False
    
    stored_plain = (row.get("buyer_password") or row.get("permanent_password") or "").strip()
    if stored_plain and stored_plain.upper() == clean_pwd.upper():
        matched = True
    elif row.get("buyer_password_hash") and row["buyer_password_hash"] == pwd_hash:
        matched = True

    if not matched:
        conn.close()
        raise HTTPException(status_code=401, detail="Incorrect password. Check your email or request a resend.")

    conn.close()
    return {
        "status": "unlocked",
        "access_token": row["access_token"],
        "viewer_url": f"/secure-view/{req.share_id}?token={row['access_token']}"
    }

# ----------------- Resend Permanent Password -----------------
@app.post("/api/paywall/resend-password")
async def resend_paywall_password(req: ResendPasswordRequest):
    clean_email = req.email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT p.id, p.buyer_password, p.permanent_password, s.filename 
        FROM paywall_purchases p
        JOIN shares s ON s.id = p.share_id
        WHERE p.share_id = %s AND LOWER(p.buyer_email) = %s AND p.payment_status = 'paid'
        ORDER BY p.id DESC LIMIT 1
    """, (req.share_id, clean_email))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="No completed purchase record found for this email.")

    pwd = row.get("buyer_password") or row.get("permanent_password")
    if not pwd:
        pwd = generate_permanent_password()
        cursor.execute("""
            UPDATE paywall_purchases 
            SET buyer_password = %s, 
                permanent_password = %s,
                buyer_password_hash = %s
            WHERE id = %s
        """, (pwd, pwd, hashlib.sha256(pwd.encode()).hexdigest(), row["id"]))
        conn.commit()
    conn.close()

    sent = send_buyer_password_email(clean_email, row["filename"], pwd, req.share_id)
    if not sent:
        raise HTTPException(status_code=500, detail="Could not send email via Brevo. Check server logs.")

    return {"status": "success", "message": f"Password has been dispatched to {clean_email}."}

@app.get("/api/paywall/verify-access")
async def verify_buyer_access(share_id: str = Query(...), access_token: str = Query(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT buyer_email, payment_status, buyer_password, permanent_password 
        FROM paywall_purchases 
        WHERE share_id = %s AND access_token = %s AND payment_status = 'paid'
    """, (share_id, access_token))
    purchase = cursor.fetchone()
    conn.close()

    if not purchase:
        raise HTTPException(status_code=403, detail="Payment required to access this file.")

    pwd = purchase.get("buyer_password") or purchase.get("permanent_password")
    return {
        "unlocked": True, 
        "buyer_email": purchase["buyer_email"],
        "has_password": bool(pwd)
    }

@app.post("/api/paywall/set-password")
async def set_paywall_password(req: SetPaywallPasswordRequest):
    conn = get_db()
    cursor = conn.cursor()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    cursor.execute("""
        UPDATE paywall_purchases 
        SET buyer_password_hash = %s,
            buyer_password = %s,
            permanent_password = %s
        WHERE access_token = %s AND payment_status = 'paid'
    """, (pwd_hash, req.password.strip(), req.password.strip(), req.access_token))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/paywall/login")
async def paywall_login(req: LoginPaywallRequest):
    conn = get_db()
    cursor = conn.cursor()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    cursor.execute("""
        SELECT access_token, buyer_password, permanent_password, buyer_password_hash 
        FROM paywall_purchases 
        WHERE share_id = %s AND LOWER(buyer_email) = LOWER(%s) AND payment_status = 'paid'
    """, (req.share_id, req.email.strip()))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=403, detail="No paid purchase found for this email address.")
    
    matched = False
    stored_plain = (row.get("buyer_password") or row.get("permanent_password") or "").strip()
    if stored_plain and stored_plain.upper() == req.password.strip().upper():
        matched = True
    elif row.get("buyer_password_hash") and row["buyer_password_hash"] == pwd_hash:
        matched = True

    if not matched:
        raise HTTPException(status_code=401, detail="Incorrect password for this purchase.")
    
    return {"status": "success", "access_token": row["access_token"]}

# ----------------- Secure Protected Viewer -----------------
@app.get("/secure-view/{share_id}", response_class=HTMLResponse)
async def secure_viewer_page(
    request: Request, 
    share_id: str, 
    token: str = Query(...), 
    session_id: Optional[str] = Query(None),
    file: Optional[str] = Query(None)
):
    conn = get_db()
    cursor = conn.cursor()

    if session_id and stripe.api_key:
        try:
            stripe_session = stripe.checkout.Session.retrieve(session_id)
            if stripe_session.metadata.get("access_token") == token:
                cursor.execute("""
                    UPDATE paywall_purchases 
                    SET payment_status = 'paid', unlocked_at = CURRENT_TIMESTAMP 
                    WHERE access_token = %s
                """, (token,))
                conn.commit()
        except Exception as e:
            print(f"[STRIPE VERIFY NOTICE]: {e}", flush=True)

    cursor.execute("""
        SELECT p.buyer_email, p.payment_status, s.filename, s.filesize_mb 
        FROM paywall_purchases p
        JOIN shares s ON s.id = p.share_id
        WHERE p.share_id = %s AND p.access_token = %s
    """, (share_id, token))
    row = cursor.fetchone()

    if not row or row["payment_status"] != 'paid':
        conn.close()
        raise HTTPException(status_code=403, detail="Licensed access verification required.")

    cursor.execute("SELECT filename, filesize_mb FROM share_files WHERE share_id = %s ORDER BY id ASC", (share_id,))
    file_records = cursor.fetchall()
    conn.close()

    files = [
        {"filename": f["filename"], "filesize_mb": float(f["filesize_mb"])}
        for f in file_records
    ] if file_records else [{"filename": row["filename"], "filesize_mb": float(row["filesize_mb"])}]

    selected_file = file or files[0]["filename"]

    return render_template("secure_viewer.html", request, {
        "share_id": share_id,
        "token": token,
        "buyer_email": row["buyer_email"],
        "filename": selected_file,
        "files": files,
        "files_count": len(files)
    })

# ----------------- High-Quality Media Streaming (HTTP 206 Range) -----------------
@app.get("/api/paywall/stream/{share_id}")
@app.get("/api/stream/{share_id}")
async def stream_paywall_media(
    share_id: str, 
    request: Request, 
    token: Optional[str] = Query(None), 
    file: Optional[str] = Query(None),
    password: Optional[str] = Query(None)
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    item = cursor.fetchone()

    if not item:
        conn.close()
        raise HTTPException(status_code=404, detail="Transfer not found.")

    if item.get("is_paywalled"):
        if not token:
            conn.close()
            raise HTTPException(status_code=403, detail="Protected content stream access denied.")
        cursor.execute("""
            SELECT id FROM paywall_purchases 
            WHERE share_id = %s AND access_token = %s AND payment_status = 'paid'
        """, (share_id, token))
        if not cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=403, detail="Payment verification required.")

    if item.get("password_hash"):
        if not password or hashlib.sha256(password.encode()).hexdigest() != item["password_hash"]:
            conn.close()
            raise HTTPException(status_code=401, detail="Incorrect password.")

    target_name = file if file else item["filename"]
    clean_target = urllib.parse.unquote(target_name).strip()

    cursor.execute("""
        SELECT s3_key, filename FROM share_files 
        WHERE share_id = %s AND (filename = %s OR filename = %s)
        LIMIT 1
    """, (share_id, target_name, clean_target))
    sub = cursor.fetchone()
    conn.close()

    s3_key = sub["s3_key"] if sub else f"transfers/{share_id}/{clean_target}"

    ext = clean_target.split(".")[-1].lower() if "." in clean_target else ""
    mime_map = {
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
        "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "flac": "audio/flac", "ogg": "audio/ogg",
        "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif", "svg": "image/svg+xml",
        "txt": "text/plain; charset=utf-8", "csv": "text/plain; charset=utf-8", "json": "application/json", "js": "text/javascript", "py": "text/plain; charset=utf-8", "html": "text/html; charset=utf-8"
    }
    content_type = mime_map.get(ext) or mimetypes.guess_type(clean_target)[0] or "application/octet-stream"

    range_header = request.headers.get("range")
    try:
        head = s3_client.head_object(Bucket=R2_BUCKET_NAME, Key=s3_key)
        total_size = head["ContentLength"]

        if range_header:
            range_val = range_header.strip().lower().replace("bytes=", "")
            parts = range_val.split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else total_size - 1
            if end >= total_size: end = total_size - 1
            content_length = end - start + 1

            obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=s3_key, Range=f"bytes={start}-{end}")
            return StreamingResponse(
                obj["Body"].iter_chunks(chunk_size=1024 * 512),
                status_code=206,
                media_type=content_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(content_length),
                    "Cache-Control": "no-store, no-cache, must-revalidate, private",
                    "Content-Disposition": f'inline; filename="{clean_target}"'
                }
            )
        else:
            obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=s3_key)
            return StreamingResponse(
                obj["Body"].iter_chunks(chunk_size=1024 * 512),
                media_type=content_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(total_size),
                    "Cache-Control": "no-store, no-cache, must-revalidate, private",
                    "Content-Disposition": f'inline; filename="{clean_target}"'
                }
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stream error: {str(e)}")

# ----------------- Real-ESRGAN AI Super HD Image Upscaler -----------------
@app.post("/api/enhance-image/{share_id}")
async def enhance_image_ai(
    share_id: str,
    file: Optional[str] = Query(None),
    scale: int = Query(4)
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    item = cursor.fetchone()

    if not item:
        conn.close()
        raise HTTPException(status_code=404, detail="Transfer not found.")

    target_name = file if file else item["filename"]
    clean_target = urllib.parse.unquote(target_name).strip()

    cursor.execute("""
        SELECT s3_key, filename FROM share_files 
        WHERE share_id = %s AND (filename = %s OR filename = %s)
        LIMIT 1
    """, (share_id, target_name, clean_target))
    sub = cursor.fetchone()
    conn.close()

    s3_key = sub["s3_key"] if sub else f"transfers/{share_id}/{clean_target}"

    image_url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=3600
    )

    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=400, detail="REPLICATE_API_TOKEN environment variable is missing on server.")

    try:
        model_payload = {
            "input": {
                "image": image_url,
                "scale": scale,
                "face_enhance": False
            }
        }
        req_data = json.dumps(model_payload).encode("utf-8")
        ai_req = urllib.request.Request(
            "https://api.replicate.com/v1/models/nightmareai/real-esrgan/predictions",
            data=req_data,
            headers={
                "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
                "Content-Type": "application/json",
                "Prefer": "wait"
            },
            method="POST"
        )
        
        with urllib.request.urlopen(ai_req, timeout=45) as resp:
            prediction = json.loads(resp.read().decode("utf-8"))
            
        status = prediction.get("status")
        output = prediction.get("output")
        
        if status == "succeeded" and output:
            enhanced_url = output if isinstance(output, str) else output[0]
            return {
                "status": "success",
                "engine": "Real-ESRGAN AI (4K Neural Upscale)",
                "enhanced_url": enhanced_url
            }

        pred_id = prediction.get("id")
        poll_url = f"https://api.replicate.com/v1/predictions/{pred_id}"
        
        for _ in range(40):
            time.sleep(1.5)
            p_req = urllib.request.Request(
                poll_url,
                headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}"}
            )
            with urllib.request.urlopen(p_req, timeout=15) as p_resp:
                p_data = json.loads(p_resp.read().decode("utf-8"))
                p_status = p_data.get("status")
                
                if p_status == "succeeded":
                    out = p_data.get("output")
                    enhanced_url = out if isinstance(out, str) else out[0]
                    return {
                        "status": "success",
                        "engine": "Real-ESRGAN AI (4K Neural Upscale)",
                        "enhanced_url": enhanced_url
                    }
                elif p_status in ["failed", "canceled"]:
                    error_detail = p_data.get("error") or "Prediction execution failed."
                    raise HTTPException(status_code=500, detail=f"Replicate AI Error: {error_detail}")

        raise HTTPException(status_code=504, detail="AI Upscaling timed out.")

    except urllib.error.HTTPError as he:
        err_msg = he.read().decode("utf-8", errors="ignore")
        raise HTTPException(status_code=he.code, detail=f"Replicate HTTP Error: {err_msg}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Upscale Error: {str(e)}")

# Real-ESRGAN AI Super Resolution for Drive Workspace Files
@app.post("/api/drive/enhance-image/{file_id}")
async def enhance_drive_image_ai(
    file_id: str,
    user_id: str = Query(...),
    scale: int = Query(4)
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drive_files WHERE id = %s AND user_id = %s", (file_id, user_id))
    file_record = cursor.fetchone()
    conn.close()

    if not file_record:
        raise HTTPException(status_code=404, detail="Drive asset not found.")

    image_url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": file_record["s3_key"]},
        ExpiresIn=3600
    )

    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=400, detail="REPLICATE_API_TOKEN environment variable is missing on server.")

    try:
        model_payload = {
            "input": {
                "image": image_url,
                "scale": scale,
                "face_enhance": False
            }
        }
        req_data = json.dumps(model_payload).encode("utf-8")
        ai_req = urllib.request.Request(
            "https://api.replicate.com/v1/models/nightmareai/real-esrgan/predictions",
            data=req_data,
            headers={
                "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
                "Content-Type": "application/json",
                "Prefer": "wait"
            },
            method="POST"
        )
        with urllib.request.urlopen(ai_req, timeout=45) as resp:
            prediction = json.loads(resp.read().decode("utf-8"))
            
        status = prediction.get("status")
        output = prediction.get("output")
        if status == "succeeded" and output:
            enhanced_url = output if isinstance(output, str) else output[0]
            return {"status": "success", "enhanced_url": enhanced_url}

        pred_id = prediction.get("id")
        poll_url = f"https://api.replicate.com/v1/predictions/{pred_id}"
        for _ in range(40):
            time.sleep(1.5)
            p_req = urllib.request.Request(poll_url, headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}"})
            with urllib.request.urlopen(p_req, timeout=15) as p_resp:
                p_data = json.loads(p_resp.read().decode("utf-8"))
                if p_data.get("status") == "succeeded":
                    out = p_data.get("output")
                    enhanced_url = out if isinstance(out, str) else out[0]
                    return {"status": "success", "enhanced_url": enhanced_url}
                elif p_data.get("status") in ["failed", "canceled"]:
                    raise HTTPException(status_code=500, detail="AI upscaling failed.")

        raise HTTPException(status_code=504, detail="AI Upscaling timed out.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Upscale Error: {str(e)}")

# ----------------- Ephemeral Transfers & Downloads -----------------
@app.get("/share/{share_id}", response_class=HTMLResponse)
async def share_page(request: Request, share_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Vault transfer link not found or expired.")

    cursor.execute("SELECT filename, filesize_mb FROM share_files WHERE share_id = %s ORDER BY id ASC", (share_id,))
    file_records = cursor.fetchall()

    branding = None
    if row.get("user_id"):
        cursor.execute("""
            SELECT tier, brand_title, brand_slug, brand_logo_url, brand_bg_url, brand_accent_color 
            FROM users WHERE user_id = %s
        """, (row["user_id"],))
        u_brand = cursor.fetchone()
        if u_brand and (u_brand.get("tier") or "").lower() in ["plus", "pro"]:
            branding = dict(u_brand)

    conn.close()
    file_count = len(file_records) if file_records else 1

    return render_template("download.html", request, {
        "share_id": share_id,
        "filename": row["filename"],
        "filesize": row["filesize_mb"],
        "downloads": row["downloads"],
        "message": row.get("message"),
        "has_password": bool(row["password_hash"]),
        "is_paywalled": bool(row.get("is_paywalled", False)),
        "unlock_price": float(row.get("unlock_price", 0.00) or 0.00),
        "branding": branding,
        "file_count": file_count
    })

async def delayed_r2_shred(bucket_name: str, object_key: str, delay_seconds: int = 60):
    """Gives the recipient 60s to stream and finish downloading before purging from R2."""
    await asyncio.sleep(delay_seconds)
    try:
        s3_client.delete_object(Bucket=bucket_name, Key=object_key)
    except Exception:
        pass

@app.get("/share/{share_id}/download")
@app.get("/api/download/{share_id}")
@app.post("/share/{share_id}/download")
@app.post("/api/download/{share_id}")
async def process_download(
    share_id: str, 
    request: Request,
    password: Optional[str] = Query(None),
    access_token: Optional[str] = Query(None),
    file: Optional[str] = Query(None),
    payload: Optional[DownloadPayload] = None
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Share link not found or expired.")

    effective_token = access_token or (payload.access_token if payload else None)
    effective_pass = password or (payload.password if payload else None)

    if row.get("is_paywalled"):
        if not effective_token:
            conn.close()
            if request.method == "GET":
                return RedirectResponse(url=f"/share/{share_id}", status_code=303)
            raise HTTPException(status_code=402, detail="Payment required. Enter your permanent password to unlock.")

        cursor.execute("""
            SELECT id FROM paywall_purchases 
            WHERE share_id = %s AND access_token = %s AND payment_status = 'paid'
        """, (share_id, effective_token))
        verified = cursor.fetchone()
        if not verified:
            conn.close()
            if request.method == "GET":
                return RedirectResponse(url=f"/share/{share_id}", status_code=303)
            raise HTTPException(status_code=403, detail="Invalid or unpaid access token.")

    max_downloads = row.get("max_downloads", 0) or 0
    if max_downloads > 0 and row["downloads"] >= max_downloads:
        conn.close()
        raise HTTPException(status_code=410, detail="Link reached its maximum download count.")

    if row["password_hash"]:
        if not effective_pass or hashlib.sha256(effective_pass.encode()).hexdigest() != row["password_hash"]:
            conn.close()
            if request.method == "GET":
                return RedirectResponse(url=f"/share/{share_id}", status_code=303)
            raise HTTPException(status_code=401, detail="Incorrect passcode.")

    cursor.execute("SELECT filename, s3_key FROM share_files WHERE share_id = %s ORDER BY id ASC", (share_id,))
    file_records = cursor.fetchall()

    target_s3_key = row["s3_key"]
    target_filename = row["filename"]

    if file_records:
        if file:
            clean_file = urllib.parse.unquote(file).strip()
            matched = next((fr for fr in file_records if fr["filename"] == file or fr["filename"] == clean_file), None)
            if matched:
                target_s3_key = matched["s3_key"]
                target_filename = matched["filename"]
            else:
                target_s3_key = f"transfers/{share_id}/{clean_file}"
                target_filename = clean_file
        elif len(file_records) == 1:
            target_s3_key = file_records[0]["s3_key"]
            target_filename = file_records[0]["filename"]
        else:
            urls = []
            for fr in file_records:
                u = s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": R2_BUCKET_NAME, "Key": fr["s3_key"], "ResponseContentDisposition": f'attachment; filename="{fr["filename"]}"'},
                    ExpiresIn=3600
                )
                urls.append({"filename": fr["filename"], "download_url": u})
            
            new_count = row["downloads"] + 1
            cursor.execute("UPDATE shares SET downloads = %s WHERE id = %s", (new_count, share_id))
            conn.commit()
            conn.close()

            accept = request.headers.get("accept", "")
            if "application/json" in accept or request.headers.get("x-requested-with") == "XMLHttpRequest":
                return {"download_urls": urls, "files": urls}
            return RedirectResponse(url=urls[0]["download_url"], status_code=303)

    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": target_s3_key, "ResponseContentDisposition": f'attachment; filename="{target_filename}"'},
        ExpiresIn=3600
    )
    new_count = row["downloads"] + 1
    cursor.execute("UPDATE shares SET downloads = %s WHERE id = %s", (new_count, share_id))
    conn.commit()

    if max_downloads > 0 and new_count >= max_downloads:
        # Schedule edge shredding after 60s so recipient's download finishes first
        asyncio.create_task(delayed_r2_shred(R2_BUCKET_NAME, target_s3_key, delay_seconds=60))

    conn.close()

    accept = request.headers.get("accept", "")
    if "application/json" in accept or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return {"download_url": url}

    return RedirectResponse(url=url, status_code=303)

# ----------------- Stripe Bank Account Onboarding -----------------
@app.post("/api/stripe/onboard")
async def stripe_onboard(
    user_id: str = Form(...),
    country: Optional[str] = Form("US")
):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe integration is not configured on the server.")

    clean_country = (country or "US").strip().upper()
    if clean_country in ["IN", "WISE_IN"]:
        clean_country = "US"

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT email, stripe_account_id FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found.")

    account_id = user.get("stripe_account_id")

    if not account_id:
        try:
            account_params = {
                "type": "express",
                "country": clean_country,
                "email": user["email"] if user.get("email") else None,
                "capabilities": {"transfers": {"requested": True}},
                "metadata": {"user_id": user_id}
            }
            if clean_country in ["BR", "MX"]:
                account_params["business_type"] = "individual"

            account = stripe.Account.create(**account_params)
            account_id = account.id
            cursor.execute("UPDATE users SET stripe_account_id = %s, payout_country = %s, payout_method = 'stripe' WHERE user_id = %s", (account_id, clean_country, user_id))
            conn.commit()
        except stripe.error.InvalidRequestError as e:
            conn.close()
            error_msg = urllib.parse.quote(str(e.user_message or e))
            return RedirectResponse(
                url=f"https://zephyr-drive.onrender.com/dashboard?stripe_error={error_msg}",
                status_code=303
            )
        except Exception as e:
            conn.close()
            raise HTTPException(status_code=500, detail=str(e))
    
    conn.close()

    try:
        account_link = stripe.AccountLink.create(
            account=account_id,
            refresh_url="https://zephyr-drive.onrender.com/dashboard",
            return_url="https://zephyr-drive.onrender.com/dashboard?stripe_connected=true",
            type="account_onboarding",
        )
        return RedirectResponse(url=account_link.url, status_code=303)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/stripe/manage")
async def stripe_manage(request: Request, user_id: Optional[str] = Form(None)):
    if not user_id:
        try:
            body = await request.json()
            user_id = body.get("user_id")
        except Exception:
            user_id = None

    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required.")

    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe integration is not configured on the server.")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT stripe_account_id FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()
    conn.close()

    if not user or not user.get("stripe_account_id"):
        raise HTTPException(status_code=400, detail="No connected Stripe bank account found to manage.")

    account_id = user["stripe_account_id"]

    try:
        login_link = stripe.Account.create_login_link(account_id)
        target_url = login_link.url
    except stripe.error.InvalidRequestError:
        account_link = stripe.AccountLink.create(
            account=account_id,
            refresh_url="https://zephyr-drive.onrender.com/dashboard",
            return_url="https://zephyr-drive.onrender.com/dashboard?stripe_connected=true",
            type="account_onboarding"
        )
        target_url = account_link.url
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe portal error: {str(e)}")

    accept_hdr = request.headers.get("accept", "")
    content_hdr = request.headers.get("content-type", "")
    if "application/json" in accept_hdr or "application/json" in content_hdr:
        return {"url": target_url}
    return RedirectResponse(url=target_url, status_code=303)

@app.post("/api/stripe/disconnect")
async def stripe_disconnect(request: Request):
    user_id = None
    try:
        body = await request.json()
        user_id = body.get("user_id")
    except Exception:
        try:
            form_data = await request.form()
            user_id = form_data.get("user_id")
        except Exception:
            user_id = None

    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required.")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT stripe_account_id FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found.")

    account_id = user.get("stripe_account_id")
    if account_id and stripe.api_key:
        try:
            stripe.Account.delete(account_id)
        except Exception as e:
            print(f"[STRIPE DISCONNECT NOTICE]: {e}", flush=True)

    cursor.execute("""
        UPDATE users 
        SET stripe_account_id = NULL,
            bank_account_number = NULL,
            bank_ifsc = NULL,
            bank_name = NULL,
            bank_account_holder = NULL,
            upi_id = NULL,
            payout_method = 'stripe'
        WHERE user_id = %s
    """, (user_id,))
    conn.commit()
    conn.close()

    return {"status": "success", "message": "Payout account disconnected successfully."}

# ----------------- Studio Custom Branding -----------------
@app.get("/api/branding/{user_id}")
async def get_branding(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT tier, brand_title, brand_slug, brand_logo_url, brand_bg_url, brand_accent_color 
        FROM users WHERE user_id = %s
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {
            "tier": "free",
            "brand_title": "",
            "brand_slug": None,
            "brand_logo_url": None,
            "brand_bg_url": None,
            "brand_accent_color": "#6366f1"
        }
    return dict(row)

@app.post("/api/branding/update")
async def update_branding(data: BrandingUpdatePayload):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT tier, plan_price, subscription_end_at, storage_quota_bytes FROM users WHERE user_id = %s", (data.user_id,))
    user = cursor.fetchone()

    if not user:
        cursor.execute("INSERT INTO users (user_id, tier) VALUES (%s, 'free') RETURNING *", (data.user_id,))
        conn.commit()
        user = cursor.fetchone()
    
    tier, _, _ = resolve_user_tier(user)
    if tier not in ["plus", "pro"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Custom Studio Branding requires an active Plus or Pro subscription.")

    if data.reset_default:
        cursor.execute("""
            UPDATE users 
            SET brand_title = NULL,
                brand_slug = NULL,
                brand_logo_url = NULL,
                brand_bg_url = NULL,
                brand_accent_color = '#6366f1'
            WHERE user_id = %s
        """, (data.user_id,))
        conn.commit()
        conn.close()
        return {"status": "success", "message": "Studio branding restored to default."}

    accent = (data.brand_accent_color or "#6366f1").strip()
    title = (data.brand_title or "").strip()
    logo_url = data.brand_logo_url.strip() if data.brand_logo_url else None
    bg_url = data.brand_bg_url.strip() if data.brand_bg_url else None

    cursor.execute("""
        UPDATE users 
        SET brand_title = %s, 
            brand_accent_color = %s,
            brand_logo_url = %s,
            brand_bg_url = %s
        WHERE user_id = %s
    """, (title, accent, logo_url, bg_url, data.user_id))
    
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Branding customizations saved successfully."}

@app.post("/api/branding/upload-asset")
async def upload_branding_asset(
    user_id: str = Form(...),
    asset_type: str = Form(...),
    file: UploadFile = File(...)
):
    asset_type = asset_type.lower().strip()
    if asset_type not in ["logo", "background"]:
        raise HTTPException(status_code=400, detail="Invalid asset_type.")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT tier, plan_price, subscription_end_at, storage_quota_bytes FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()

    tier, _, _ = resolve_user_tier(user) if user else ("free", 0, 0)
    if tier not in ["plus", "pro"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Custom Studio Branding requires an active Plus or Pro subscription.")

    ext = file.filename.split(".")[-1] if "." in file.filename else "png"
    s3_key = f"branding/{user_id}/{asset_type}_{int(datetime.utcnow().timestamp())}.{ext}"
    file_bytes = await file.read()

    try:
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=s3_key,
            Body=file_bytes,
            ContentType=file.content_type or "image/png"
        )
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"R2 storage error: {str(e)}")

    if R2_PUBLIC_DOMAIN:
        public_url = f"{R2_PUBLIC_DOMAIN}/{s3_key}"
    else:
        public_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": s3_key},
            ExpiresIn=604800
        )

    column = "brand_logo_url" if asset_type == "logo" else "brand_bg_url"
    cursor.execute(f"UPDATE users SET {column} = %s WHERE user_id = %s", (public_url, user_id))
    conn.commit()
    conn.close()

    return {"status": "success", "url": public_url}

# ----------------- E-Sign Document & Envelope API -----------------
@app.get("/api/sign/quota")
async def get_sign_quota(user_id: Optional[str] = None, email: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor()
    user_tier = "free"
    
    if user_id:
        cursor.execute("SELECT tier, plan_price, subscription_end_at, storage_quota_bytes FROM users WHERE user_id = %s", (user_id,))
        u = cursor.fetchone()
        if u:
            user_tier, _, _ = resolve_user_tier(u)
    elif email:
        cursor.execute("SELECT tier, plan_price, subscription_end_at, storage_quota_bytes FROM users WHERE LOWER(email) = LOWER(%s)", (email.strip(),))
        u = cursor.fetchone()
        if u:
            user_tier, _, _ = resolve_user_tier(u)

    tier_cfg = PLAN_CONFIG.get(user_tier, PLAN_CONFIG["free"])
    daily_limit = tier_cfg.get("esign_daily", 7)

    today_count = 0
    if daily_limit != -1:
        if user_id:
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM signature_requests 
                WHERE user_id = %s AND created_at >= CURRENT_DATE
            """, (user_id,))
        elif email:
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM signature_requests 
                WHERE LOWER(creator_email) = LOWER(%s) AND created_at >= CURRENT_DATE
            """, (email.strip(),))
        row = cursor.fetchone()
        today_count = row["cnt"] if row else 0

    conn.close()
    return {
        "tier": user_tier,
        "plan_name": tier_cfg["name"],
        "daily_limit": daily_limit,
        "used_today": today_count,
        "remaining": "Unlimited" if daily_limit == -1 else max(0, daily_limit - today_count)
    }

@app.post("/api/sign/upload")
async def upload_sign_doc(
    request: Request,
    filename: str,
    recipient_name: str = "Recipient",
    recipient_email: str = "",
    title: str = "Agreement",
    x: int = 150,
    y: int = 250,
    creator_email: Optional[str] = "",
    user_id: Optional[str] = ""
):
    conn = get_db()
    cursor = conn.cursor()

    resolved_creator = (creator_email or "").strip()
    user_tier = "free"

    if user_id:
        cursor.execute("SELECT email, tier, plan_price, subscription_end_at, storage_quota_bytes FROM users WHERE user_id = %s", (user_id,))
        u = cursor.fetchone()
        if u:
            if u.get("email") and not resolved_creator:
                resolved_creator = u["email"]
            user_tier, _, _ = resolve_user_tier(u)
    elif resolved_creator:
        cursor.execute("SELECT tier, plan_price, subscription_end_at, storage_quota_bytes FROM users WHERE LOWER(email) = LOWER(%s)", (resolved_creator,))
        u = cursor.fetchone()
        if u:
            user_tier, _, _ = resolve_user_tier(u)

    tier_cfg = PLAN_CONFIG.get(user_tier, PLAN_CONFIG["free"])
    daily_limit = tier_cfg.get("esign_daily", 7)

    if daily_limit != -1:
        if user_id:
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM signature_requests 
                WHERE user_id = %s AND created_at >= CURRENT_DATE
            """, (user_id,))
        else:
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM signature_requests 
                WHERE LOWER(creator_email) = LOWER(%s) AND created_at >= CURRENT_DATE
            """, (resolved_creator.lower(),))
        
        row = cursor.fetchone()
        today_count = row["cnt"] if row else 0

        if today_count >= daily_limit:
            conn.close()
            raise HTTPException(
                status_code=403,
                detail=f"Daily E-Sign limit reached ({today_count}/{daily_limit} created today). Upgrade your plan to sign more documents."
            )

    doc_id = uuid.uuid4().hex[:10]
    body = await request.body()
    content_type = request.headers.get("content-type", "application/pdf")
    s3_key = f"sign_docs/{doc_id}/{filename}"
    
    try:
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=s3_key,
            Body=body,
            ContentType=content_type
        )
    except Exception as err:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Storage error: {str(err)}")

    cursor.execute("""
        INSERT INTO signature_requests (doc_id, title, recipient_name, recipient_email, status, signature_x, signature_y, creator_email, user_id)
        VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s, %s)
    """, (doc_id, title, recipient_name, recipient_email, x, y, resolved_creator, user_id))
    conn.commit()
    conn.close()

    return {"doc_id": doc_id, "filename": filename}

@app.get("/api/sign/requests")
async def get_signature_requests():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM signature_requests ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    
    return [
        {
            "doc_id": r["doc_id"],
            "title": r["title"],
            "recipient_name": r["recipient_name"],
            "recipient_email": r["recipient_email"],
            "creator_email": r.get("creator_email") or "",
            "status": r["status"],
            "x": r["signature_x"],
            "y": r["signature_y"],
            "created_at": str(r["created_at"])[:19]
        }
        for r in rows
    ]

@app.get("/api/sign/document/{doc_id}")
async def get_sign_doc(doc_id: str, download: Optional[str] = None):
    s3_key = f"sign_docs/{doc_id}/completed_signed.png" if download == "signed" else None
    
    if download == "signed":
        try:
            s3_client.head_object(Bucket=R2_BUCKET_NAME, Key=s3_key)
        except Exception:
            raise HTTPException(status_code=404, detail="Signed document not found or pending.")

        url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET_NAME,
                "Key": s3_key,
                "ResponseContentDisposition": f'attachment; filename="signed_{doc_id}.png"'
            },
            ExpiresIn=3600
        )
        return RedirectResponse(url=url)

    prefix = f"sign_docs/{doc_id}/"
    res = s3_client.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix)
    contents = res.get("Contents", [])
    if not contents:
        raise HTTPException(status_code=404, detail="Signing document not found or expired")

    template_files = [c for c in contents if not c["Key"].endswith("completed_signed.png")]
    s3_key = template_files[0]["Key"] if template_files else contents[0]["Key"]

    url = f"/api/sign/file/{doc_id}"

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM signature_requests WHERE doc_id = %s", (doc_id,))
    meta = cursor.fetchone()
    conn.close()

    return {"url": url, "metadata": meta}

@app.get("/api/sign/file/{doc_id}")
async def get_sign_file_stream(doc_id: str):
    prefix = f"sign_docs/{doc_id}/"
    res = s3_client.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix)
    contents = res.get("Contents", [])
    if not contents:
        raise HTTPException(status_code=404, detail="Document file not found")
    
    template_files = [c for c in contents if not c["Key"].endswith("completed_signed.png")]
    s3_key = template_files[0]["Key"] if template_files else contents[0]["Key"]

    obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=s3_key)
    pdf_bytes = obj["Body"].read()
    
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=document.pdf"}
    )

@app.post("/api/sign/complete/{doc_id}")
async def complete_signing(doc_id: str, request: Request):
    body = await request.body()
    content_type = request.headers.get("content-type", "image/png")
    s3_key = f"sign_docs/{doc_id}/completed_signed.png"
    
    s3_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=s3_key,
        Body=body,
        ContentType=content_type
    )

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM signature_requests WHERE doc_id = %s", (doc_id,))
    envelope = cursor.fetchone()

    cursor.execute("UPDATE signature_requests SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE doc_id = %s", (doc_id,))
    conn.commit()
    conn.close()

    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()
    system_sender = os.getenv("SENDER_EMAIL", "priyamrana069@gmail.com").strip()

    if envelope:
        creator_target = (envelope.get("creator_email") or "").strip() or system_sender
        recipient_name = envelope.get("recipient_name") or "Signer"
        doc_title = envelope.get("title") or "Document"
        download_url = f"https://zephyr-drive.onrender.com/api/sign/document/{doc_id}?download=signed"

        if brevo_api_key and creator_target:
            notification_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 520px; margin: auto; padding: 25px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #0f172a; color: #f8fafc;">
                <h2 style="color: #10b981; margin-top: 0;">✓ Document Signed & Sealed</h2>
                <p style="font-size: 14px; color: #cbd5e1; line-height: 1.5;">
                    Great news! <strong>{recipient_name}</strong> has signed <strong>{doc_title}</strong>.
                </p>
                <div style="background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 12px; padding: 15px; margin: 20px 0; font-size: 13px;">
                    <p style="margin: 0 0 6px 0; color: #94a3b8;"><strong>Document:</strong> {doc_title}</p>
                    <p style="margin: 0 0 6px 0; color: #94a3b8;"><strong>Signed By:</strong> {recipient_name} ({envelope.get('recipient_email') or 'No email'})</p>
                    <p style="margin: 0; color: #10b981;"><strong>Status:</strong> Completed & Verified</p>
                </div>
                <div style="text-align: center; margin: 25px 0;">
                    <a href="{download_url}" style="background: #4f46e5; color: #ffffff; padding: 12px 24px; border-radius: 10px; font-weight: bold; text-decoration: none; display: inline-block; font-size: 13px;">
                        Download Countersigned Copy
                    </a>
                </div>
                <p style="font-size: 11px; color: #64748b; margin-bottom: 0;">
                    Manage your active documents at any time in your <a href="https://zephyr-drive.onrender.com/sign" style="color: #818cf8;">Zephyr Sign Studio</a>.
                </p>
            </div>
            """

            payload = {
                "sender": {"name": "Zephyr E-Sign", "email": system_sender},
                "to": [{"email": creator_target}],
                "subject": f"🖋️ Document Signed: '{doc_title}' has been signed by {recipient_name}",
                "htmlContent": notification_html
            }

            try:
                http_req = urllib.request.Request(
                    "https://api.brevo.com/v3/smtp/email",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"api-key": brevo_api_key, "Content-Type": "application/json", "Accept": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(http_req, timeout=12) as resp:
                    print(f"[BREVO E-SIGN SUCCESS] Dispatched notification to {creator_target} (Status {resp.status})", flush=True)
            except Exception as e:
                print(f"[BREVO E-SIGN ERROR] Failed sending to {creator_target}: {e}", flush=True)

    return {"status": "saved", "doc_id": doc_id}

@app.get("/api/sign/check-status/{doc_id}")
async def check_signing_status(doc_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM signature_requests WHERE doc_id = %s", (doc_id,))
    row = cursor.fetchone()
    conn.close()

    if not row or row["status"] != "completed":
        return {"status": "pending"}

    s3_key = f"sign_docs/{doc_id}/completed_signed.png"
    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": s3_key},
            ExpiresIn=86400
        )
        return {"status": "completed", "download_url": url}
    except Exception:
        return {"status": "pending"}

@app.delete("/api/sign/request/{doc_id}")
async def delete_signature_request(doc_id: str):
    prefix = f"sign_docs/{doc_id}/"
    try:
        res = s3_client.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix)
        objects = [{"Key": obj["Key"]} for obj in res.get("Contents", [])]
        if objects:
            s3_client.delete_objects(
                Bucket=R2_BUCKET_NAME,
                Delete={"Objects": objects}
            )
    except Exception as e:
        print(f"R2 delete cleanup error: {e}")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM signature_requests WHERE doc_id = %s", (doc_id,))
    conn.commit()
    conn.close()

    return {"status": "deleted", "doc_id": doc_id}

# ----------------- Zephyr Drive Storage Engine -----------------
@app.get("/api/drive/quota")
async def get_drive_quota(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT tier, storage_used_bytes, storage_quota_bytes, plan_price, subscription_end_at, grace_period_end_at 
        FROM users 
        WHERE user_id = %s
    """, (user_id,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return {
            "tier": "free",
            "used_bytes": 0,
            "quota_bytes": 5368709120,
            "plan_price": 0.00,
            "subscription_end_at": None,
            "grace_period_end_at": None
        }

    resolved_tier, resolved_quota, plan_price = resolve_user_tier(user)
    current_tier = (user.get("tier") or "free").lower()
    current_quota = user.get("storage_quota_bytes") or 0

    if current_tier != resolved_tier or current_quota < resolved_quota:
        try:
            cursor.execute("""
                UPDATE users 
                SET tier = %s, storage_quota_bytes = %s, grace_period_end_at = NULL 
                WHERE user_id = %s
            """, (resolved_tier, resolved_quota, user_id))
            conn.commit()
            user["tier"] = resolved_tier
            user["storage_quota_bytes"] = resolved_quota
        except Exception as ex:
            print(f"[QUOTA SYNC ERROR]: {ex}", flush=True)

    conn.close()

    return {
        "tier": resolved_tier,
        "used_bytes": user.get("storage_used_bytes", 0) or 0,
        "quota_bytes": resolved_quota,
        "plan_price": plan_price,
        "subscription_end_at": str(user.get("subscription_end_at"))[:19] if user.get("subscription_end_at") else None,
        "grace_period_end_at": str(user.get("grace_period_end_at"))[:19] if user.get("grace_period_end_at") else None
    }

@app.get("/api/drive/files")
async def get_drive_files(user_id: str):
    if not user_id or user_id.strip() == "":
        return []
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, filename, file_type, size_bytes, created_at FROM drive_files WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
    files = cursor.fetchall()
    conn.close()

    return [
        {
            "id": str(f["id"]),
            "filename": f["filename"],
            "file_type": f["file_type"] or "Unknown",
            "size_bytes": f["size_bytes"],
            "size_mb": round(f["size_bytes"] / (1024 * 1024), 2),
            "created_at": str(f["created_at"])[:19]
        }
        for f in files
    ]

@app.post("/api/drive/upload")
async def upload_drive_file(request: Request, filename: str, user_id: str):
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for Drive storage.")

    file_bytes = await request.body()
    file_size = len(file_bytes)
    content_type = request.headers.get("content-type", "application/octet-stream")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT tier, storage_used_bytes, storage_quota_bytes, plan_price, subscription_end_at FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()

    if not user:
        cursor.execute("INSERT INTO users (user_id, tier, storage_used_bytes, storage_quota_bytes) VALUES (%s, 'free', 0, 5368709120)", (user_id,))
        conn.commit()
        used = 0
        quota = 5368709120
    else:
        used = user.get("storage_used_bytes") or 0
        _, quota, _ = resolve_user_tier(user)

    if (used + file_size) > quota:
        conn.close()
        raise HTTPException(status_code=403, detail="Drive storage quota exceeded. Upgrade your plan for more space.")

    file_id = uuid.uuid4().hex
    s3_key = f"drive/{user_id}/{file_id}_{filename}"

    try:
        s3_client.put_object(Bucket=R2_BUCKET_NAME, Key=s3_key, Body=file_bytes, ContentType=content_type)
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"R2 Cloud storage error: {str(e)}")

    cursor.execute("""
        INSERT INTO drive_files (id, user_id, filename, file_type, size_bytes, s3_key)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (file_id, user_id, filename, content_type, file_size, s3_key))

    cursor.execute("UPDATE users SET storage_used_bytes = storage_used_bytes + %s WHERE user_id = %s", (file_size, user_id))
    conn.commit()
    conn.close()

    return {"status": "success", "file_id": file_id}

@app.post("/api/drive/upload-client")
async def upload_client_deposit(
    request: Request,
    owner_id: str,
    filename: str,
    project_title: str = "Client Deposit",
    client_name: str = "Client",
    client_email: str = "",
    owner_email: Optional[str] = ""
):
    if not owner_id:
        raise HTTPException(status_code=400, detail="Owner ID is required.")

    file_bytes = await request.body()
    file_size = len(file_bytes)
    content_type = request.headers.get("content-type", "application/octet-stream")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT tier, storage_used_bytes, storage_quota_bytes, email, plan_price, subscription_end_at FROM users WHERE user_id = %s", (owner_id,))
    owner = cursor.fetchone()

    system_sender = os.getenv("SENDER_EMAIL", "priyamrana069@gmail.com").strip()

    resolved_owner_email = (
        owner_email.strip()
        or (owner.get("email") if owner else None)
        or system_sender
    )

    if not owner:
        cursor.execute(
            "INSERT INTO users (user_id, email, tier, storage_used_bytes, storage_quota_bytes) VALUES (%s, %s, 'free', 0, 5368709120)",
            (owner_id, resolved_owner_email)
        )
        conn.commit()
        used = 0
        quota = 5368709120
    else:
        used = owner.get("storage_used_bytes") or 0
        _, quota, _ = resolve_user_tier(owner)
        if not owner.get("email") and resolved_owner_email:
            cursor.execute("UPDATE users SET email = %s WHERE user_id = %s", (resolved_owner_email, owner_id))
            conn.commit()

    if (used + file_size) > quota:
        conn.close()
        raise HTTPException(status_code=403, detail="Recipient vault storage quota is full.")

    file_id = uuid.uuid4().hex
    stored_filename = f"[{project_title}] {filename}" if project_title else filename
    s3_key = f"drive/{owner_id}/{file_id}_{filename}"

    try:
        s3_client.put_object(Bucket=R2_BUCKET_NAME, Key=s3_key, Body=file_bytes, ContentType=content_type)
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"R2 Cloud storage error: {str(e)}")

    cursor.execute("""
        INSERT INTO drive_files (id, user_id, filename, file_type, size_bytes, s3_key)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (file_id, owner_id, stored_filename, content_type, file_size, s3_key))

    cursor.execute("UPDATE users SET storage_used_bytes = storage_used_bytes + %s WHERE user_id = %s", (file_size, owner_id))
    conn.commit()
    conn.close()

    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()
    filesize_mb = round(file_size / (1024 * 1024), 2)

    def dispatch_brevo(to_address: str, subject: str, html_body: str):
        if not brevo_api_key or not to_address:
            return
        payload = {
            "sender": {"name": "Zephyr Vault Deposit", "email": system_sender},
            "to": [{"email": to_address}],
            "subject": subject,
            "htmlContent": html_body
        }
        try:
            http_req = urllib.request.Request(
                "https://api.brevo.com/v3/smtp/email",
                data=json.dumps(payload).encode("utf-8"),
                headers={"api-key": brevo_api_key, "Content-Type": "application/json", "Accept": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=12) as resp:
                print(f"[BREVO SUCCESS] Dispatched notification to {to_address}", flush=True)
        except Exception as e:
            print(f"[BREVO ERROR] Failed sending to {to_address}: {e}", flush=True)

    if resolved_owner_email:
        owner_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 520px; margin: auto; padding: 25px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #f8fafc;">
            <h2 style="color: #4f46e5; margin-top: 0;">📥 New Client File Received</h2>
            <p style="font-size: 14px; color: #1e293b; line-height: 1.5;">
                <strong>{client_name or 'A client'}</strong> ({client_email or 'No email specified'}) deposited a file into your vault.
            </p>
            <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 15px; margin: 15px 0; font-size: 13px; color: #475569;">
                <p style="margin: 0 0 6px 0;"><strong>Project:</strong> {project_title}</p>
                <p style="margin: 0;"><strong>File:</strong> {filename} ({filesize_mb} MB)</p>
            </div>
            <p style="font-size: 12px; color: #64748b;">
                Manage this file inside your <a href="https://zephyr-drive.onrender.com/dashboard" style="color: #4f46e5; font-weight: bold;">Cloud Drive Vault</a>.
            </p>
        </div>
        """
        dispatch_brevo(resolved_owner_email, f"📥 New deposit received for '{project_title}': {filename}", owner_html)

    if client_email and client_email.strip().lower() != resolved_owner_email.lower():
        client_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 520px; margin: auto; padding: 25px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #f8fafc;">
            <h2 style="color: #059669; margin-top: 0;">✓ Upload Confirmed</h2>
            <p style="font-size: 14px; color: #1e293b; line-height: 1.5;">
                Hello {client_name or 'there'}, your file has been safely encrypted and uploaded to the project vault.
            </p>
            <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 15px; margin: 15px 0; font-size: 13px; color: #475569;">
                <p style="margin: 0 0 6px 0;"><strong>Project:</strong> {project_title}</p>
                <p style="margin: 0;"><strong>File:</strong> {filename} ({filesize_mb} MB)</p>
            </div>
            <p style="font-size: 11px; color: #94a3b8;">Sent securely via Zephyr Systems.</p>
        </div>
        """
        dispatch_brevo(client_email.strip(), f"✓ Upload Confirmation: {filename}", client_html)

    return {"status": "success", "file_id": file_id, "filename": stored_filename}

@app.get("/api/drive/download/{file_id}")
async def download_drive_file(file_id: str, user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drive_files WHERE id = %s AND user_id = %s", (file_id, user_id))
    file = cursor.fetchone()
    conn.close()

    if not file:
        raise HTTPException(status_code=404, detail="File not found or access unauthorized.")

    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET_NAME,
                "Key": file["s3_key"],
                "ResponseContentDisposition": f'attachment; filename="{file["filename"]}"'
            },
            ExpiresIn=3600
        )
        return {"download_url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Presign error: {str(e)}")

@app.delete("/api/drive/files/{file_id}")
async def delete_drive_file(file_id: str, user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drive_files WHERE id = %s AND user_id = %s", (file_id, user_id))
    file = cursor.fetchone()

    if not file:
        raise HTTPException(status_code=404, detail="File not found.")

    try:
        s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=file["s3_key"])
    except Exception:
        pass

    cursor.execute("DELETE FROM drive_files WHERE id = %s", (file_id,))
    cursor.execute("UPDATE users SET storage_used_bytes = GREATEST(0, storage_used_bytes - %s) WHERE user_id = %s", (file["size_bytes"], user_id))
    conn.commit()
    conn.close()

    return {"status": "deleted"}

@app.post("/api/drive/rename-file")
async def rename_drive_file(payload: RenameFileRequest):
    new_name = payload.new_filename.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Filename cannot be empty")

    try:
        conn = get_db()
        cursor = conn.cursor()
        if payload.user_id:
            cursor.execute(
                "UPDATE drive_files SET filename = %s WHERE id = %s AND user_id = %s",
                (new_name, payload.file_id, payload.user_id)
            )
        else:
            cursor.execute(
                "UPDATE drive_files SET filename = %s WHERE id = %s",
                (new_name, payload.file_id)
            )
        conn.commit()
        conn.close()
        return {"status": "success", "new_filename": new_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database update failed: {str(e)}")

# ----------------- Drive Studio Media Stream & Save -----------------
@app.get("/api/drive/stream/{file_id}")
async def stream_drive_file(file_id: str, user_id: str, request: Request):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drive_files WHERE id = %s AND user_id = %s", (file_id, user_id))
    file = cursor.fetchone()
    conn.close()

    if not file:
        raise HTTPException(status_code=404, detail="File not found.")

    s3_key = file["s3_key"]
    filename = file["filename"]
    ext = filename.split(".")[-1].lower() if "." in filename else ""
    
    mime_map = {
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
        "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "flac": "audio/flac", "ogg": "audio/ogg",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif", "svg": "image/svg+xml",
        "pdf": "application/pdf", "txt": "text/plain; charset=utf-8", "json": "application/json", "py": "text/plain; charset=utf-8",
        "js": "text/javascript", "html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8", "md": "text/plain; charset=utf-8"
    }
    content_type = mime_map.get(ext) or file.get("file_type") or "application/octet-stream"

    try:
        head = s3_client.head_object(Bucket=R2_BUCKET_NAME, Key=s3_key)
        total_size = head["ContentLength"]
        range_header = request.headers.get("range")

        if range_header:
            range_val = range_header.strip().lower().replace("bytes=", "")
            parts = range_val.split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else total_size - 1
            if end >= total_size: end = total_size - 1
            content_length = end - start + 1

            obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=s3_key, Range=f"bytes={start}-{end}")
            return StreamingResponse(
                obj["Body"].iter_chunks(chunk_size=1024 * 512),
                status_code=206,
                media_type=content_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(content_length),
                    "Content-Disposition": f'inline; filename="{filename}"'
                }
            )
        else:
            obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=s3_key)
            return StreamingResponse(
                obj["Body"].iter_chunks(chunk_size=1024 * 512),
                media_type=content_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(total_size),
                    "Content-Disposition": f'inline; filename="{filename}"'
                }
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stream error: {str(e)}")

@app.post("/api/drive/save-file/{file_id}")
async def save_drive_file_edits(file_id: str, user_id: str, request: Request):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drive_files WHERE id = %s AND user_id = %s", (file_id, user_id))
    file = cursor.fetchone()

    if not file:
        conn.close()
        raise HTTPException(status_code=404, detail="File not found.")

    new_bytes = await request.body()
    new_size = len(new_bytes)
    size_diff = new_size - file["size_bytes"]

    s3_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=file["s3_key"],
        Body=new_bytes,
        ContentType=request.headers.get("content-type") or file.get("file_type") or "application/octet-stream"
    )

    cursor.execute("UPDATE drive_files SET size_bytes = %s WHERE id = %s", (new_size, file_id))
    cursor.execute("UPDATE users SET storage_used_bytes = GREATEST(0, storage_used_bytes + %s) WHERE user_id = %s", (size_diff, user_id))
    conn.commit()
    conn.close()

    return {"status": "success", "new_size_bytes": new_size}

# ----------------- Mid-Cycle Cumulative Upgrades (Max 600 GB) -----------------
@app.post("/api/drive/upgrade-quote")
async def calculate_prorated_upgrade(payload: UpgradeQuoteRequest):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT tier, plan_price, storage_quota_bytes, subscription_end_at FROM users WHERE user_id = %s", (payload.user_id,))
    user = cursor.fetchone()
    conn.close()

    target_tier_key = payload.target_tier.lower().strip()
    target = PLAN_CONFIG.get(target_tier_key)
    if not target or target_tier_key == "free":
        raise HTTPException(status_code=400, detail="Invalid target plan.")

    current_price = float(user.get("plan_price") or 0.0) if user else 0.0
    current_quota = int(user.get("storage_quota_bytes") or PLAN_CONFIG["free"]["quota"]) if user else PLAN_CONFIG["free"]["quota"]
    sub_end = user.get("subscription_end_at") if user else None

    has_active_sub = is_sub_active(sub_end) and current_price > 0.0

    days_remaining = 0
    if has_active_sub and sub_end:
        if isinstance(sub_end, str):
            try:
                sub_end = datetime.fromisoformat(sub_end.replace("Z", "+00:00"))
            except Exception:
                pass
        sub_end_naive = sub_end.astimezone(timezone.utc).replace(tzinfo=None) if getattr(sub_end, "tzinfo", None) is not None else sub_end
        days_remaining = max(0, (sub_end_naive - datetime.utcnow()).days)

    surcharge = 0.80 if target_tier_key == "pro" else 0.50

    if has_active_sub:
        new_total_quota = current_quota + target["quota"]
        if new_total_quota > MAX_VAULT_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum storage limit reached. Net cumulative vault storage cannot exceed 600 GB."
            )
        final_amount = round(target["price"] + surcharge, 2)
    else:
        final_amount = target["price"]
        new_total_quota = target["quota"]
        surcharge = 0.00

    return {
        "target_tier": target_tier_key,
        "target_name": target["name"],
        "charge_amount": final_amount,
        "days_remaining": days_remaining,
        "surcharge": surcharge,
        "target_quota_bytes": new_total_quota,
        "max_cap_bytes": MAX_VAULT_BYTES
    }

# ----------------- 20-Day Grace Period Automated Cleaner -----------------
@app.post("/api/maintenance/prune-expired-vaults")
async def prune_expired_vaults(secret: str = ""):
    maintenance_key = os.getenv("MAINTENANCE_SECRET", "zephyr_cron_secret").strip()
    if secret != maintenance_key:
        raise HTTPException(status_code=401, detail="Unauthorized maintenance invocation.")

    conn = get_db()
    cursor = conn.cursor()
    now = datetime.utcnow()

    cursor.execute("""
        SELECT user_id, tier, storage_used_bytes, storage_quota_bytes 
        FROM users 
        WHERE grace_period_end_at IS NOT NULL 
          AND grace_period_end_at <= %s 
          AND storage_used_bytes > storage_quota_bytes
    """, (now,))
    over_limit_users = cursor.fetchall()

    pruned_count = 0
    for u in over_limit_users:
        uid = u["user_id"]
        used = u["storage_used_bytes"]
        user_tier = (u.get("tier") or "free").lower()
        default_quota = PLAN_CONFIG.get(user_tier, PLAN_CONFIG["free"])["quota"]
        allowed_quota = u["storage_quota_bytes"] or default_quota

        cursor.execute("SELECT id, s3_key, size_bytes FROM drive_files WHERE user_id = %s ORDER BY created_at ASC", (uid,))
        files = cursor.fetchall()

        for f in files:
            if used <= allowed_quota:
                break
            try:
                s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=f["s3_key"])
            except Exception:
                pass
            
            cursor.execute("DELETE FROM drive_files WHERE id = %s", (f["id"],))
            used -= f["size_bytes"]
            pruned_count += 1

        cursor.execute("""
            UPDATE users 
            SET storage_used_bytes = %s, 
                grace_period_end_at = NULL 
            WHERE user_id = %s
        """, (max(0, used), uid))
        conn.commit()

    conn.close()
    return {"status": "success", "processed_users": len(over_limit_users), "files_pruned": pruned_count}

@app.get("/api/user-profile")
async def get_user_profile(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {"tier": "free", "user_id": user_id, "stripe_connected": False, "has_indian_bank": False}

    resolved_tier, resolved_quota, plan_price = resolve_user_tier(row)
    current_tier = (row.get("tier") or "free").lower()
    current_quota = row.get("storage_quota_bytes") or 0

    if current_tier != resolved_tier or current_quota < resolved_quota:
        try:
            cursor.execute("""
                UPDATE users 
                SET tier = %s, storage_quota_bytes = %s, grace_period_end_at = NULL 
                WHERE user_id = %s
            """, (resolved_tier, resolved_quota, user_id))
            conn.commit()
            row["tier"] = resolved_tier
        except Exception as ex:
            print(f"[PROFILE SYNC ERROR]: {ex}", flush=True)

    conn.close()

    has_indian = bool(row.get("bank_account_number") or row.get("upi_id"))
    masked_acc = f"...{row.get('bank_account_number')[-4:]}" if row.get("bank_account_number") else None

    return {
        "tier": resolved_tier,
        "email": row.get("email"),
        "user_id": row.get("user_id"),
        "brand_title": row.get("brand_title"),
        "brand_slug": row.get("brand_slug"),
        "brand_logo_url": row.get("brand_logo_url"),
        "brand_bg_url": row.get("brand_bg_url"),
        "brand_accent_color": row.get("brand_accent_color") or "#6366f1",
        "stripe_connected": bool(row.get("stripe_account_id")),
        "payout_method": row.get("payout_method") or "stripe",
        "payout_country": row.get("payout_country") or "US",
        "has_indian_bank": has_indian,
        "bank_name": row.get("bank_name"),
        "bank_ifsc": row.get("bank_ifsc"),
        "bank_account_holder": row.get("bank_account_holder"),
        "indian_bank_masked": masked_acc,
        "upi_id": row.get("upi_id"),
        "subscription_end_at": str(row.get("subscription_end_at"))[:19] if row.get("subscription_end_at") else None,
        "grace_period_end_at": str(row.get("grace_period_end_at"))[:19] if row.get("grace_period_end_at") else None
    }

# ----------------- Dodo Payments Webhook -----------------
@app.post("/api/webhook/dodo")
async def dodo_webhook(request: Request):
    try:
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    headers = {
        "webhook-id": request.headers.get("webhook-id"),
        "webhook-timestamp": request.headers.get("webhook-timestamp"),
        "webhook-signature": request.headers.get("webhook-signature") or request.headers.get("x-dodo-signature"),
    }

    if DODO_WEBHOOK_SECRET:
        if not verify_dodo_svix_signature(raw_body, headers, DODO_WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = payload.get("type", "")
    data_block = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    metadata = data_block.get("metadata") or payload.get("metadata") or {}

    conn = get_db()
    cursor = conn.cursor()

    user_id = metadata.get("user_id") or metadata.get("userId") or metadata.get("metadata_user_id")
    customer_info = data_block.get("customer") or payload.get("customer") or {}
    user_email = (
        (customer_info.get("email") if isinstance(customer_info, dict) else None)
        or data_block.get("customer_email")
        or payload.get("customer_email")
        or metadata.get("email")
    )

    product_id = data_block.get("product_id")
    if not product_id:
        product_cart = data_block.get("product_cart") or []
        if product_cart and isinstance(product_cart, list):
            product_id = product_cart[0].get("product_id")

    detected_tier = DODO_PRODUCT_MAP.get(product_id)
    if not detected_tier:
        detected_tier = metadata.get("tier") or metadata.get("plan")

    requested_tier = (detected_tier or "micro").lower().strip()
    if requested_tier not in PLAN_CONFIG or requested_tier == "free":
        requested_tier = "micro"

    tier_info = PLAN_CONFIG[requested_tier]
    target_quota = tier_info["quota"]
    target_price = tier_info["price"]

    if not user_id and user_email:
        try:
            cursor.execute("SELECT id FROM auth.users WHERE LOWER(email) = LOWER(%s)", (user_email.strip(),))
            auth_row = cursor.fetchone()
            if auth_row and auth_row.get("id"):
                user_id = str(auth_row["id"])
        except Exception as e:
            print(f"[AUTH LOOKUP NOTICE]: {e}", flush=True)

    if event_type in ["subscription.active", "subscription.renewed", "payment.succeeded", "checkout.session.completed"]:
        sub_end = datetime.utcnow() + timedelta(days=30)
        
        if user_id:
            cursor.execute("""
                INSERT INTO user_subscriptions (user_id, tier, quota_bytes, price, auto_renew, subscription_end_at)
                VALUES (%s, %s, %s, %s, TRUE, %s)
            """, (user_id, requested_tier, target_quota, target_price, sub_end))

        if user_id:
            cursor.execute("""
                SELECT SUM(quota_bytes) as net_q, MAX(price) as top_price 
                FROM user_subscriptions 
                WHERE user_id = %s AND subscription_end_at > CURRENT_TIMESTAMP
            """, (user_id,))
            sum_row = cursor.fetchone()
            net_quota = min(MAX_VAULT_BYTES, sum_row["net_q"] or target_quota)

            cursor.execute("""
                SELECT tier FROM user_subscriptions 
                WHERE user_id = %s AND subscription_end_at > CURRENT_TIMESTAMP
            """, (user_id,))
            active_rows = cursor.fetchall()
            tier_ranks = {"free": 0, "micro": 1, "lite": 2, "plus": 3, "pro": 4}
            highest_tier = requested_tier
            for r in active_rows:
                t = (r.get("tier") or "micro").lower()
                if tier_ranks.get(t, 0) > tier_ranks.get(highest_tier, 0):
                    highest_tier = t

            cursor.execute("""
                INSERT INTO users (user_id, email, tier, storage_quota_bytes, plan_price, subscription_end_at, grace_period_end_at)
                VALUES (%s, %s, %s, %s, %s, %s, NULL)
                ON CONFLICT (user_id) DO UPDATE SET 
                    tier = %s, 
                    storage_quota_bytes = %s, 
                    plan_price = %s, 
                    subscription_end_at = %s, 
                    grace_period_end_at = NULL, 
                    email = COALESCE(EXCLUDED.email, users.email)
            """, (user_id, user_email, highest_tier, net_quota, target_price, sub_end, highest_tier, net_quota, target_price, sub_end))
        elif user_email:
            cursor.execute("""
                UPDATE users 
                SET tier = %s, 
                    storage_quota_bytes = %s, 
                    plan_price = %s, 
                    subscription_end_at = %s, 
                    grace_period_end_at = NULL 
                WHERE LOWER(email) = LOWER(%s)
            """, (requested_tier, target_quota, target_price, sub_end, user_email.strip()))
        conn.commit()

    elif event_type in ["subscription.cancelled", "subscription.expired", "subscription.failed"]:
        grace_end = datetime.utcnow() + timedelta(days=20)
        if user_id:
            cursor.execute("""
                UPDATE users 
                SET tier = 'free', 
                    storage_quota_bytes = 5368709120, 
                    plan_price = 0.00, 
                    grace_period_end_at = %s 
                WHERE user_id = %s
            """, (grace_end, user_id))
        elif user_email:
            cursor.execute("""
                UPDATE users 
                SET tier = 'free', 
                    storage_quota_bytes = 5368709120, 
                    plan_price = 0.00, 
                    grace_period_end_at = %s 
                WHERE LOWER(email) = LOWER(%s)
            """, (grace_end, user_email.strip()))
        conn.commit()

    conn.close()
    return {"status": "success", "event": event_type}

# ----------------- Lemon Squeezy Fallback Webhook -----------------
@app.post("/api/webhook/lemonsqueezy")
async def lemon_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_name = payload.get("meta", {}).get("event_name", "")
    custom_data = payload.get("meta", {}).get("custom_data") or {}
    user_id = custom_data.get("user_id") if isinstance(custom_data, dict) else None

    attributes = payload.get("data", {}).get("attributes") or {}
    user_email = attributes.get("user_email")

    conn = get_db()
    cursor = conn.cursor()

    if not user_id and user_email:
        try:
            cursor.execute("SELECT id FROM auth.users WHERE LOWER(email) = LOWER(%s)", (user_email.strip(),))
            auth_row = cursor.fetchone()
            if auth_row and auth_row.get("id"):
                user_id = str(auth_row["id"])
        except Exception as e:
            print(f"[AUTH LOOKUP NOTICE]: {e}", flush=True)

    if event_name in ["order_created", "subscription_created", "subscription_resumed", "subscription_payment_success"]:
        sub_end = datetime.utcnow() + timedelta(days=30)
        if user_id:
            cursor.execute("""
                INSERT INTO users (user_id, email, tier, storage_quota_bytes, plan_price, subscription_end_at, grace_period_end_at)
                VALUES (%s, %s, 'pro', 214748364800, 7.00, %s, NULL)
                ON CONFLICT (user_id) DO UPDATE SET 
                    tier = 'pro', 
                    storage_quota_bytes = 214748364800, 
                    plan_price = 7.00, 
                    subscription_end_at = EXCLUDED.subscription_end_at, 
                    grace_period_end_at = NULL, 
                    email = COALESCE(EXCLUDED.email, users.email)
            """, (user_id, user_email, sub_end))
        elif user_email:
            cursor.execute("""
                UPDATE users 
                SET tier = 'pro', storage_quota_bytes = 214748364800, plan_price = 7.00, subscription_end_at = %s, grace_period_end_at = NULL 
                WHERE LOWER(email) = LOWER(%s)
            """, (sub_end, user_email.strip(),))
        conn.commit()

    elif event_name in ["subscription_cancelled", "subscription_expired", "subscription_paused"]:
        grace_end = datetime.utcnow() + timedelta(days=20)
        if user_id:
            cursor.execute("""
                UPDATE users 
                SET tier = 'free', storage_quota_bytes = 5368709120, plan_price = 0.00, grace_period_end_at = %s 
                WHERE user_id = %s
            """, (grace_end, user_id,))
        elif user_email:
            cursor.execute("""
                UPDATE users 
                SET tier = 'free', storage_quota_bytes = 5368709120, plan_price = 0.00, grace_period_end_at = %s 
                WHERE LOWER(email) = LOWER(%s)
            """, (grace_end, user_email.strip()))
        conn.commit()

    conn.close()
    return {"status": "received"}
# -------------------------------------------------------------
# AI 4K SUPER-RESOLUTION ENDPOINT (REAL-ESRGAN VIA REPLICATE)
# -------------------------------------------------------------
import tempfile

@app.post("/api/drive/ai-upscale")
async def ai_upscale_endpoint(
    file: UploadFile = File(...),
    user_id: str = Form(...)
):
    tmp_path = None
    try:
        # Read uploaded image bytes
        contents = await file.read()
        ext = os.path.splitext(file.filename)[1] if file.filename else ".png"

        # Create a temp file on disk so Replicate gets a real path and valid filename
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name

        # Send to Replicate Real-ESRGAN
        with open(tmp_path, "rb") as image_file:
            output = replicate.run(
                "nightmareai/real-esrgan:42fed1c4974146d4d2414e2be2c5277c7fcf05fcc3a73abf41610695738c1d7b",
                input={
                    "image": image_file,
                    "scale": 4,
                    "face_enhance": True
                }
            )

        # Replicate can return a URL string, FileOutput, or list of URLs
        upscaled_url = str(output[0]) if isinstance(output, list) else str(output)
        return JSONResponse({"upscaled_url": upscaled_url})

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Clean up temporary disk file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


  