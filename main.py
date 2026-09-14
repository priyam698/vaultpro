import os
import sys
import uuid
import time
import math
import random
import secrets
import hashlib
import hmac
import json
import urllib.request
import urllib.parse
import traceback
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import boto3
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

SUPERSONIC_FAVICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='rc' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='#38bdf8'/><stop offset='60%' stop-color='#6366f1'/><stop offset='100%' stop-color='#4338ca'/></linearGradient><linearGradient id='rv' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='#c084fc'/><stop offset='50%' stop-color='#818cf8'/><stop offset='100%' stop-color='#06b6d4'/></linearGradient><linearGradient id='gs' x1='0%' y1='0%' x2='0%' y2='100%'><stop offset='0%' stop-color='#ffffff' stop-opacity='0.85'/><stop offset='100%' stop-color='#ffffff' stop-opacity='0'/></linearGradient></defs><path d='M18 22C36 16 74 16 86 22C70 32 40 34 18 34Z' fill='url(#rc)'/><path d='M18 22C36 16 74 16 86 22L78 26C66 21 34 21 18 26Z' fill='url(#gs)'/><path d='M86 22L30 76L46 76L86 34Z' fill='url(#rv)'/><path d='M14 78C26 68 58 66 82 76C66 84 32 84 14 78Z' fill='url(#rc)'/><path d='M14 78C28 72 60 72 82 76L76 80C58 76 28 76 14 81Z' fill='url(#gs)'/></svg>"""

app = FastAPI(
    title="Zephyr Drive & Transfer API",
    version="2.6.0",
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;"
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        share_migrations = [
            "ALTER TABLE shares ADD COLUMN IF NOT EXISTS is_paywalled BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE shares ADD COLUMN IF NOT EXISTS unlock_price NUMERIC(10, 2) DEFAULT 0.00;",
            "ALTER TABLE shares ADD COLUMN IF NOT EXISTS paywall_creator_id VARCHAR(120);"
        ]
        for sm in share_migrations:
            try:
                cursor.execute(sm)
            except Exception:
                pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS paywall_purchases (
                id SERIAL PRIMARY KEY,
                share_id VARCHAR(64) REFERENCES shares(id) ON DELETE CASCADE,
                buyer_email VARCHAR(255) NOT NULL,
                access_token VARCHAR(64) UNIQUE NOT NULL,
                amount_paid NUMERIC(10, 2) NOT NULL,
                payment_status VARCHAR(30) DEFAULT 'unpaid',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                unlocked_at TIMESTAMP
            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_paywall_lookup 
            ON paywall_purchases (share_id, buyer_email, payment_status);
        """)

        cursor.close()
        conn.close()
        print("[DB STARTUP]: Schema verified, paywall escrow tables and columns synchronized.", flush=True)
    except Exception as e:
        print(f"[DB STARTUP ERROR]: {e}", flush=True)

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
DODO_WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "").strip()

otp_storage = {}

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

class CreateShareRequest(BaseModel):
    filename: str
    filesize_mb: float
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

ZEPHYR_SYSTEM_KNOWLEDGE = """
You are Zephyr Copilot, the friendly and authoritative AI assistant for Zephyr Vault.
Your job is to answer user questions in simple, easy-to-understand language while covering all technical specifics accurately.

Rules:
1. Explain clearly like talking to a helpful peer. Keep answers direct, friendly, and easily actionable (2 to 4 sentences max unless detailed step-by-step instructions are required).
2. If the user asks for human support, needs developer escalation, or encounters a bug, direct them to Priyam Rana at priyamrana069@gmail.com.

Platform Knowledge & Pricing Tiers:
- Physical Storage Location: Files are securely hosted on Cloudflare R2's global edge network with zero egress costs.
- Security & Encryption: End-to-end client-side AES-256 encryption. We never hold your passcodes or private keys on our servers.
- Free Starter Tier: $0 forever. Includes 5 GB permanent Cloud Drive storage, 2 GB single transfers, and 7 E-Sign documents per day.
- Zephyr Micro Tier: $1.80/month. Includes 15 GB permanent Cloud Drive storage, 5 GB single transfers, and 15 E-Sign documents per day.
- Zephyr Lite Tier: $2.50/month. Includes 30 GB permanent Cloud Drive storage, 10 GB single transfers, and 30 E-Sign documents per day.
- Zephyr Plus Tier: $4.50/month. Includes 80 GB permanent Cloud Drive storage, 25 GB single transfers, 50 E-Sign documents per day, and Studio Branding.
- Zephyr Pro Tier: $7.00/month. Includes 200 GB permanent Cloud Drive vault, 50 GB single transfers, unlimited daily E-Sign documents, and Studio Branding.
- Studio Branding: Plus and Pro users can customize client transfer backgrounds, add custom studio logos, and choose custom theme colors.
- Pay-to-Unlock Transfers: Creators can attach invoice prices to shared files. In group shares, each buyer unlocks their own unique access token without compromising group access. Protected files feature forensic moving watermarks and anti-screenshot shielding.
- 20-Day Grace Period: If a plan expires or cancels, accounts enter a 20-day read-only grace period. After 20 days, files exceeding the active plan limit are pruned starting from the oldest uploaded files.
- Mid-Cycle Upgrades: Users can upgrade plans mid-cycle. The charge is prorated for the remaining days of their billing cycle plus a $0.50 upgrade fee.
- Burn-on-Read: If set to 1 download under Security settings, the file on Cloudflare R2 is shredded the exact millisecond the recipient finishes downloading it.
"""

@app.post("/api/support/chat")
async def support_chat(req: SupportChatRequest):
    user_msg = req.message.strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="Empty query.")

    grok_key = (os.getenv("GROK_API_KEY", "") or os.getenv("XAI_API_KEY", "")).strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    if grok_key:
        try:
            url = "https://api.x.ai/v1/chat/completions"
            payload = {
                "model": "grok-beta",
                "messages": [
                    {"role": "system", "content": ZEPHYR_SYSTEM_KNOWLEDGE},
                    {"role": "user", "content": user_msg}
                ],
                "temperature": 0.3
            }
            http_req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {grok_key}", "Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {"reply": data["choices"][0]["message"]["content"]}
        except Exception as e:
            print(f"[COPILOT GROK ERROR]: {e}", flush=True)

    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
            payload = {
                "system_instruction": {"parts": [{"text": ZEPHYR_SYSTEM_KNOWLEDGE}]},
                "contents": [{"role": "user", "parts": [{"text": user_msg}]}]
            }
            http_req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {"reply": data["candidates"][0]["content"]["parts"][0]["text"]}
        except Exception as e:
            print(f"[COPILOT GEMINI ERROR]: {e}", flush=True)

    if openai_key:
        try:
            url = "https://api.openai.com/v1/chat/completions"
            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": ZEPHYR_SYSTEM_KNOWLEDGE},
                    {"role": "user", "content": user_msg}
                ],
                "max_tokens": 250
            }
            http_req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {"reply": data["choices"][0]["message"]["content"]}
        except Exception as e:
            print(f"[COPILOT OPENAI ERROR]: {e}", flush=True)

    q = user_msg.lower()
    if any(k in q for k in ["where", "physical", "physically", "store", "stored", "server", "location", "r2", "cloudflare"]):
        reply = "Your files are stored on Cloudflare R2's global edge network. Because Zephyr uses zero-knowledge encryption, your files are encrypted locally on your device first—meaning no one, not even server hosts, can see what's inside."
    elif any(k in q for k in ["pay", "escrow", "paywall", "bounty", "unlock"]):
        reply = "Zephyr Pay-to-Unlock allows creators to monetize deliveries. When sent to a group, each recipient purchases their own individual access token. Media is served inside a protected viewer with moving forensic watermarks and focus-loss anti-screenshot shielding."
    elif any(k in q for k in ["pricing", "price", "cost", "plan", "upgrade", "subscription", "micro", "lite", "plus", "pro"]):
        reply = "Zephyr offers 5 tiers:\n• Free Starter ($0): 5 GB vault, 2 GB transfers, 7 E-Signs/day\n• Micro ($1.80/mo): 15 GB vault, 5 GB transfers, 15 E-Signs/day\n• Lite ($2.50/mo): 30 GB vault, 10 GB transfers, 30 E-Signs/day\n• Plus ($4.50/mo): 80 GB vault, 25 GB transfers, 50 E-Signs/day, and Studio Branding\n• Pro ($7.00/mo): 200 GB vault, 50 GB transfers, unlimited E-Signs, and Studio Branding."
    elif any(k in q for k in ["brand", "branding", "logo", "wallpaper", "customization"]):
        reply = "Studio Branding is available exclusively on our Plus ($4.50/mo) and Pro ($7.00/mo) tiers. It lets you customize public transfer backgrounds, showcase your studio logo, and set custom accent colors."
    elif any(k in q for k in ["esign", "e-sign", "signature", "limit", "daily"]):
        reply = "Daily E-Sign document creation limits: Free Starter (7/day), Micro (15/day), Lite (30/day), Plus (50/day), and Pro (Unlimited). Limits reset every night at midnight."
    elif any(k in q for k in ["grace", "expire", "expiration", "20 day", "prune", "delete files"]):
        reply = "If your plan lapses, your account enters a 20-day read-only grace period. During these 20 days, you can renew or download your files. After 20 days, any data exceeding your current plan limit will be automatically deleted starting from the oldest files."
    elif any(k in q for k in ["burn", "shred", "destroy", "self-destruct"]):
        reply = "When you set '1 (Burn on Read 🔥)' under Security, the file on Cloudflare R2 is shredded the second your recipient finishes downloading it. After that, the link is destroyed permanently."
    else:
        reply = "I'm here to help with Zephyr transfers, storage vaults, E-Sign, paywall escrow, and privacy features. If you need dedicated human support, feel free to email Priyam Rana at priyamrana069@gmail.com!"

    return {"reply": reply}

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

@app.get("/sign", response_class=HTMLResponse)
async def sign_page(request: Request):
    return render_template("sign.html", request)

@app.get("/secure-view/{share_id}", response_class=HTMLResponse)
async def secure_viewer_page(request: Request, share_id: str, token: str = Query(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.buyer_email, p.payment_status, s.filename 
        FROM paywall_purchases p
        JOIN shares s ON s.id = p.share_id
        WHERE p.share_id = %s AND p.access_token = %s AND p.payment_status = 'paid'
    """, (share_id, token))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=403, detail="Unauthorized access. Payment required to view this secured file.")

    return render_template("secure_viewer.html", request, {
        "share_id": share_id,
        "filename": row["filename"],
        "buyer_email": row["buyer_email"],
        "token": token
    })

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
    cursor.execute("SELECT tier FROM users WHERE user_id = %s", (data.user_id,))
    user = cursor.fetchone()

    if not user:
        cursor.execute("INSERT INTO users (user_id, tier) VALUES (%s, 'free') RETURNING *", (data.user_id,))
        conn.commit()
        user = cursor.fetchone()
    
    tier = (user.get("tier") or "free").lower()
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
        raise HTTPException(status_code=400, detail="Invalid asset_type. Must be 'logo' or 'background'.")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT tier FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()

    if not user or (user.get("tier") or "").lower() not in ["plus", "pro"]:
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
        cursor.execute("SELECT tier FROM users WHERE user_id = %s", (user_id,))
        u = cursor.fetchone()
        if u and u.get("tier"):
            user_tier = u["tier"].lower()
    elif email:
        cursor.execute("SELECT tier FROM users WHERE LOWER(email) = LOWER(%s)", (email.strip(),))
        u = cursor.fetchone()
        if u and u.get("tier"):
            user_tier = u["tier"].lower()

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
        cursor.execute("SELECT email, tier FROM users WHERE user_id = %s", (user_id,))
        u = cursor.fetchone()
        if u:
            if u.get("email") and not resolved_creator:
                resolved_creator = u["email"]
            if u.get("tier"):
                user_tier = u["tier"].lower()
    elif resolved_creator:
        cursor.execute("SELECT tier FROM users WHERE LOWER(email) = LOWER(%s)", (resolved_creator,))
        u = cursor.fetchone()
        if u and u.get("tier"):
            user_tier = u["tier"].lower()

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
    conn.close()

    if not user:
        return {
            "tier": "free",
            "used_bytes": 0,
            "quota_bytes": 5368709120,
            "plan_price": 0.00,
            "subscription_end_at": None,
            "grace_period_end_at": None
        }

    tier_key = (user.get("tier") or "free").lower()
    default_quota = PLAN_CONFIG.get(tier_key, {}).get("quota", 5368709120)
    quota = user.get("storage_quota_bytes") or default_quota

    return {
        "tier": tier_key,
        "used_bytes": user.get("storage_used_bytes", 0) or 0,
        "quota_bytes": quota,
        "plan_price": float(user.get("plan_price") or 0.0),
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

    cursor.execute("SELECT tier, storage_used_bytes, storage_quota_bytes FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()

    if not user:
        cursor.execute("INSERT INTO users (user_id, tier, storage_used_bytes, storage_quota_bytes) VALUES (%s, 'free', 0, 5368709120)", (user_id,))
        conn.commit()
        used = 0
        quota = 5368709120
    else:
        used = user.get("storage_used_bytes") or 0
        tier_key = (user.get("tier") or "free").lower()
        default_quota = PLAN_CONFIG.get(tier_key, {}).get("quota", 5368709120)
        quota = user.get("storage_quota_bytes") or default_quota

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

    cursor.execute("SELECT tier, storage_used_bytes, storage_quota_bytes, email FROM users WHERE user_id = %s", (owner_id,))
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
        tier_key = (owner.get("tier") or "free").lower()
        default_quota = PLAN_CONFIG.get(tier_key, {}).get("quota", 5368709120)
        quota = owner.get("storage_quota_bytes") or default_quota
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
        conn.close()
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

# ----------------- Mid-Cycle Prorated Upgrades -----------------
@app.post("/api/drive/upgrade-quote")
async def calculate_prorated_upgrade(payload: UpgradeQuoteRequest):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT tier, plan_price, subscription_end_at FROM users WHERE user_id = %s", (payload.user_id,))
    user = cursor.fetchone()
    conn.close()

    target_tier_key = payload.target_tier.lower().strip()
    target = PLAN_CONFIG.get(target_tier_key)
    if not target or target_tier_key == "free":
        raise HTTPException(status_code=400, detail="Invalid target plan.")

    current_price = float(user.get("plan_price") or 0.0) if user else 0.0
    sub_end = user.get("subscription_end_at") if user else None

    if not sub_end or current_price <= 0.0:
        return {
            "target_tier": target_tier_key,
            "target_name": target["name"],
            "charge_amount": target["price"],
            "days_remaining": 0,
            "surcharge": 0.00,
            "target_quota_bytes": target["quota"]
        }

    now = datetime.utcnow()
    if isinstance(sub_end, str):
        sub_end = datetime.fromisoformat(sub_end)

    days_remaining = max(0, (sub_end - now).days)
    if days_remaining <= 0:
        return {
            "target_tier": target_tier_key,
            "target_name": target["name"],
            "charge_amount": target["price"],
            "days_remaining": 0,
            "surcharge": 0.00,
            "target_quota_bytes": target["quota"]
        }

    price_diff = max(0.0, target["price"] - current_price)
    prorated_base = (price_diff / 30.0) * days_remaining
    final_amount = round(prorated_base + 0.50, 2)

    return {
        "target_tier": target_tier_key,
        "target_name": target["name"],
        "charge_amount": final_amount,
        "days_remaining": days_remaining,
        "surcharge": 0.50,
        "target_quota_bytes": target["quota"]
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

# ----------------- Ephemeral Transfers & Paywall Escrow -----------------
@app.get("/share/{share_id}", response_class=HTMLResponse)
async def share_page(request: Request, share_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Vault transfer link not found or expired.")

    max_downloads = row.get("max_downloads", 0) or 0
    if max_downloads > 0 and row["downloads"] >= max_downloads:
        conn.close()
        raise HTTPException(status_code=410, detail="This link reached its maximum download limit and was shredded.")

    if row["expiry_hours"] != 0:
        expires_at = row["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if datetime.utcnow().astimezone() > expires_at:
            conn.close()
            raise HTTPException(status_code=410, detail="This vault link has expired.")

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

    return render_template("download.html", request, {
        "share_id": share_id,
        "filename": row["filename"],
        "filesize": row["filesize_mb"],
        "downloads": row["downloads"],
        "has_password": bool(row["password_hash"]),
        "is_paywalled": bool(row.get("is_paywalled", False)),
        "unlock_price": float(row.get("unlock_price", 0.00) or 0.00),
        "branding": branding
    })

@app.get("/api/share-details/{share_id}")
async def get_share_details(share_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="File share expired or purged.")

    if row["expiry_hours"] != 0:
        expires_at = row["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if datetime.utcnow().astimezone() > expires_at:
            conn.close()
            raise HTTPException(status_code=410, detail="Transfer link has expired.")

    branding = None
    if row.get("user_id"):
        cursor.execute("""
            SELECT tier, brand_title, brand_logo_url, brand_bg_url, brand_accent_color 
            FROM users WHERE user_id = %s
        """, (row["user_id"],))
        u_brand = cursor.fetchone()
        if u_brand and (u_brand.get("tier") or "").lower() in ["plus", "pro"]:
            branding = dict(u_brand)

    conn.close()

    return {
        "share_id": row["id"],
        "filename": row["filename"],
        "filesize_mb": float(row["filesize_mb"]),
        "has_password": bool(row["password_hash"]),
        "is_paywalled": bool(row.get("is_paywalled", False)),
        "unlock_price": float(row.get("unlock_price", 0.00) or 0.00),
        "sender_branding": branding
    }

@app.post("/api/create-share")
async def create_share(payload: CreateShareRequest):
    conn = get_db()
    cursor = conn.cursor()
    user_tier = "free"
    if payload.user_id:
        cursor.execute("SELECT tier FROM users WHERE user_id = %s", (payload.user_id,))
        user_row = cursor.fetchone()
        if user_row and user_row.get("tier"):
            user_tier = user_row["tier"].lower()

    max_mb = PLAN_CONFIG.get(user_tier, {}).get("single_mb", 2048)
    if payload.filesize_mb > max_mb:
        conn.close()
        raise HTTPException(status_code=400, detail=f"File exceeds the {max_mb} MB limit for your {user_tier.upper()} tier.")

    created_at = datetime.utcnow()
    expires_at = created_at + timedelta(days=36500) if payload.expiry_hours == 0 else created_at + timedelta(hours=payload.expiry_hours)
    share_id = uuid.uuid4().hex[:8]
    s3_key = f"transfers/{share_id}/{payload.filename}"
    password_hash = hashlib.sha256(payload.password.encode()).hexdigest() if payload.password else None

    cursor.execute("""
        INSERT INTO shares (
            id, filename, filesize_mb, s3_key, password_hash, expiry_hours, 
            max_downloads, created_at, expires_at, downloads, user_id, 
            is_paywalled, unlock_price, paywall_creator_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s)
    """, (
        share_id, payload.filename, payload.filesize_mb, s3_key, password_hash, 
        payload.expiry_hours, payload.max_downloads, created_at, expires_at, 
        payload.user_id, payload.is_paywalled, payload.unlock_price, payload.user_id
    ))
    conn.commit()
    conn.close()

    return {"share_id": share_id, "expires_at": expires_at.isoformat()}

@app.post("/api/upload-file/{share_id}")
async def upload_file_direct(share_id: str, request: Request):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Vault record not found.")

    file_bytes = await request.body()
    s3_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=row["s3_key"],
        Body=file_bytes,
        ContentType=request.headers.get("content-type", "application/octet-stream")
    )
    return {"status": "success", "share_id": share_id}

# ----------------- Group Paywall Multi-Buyer Escrow Engine -----------------
@app.post("/api/paywall/initiate")
async def initiate_paywall_checkout(payload: InitiatePaywallRequest):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM shares WHERE id = %s AND is_paywalled = TRUE", (payload.share_id,))
    share = cursor.fetchone()
    if not share:
        conn.close()
        raise HTTPException(status_code=404, detail="Paywalled transfer session not found or inactive.")

    buyer = payload.buyer_email.lower().strip()

    cursor.execute("""
        SELECT access_token FROM paywall_purchases 
        WHERE share_id = %s AND LOWER(buyer_email) = %s AND payment_status = 'paid'
    """, (payload.share_id, buyer))
    existing = cursor.fetchone()

    if existing:
        conn.close()
        return {
            "status": "already_unlocked",
            "access_token": existing["access_token"],
            "viewer_url": f"/secure-view/{payload.share_id}?token={existing['access_token']}"
        }

    access_token = f"pwtk_{secrets.token_hex(20)}"
    price = float(share.get("unlock_price") or 0.00)

    cursor.execute("""
        INSERT INTO paywall_purchases (share_id, buyer_email, access_token, amount_paid, payment_status)
        VALUES (%s, %s, %s, %s, 'pending')
        RETURNING id
    """, (payload.share_id, buyer, access_token, price))
    conn.commit()
    conn.close()

    dodo_product_id = os.getenv("DODO_PAYWALL_PRODUCT_ID", "pdt_0NrnVvaQ9xDPDhpjrniuR3A")
    checkout_url = (
        f"https://checkout.dodopayments.com/buy/{dodo_product_id}"
        f"?email={urllib.parse.quote(buyer)}"
        f"&metadata_share_id={payload.share_id}"
        f"&metadata_access_token={access_token}"
        f"&metadata_buyer_email={urllib.parse.quote(buyer)}"
    )

    return {
        "status": "checkout_ready",
        "checkout_url": checkout_url,
        "access_token": access_token
    }

@app.get("/api/paywall/verify-access")
async def verify_buyer_access(share_id: str = Query(...), access_token: str = Query(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT buyer_email, payment_status 
        FROM paywall_purchases 
        WHERE share_id = %s AND access_token = %s AND payment_status = 'paid'
    """, (share_id, access_token))
    purchase = cursor.fetchone()
    conn.close()

    if not purchase:
        raise HTTPException(status_code=403, detail="Payment required to access this file.")

    return {"unlocked": True, "buyer_email": purchase["buyer_email"]}

@app.get("/api/paywall/stream/{share_id}")
async def stream_paywall_media(share_id: str, token: str = Query(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.buyer_email, s.s3_key, s.filename 
        FROM paywall_purchases p
        JOIN shares s ON s.id = p.share_id
        WHERE p.share_id = %s AND p.access_token = %s AND p.payment_status = 'paid'
    """, (share_id, token))
    item = cursor.fetchone()
    conn.close()

    if not item:
        raise HTTPException(status_code=403, detail="Protected content stream access denied.")

    try:
        obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=item["s3_key"])
        content_type = obj.get("ContentType", "application/octet-stream")
        return StreamingResponse(
            obj["Body"].iter_chunks(),
            media_type=content_type,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, private",
                "Content-Disposition": f'inline; filename="{item["filename"]}"'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Streaming pipe error: {str(e)}")

@app.post("/share/{share_id}/download")
@app.post("/api/download/{share_id}")
async def process_download(share_id: str, payload: Optional[DownloadPayload] = None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Share link not found or expired.")

    # Enforce Individual Multi-Buyer Escrow Lock
    if row.get("is_paywalled"):
        token = payload.access_token if payload else None
        if not token:
            conn.close()
            raise HTTPException(status_code=402, detail="Payment required. Each recipient must unlock their personal access token.")

        cursor.execute("""
            SELECT id FROM paywall_purchases 
            WHERE share_id = %s AND access_token = %s AND payment_status = 'paid'
        """, (share_id, token))
        verified = cursor.fetchone()
        if not verified:
            conn.close()
            raise HTTPException(status_code=403, detail="Invalid or unpaid access token.")

    max_downloads = row.get("max_downloads", 0) or 0
    if max_downloads > 0 and row["downloads"] >= max_downloads:
        conn.close()
        raise HTTPException(status_code=410, detail="Link reached its maximum download count.")

    if row["password_hash"]:
        user_pass = payload.password if payload else None
        if not user_pass or hashlib.sha256(user_pass.encode()).hexdigest() != row["password_hash"]:
            conn.close()
            raise HTTPException(status_code=401, detail="Incorrect passcode.")

    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": row["s3_key"], "ResponseContentDisposition": f'attachment; filename="{row["filename"]}"'},
        ExpiresIn=3600
    )
    new_count = row["downloads"] + 1
    cursor.execute("UPDATE shares SET downloads = %s WHERE id = %s", (new_count, share_id))
    conn.commit()

    if max_downloads > 0 and new_count >= max_downloads:
        try:
            s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=row["s3_key"])
        except Exception:
            pass

    conn.close()
    return {"download_url": url}

@app.get("/api/user-profile")
async def get_user_profile(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {"tier": "free", "user_id": user_id}
    return {
        "tier": row.get("tier", "free"),
        "email": row.get("email"),
        "user_id": row.get("user_id"),
        "brand_title": row.get("brand_title"),
        "brand_slug": row.get("brand_slug"),
        "brand_logo_url": row.get("brand_logo_url"),
        "brand_bg_url": row.get("brand_bg_url"),
        "brand_accent_color": row.get("brand_accent_color") or "#6366f1",
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

    webhook_signature = request.headers.get("webhook-signature") or request.headers.get("x-dodo-signature")
    if DODO_WEBHOOK_SECRET and webhook_signature:
        expected = hmac.new(DODO_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, webhook_signature.replace("sha256=", "")):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = payload.get("type", "")
    data_block = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    metadata = data_block.get("metadata") or payload.get("metadata") or {}

    conn = get_db()
    cursor = conn.cursor()

    # Handle Individual Paywall Escrow Unlocks (Multi-Buyer Group Links)
    paywall_token = metadata.get("access_token") or metadata.get("metadata_access_token")
    if paywall_token and event_type in ["payment.succeeded", "checkout.session.completed"]:
        cursor.execute("""
            UPDATE paywall_purchases 
            SET payment_status = 'paid', unlocked_at = CURRENT_TIMESTAMP 
            WHERE access_token = %s
        """, (paywall_token,))
        conn.commit()
        conn.close()
        print(f"[DODO PAYWALL UNLOCKED] Activated purchase token: {paywall_token}", flush=True)
        return {"status": "success", "paywall_token": paywall_token}

    user_id = metadata.get("user_id") or metadata.get("userId") or metadata.get("metadata_user_id")
    customer_info = data_block.get("customer") or payload.get("customer") or {}
    user_email = (
        (customer_info.get("email") if isinstance(customer_info, dict) else None)
        or data_block.get("customer_email")
        or payload.get("customer_email")
        or metadata.get("email")
    )

    requested_tier = (metadata.get("tier") or metadata.get("plan") or "pro").lower().strip()
    if requested_tier not in PLAN_CONFIG or requested_tier == "free":
        requested_tier = "pro"

    tier_info = PLAN_CONFIG[requested_tier]
    target_quota = tier_info["quota"]
    target_price = tier_info["price"]

    print(f"[DODO WEBHOOK] Event: {event_type} | Email: {user_email} | User ID: {user_id} | Tier: {requested_tier}", flush=True)

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
                INSERT INTO users (user_id, email, tier, storage_quota_bytes, plan_price, subscription_end_at, grace_period_end_at)
                VALUES (%s, %s, %s, %s, %s, %s, NULL)
                ON CONFLICT (user_id) DO UPDATE SET 
                    tier = EXCLUDED.tier, 
                    storage_quota_bytes = EXCLUDED.storage_quota_bytes,
                    plan_price = EXCLUDED.plan_price,
                    subscription_end_at = EXCLUDED.subscription_end_at,
                    grace_period_end_at = NULL,
                    email = COALESCE(EXCLUDED.email, users.email)
            """, (user_id, user_email, requested_tier, target_quota, target_price, sub_end))
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
        print(f"[TIER ACTIVATED]: Provisioned {tier_info['name']} ({target_quota / (1024**3):.0f} GB) for {user_email or user_id}", flush=True)

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
        print(f"[GRACE PERIOD ACTIVATED]: 20-day countdown started for {user_email or user_id}", flush=True)

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
            """, (grace_end, user_email.strip(),))
        conn.commit()

    conn.close()
    return {"status": "received"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)