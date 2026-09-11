import os
import uuid
import time
import math
import random
import hashlib
import json
import urllib.request
import traceback
from datetime import datetime, timedelta
from typing import Optional

import boto3
from botocore.config import Config
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import psycopg2
from psycopg2.extras import RealDictCursor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Supersonic Monogram SVG Asset
SUPERSONIC_FAVICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='rc' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='#38bdf8'/><stop offset='60%' stop-color='#6366f1'/><stop offset='100%' stop-color='#4338ca'/></linearGradient><linearGradient id='rv' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='#c084fc'/><stop offset='50%' stop-color='#818cf8'/><stop offset='100%' stop-color='#06b6d4'/></linearGradient><linearGradient id='gs' x1='0%' y1='0%' x2='0%' y2='100%'><stop offset='0%' stop-color='#ffffff' stop-opacity='0.85'/><stop offset='100%' stop-color='#ffffff' stop-opacity='0'/></linearGradient></defs><path d='M18 22C36 16 74 16 86 22C70 32 40 34 18 34Z' fill='url(#rc)'/><path d='M18 22C36 16 74 16 86 22L78 26C66 21 34 21 18 26Z' fill='url(#gs)'/><path d='M86 22L30 76L46 76L86 34Z' fill='url(#rv)'/><path d='M14 78C26 68 58 66 82 76C66 84 32 84 14 78Z' fill='url(#rc)'/><path d='M14 78C28 72 60 72 82 76L76 80C58 76 28 76 14 81Z' fill='url(#gs)'/></svg>"""

app = FastAPI(
    title="Zephyr Drive & Transfer API",
    version="2.0.0",
    swagger_favicon_url="/favicon.ico"
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

@app.on_event("startup")
def init_db_schema():
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signature_requests (
                doc_id VARCHAR(32) PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                recipient_name VARCHAR(255) NOT NULL,
                recipient_email VARCHAR(255) NOT NULL,
                status VARCHAR(32) DEFAULT 'pending',
                signature_x INT DEFAULT 150,
                signature_y INT DEFAULT 250,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        cursor.execute("ALTER TABLE signature_requests ADD COLUMN IF NOT EXISTS creator_email VARCHAR(255);")
        cursor.execute("ALTER TABLE signature_requests ADD COLUMN IF NOT EXISTS user_id VARCHAR(64);")
        conn.commit()
        conn.close()
        print("[DB STARTUP]: Signature schema verified and migration applied.", flush=True)
    except Exception as e:
        print(f"[DB STARTUP WARNING]: {e}", flush=True)

# Cloudflare R2 Config
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "").strip()
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "vault-storage-backend").strip()

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

# ----------------- OTP Verification Engine (Brevo HTTP API + Rate Limiter) -----------------
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

    print(f"\n[ZEPHYR LIVE OTP FOR {target_email}]: {code} (Valid for 3 mins)\n", flush=True)

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
        print(f"[BREVO API ERROR]: {e}")
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
        print(f"[RECIPIENT NOTIFICATION ERROR]: {e}")
        raise HTTPException(status_code=500, detail=f"Files uploaded, but failed to email recipient: {str(e)}")

    return {"status": "dispatched", "recipient": req.recipient_email}

# ----------------- Zephyr Copilot Comprehensive Knowledge & AI Support -----------------
class SupportChatRequest(BaseModel):
    message: str
    history: Optional[list] = []

ZEPHYR_SYSTEM_KNOWLEDGE = """
You are Zephyr Copilot, the friendly and authoritative AI assistant for Zephyr Vault.
Your job is to answer user questions in simple, easy-to-understand language while covering all technical specifics accurately.

Rules:
1. Explain clearly like talking to a helpful peer. Keep answers direct, friendly, and easily actionable (2 to 4 sentences max unless detailed step-by-step instructions are required).
2. If the user asks for human support, needs developer escalation, or encounters a bug, direct them to Priyam Rana at priyamrana069@gmail.com.

Platform Knowledge & Micro-Details:
- Physical Storage Location: Files are securely hosted on Cloudflare R2's global edge network. Because Zephyr uses zero-knowledge encryption, files are encrypted directly on your local device before transmission, so neither Zephyr nor Cloudflare can see or decrypt your data.
- Security & Encryption: End-to-end client-side AES-256 encryption. We never hold your passcodes or private keys on our servers.
- Free Starter Tier: $0 forever. Includes 5 GB permanent Cloud Drive storage and single transfers up to 2.00 GB (plus 4 free guest transfers before requiring free account registration).
- Zephyr Pro Tier: $4.00/month (billed via Lemon Squeezy). Includes 200 GB permanent Cloud Drive vault, 50 GB single transfers, extended retention windows (1h, 24h, 3d, 7d, 14d, 30d, or Never), and white-label download screens.
- Burn-on-Read: If set to 1 download under Security settings, the file on Cloudflare R2 is shredded and permanently deleted the exact millisecond the recipient finishes downloading it.
- Email Transfers & OTP: Senders verify with a 6-digit code sent to their email via Brevo. The code is valid for 3 minutes, with 4 allowed attempts before a 30-minute lockout.
- Client Deposit Portals: Created by clicking 'Request' in the header. Anyone with the drop link can upload files straight into your vault without an account. In drop-zone mode, your private drive and profile links are locked so depositors cannot view your personal files.
- E-Sign Studio (/sign): Allows users to sign PDFs and agreements by typing (with 4 cursive font styles: Caveat, Dancing Script, Great Vibes, Pacifico), drawing realistic ink signatures, or uploading image files. When the other party signs, you receive an automated email notification with the signed copy.
- Keep Me Signed In: Check the box to save your login in localStorage across browser restarts. Leave it unchecked to use temporary session storage, requiring you to sign in each new browser session.
- Password Reset: Users can click 'Forgot password?' on /auth to receive a password recovery link.
"""

@app.post("/api/support/chat")
async def support_chat(req: SupportChatRequest):
    user_msg = req.message.strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="Empty query.")

    grok_key = (os.getenv("GROK_API_KEY", "") or os.getenv("XAI_API_KEY", "")).strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    # 1. Live xAI Grok API
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

    # 2. Live Gemini AI
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

    # 3. Live OpenAI
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

    # 4. Comprehensive Easy-Language Deterministic Offline Engine
    q = user_msg.lower()

    if any(k in q for k in ["where", "physical", "physically", "store", "stored", "server", "location", "datacenter", "r2", "cloudflare"]):
        reply = "Your files are stored on Cloudflare R2's global edge network. Because Zephyr uses zero-knowledge encryption, your files are encrypted on your device first—meaning no one, not even the server hosts, can see what's inside."

    elif any(k in q for k in ["password", "reset", "forgot", "recovery", "change password"]):
        reply = "To reset your password, head to the Sign In page (/auth) and click 'Forgot password?'. Enter your email, and we'll send you a secure link to choose a new password. If you get stuck, contact human support at priyamrana069@gmail.com."

    elif any(k in q for k in ["pricing", "price", "cost", "free", "pro", "plan", "upgrade", "subscription", "$4", "limit", "storage", "quota", "10 gb", "gigabyte", "mb", "file size", "how big", "send a"]):
        reply = "Zephyr has two simple plans:\n• Free Starter ($0): 5 GB permanent cloud drive storage, up to 2.00 GB per single transfer, and Burn-on-Read shredding.\n• Zephyr Pro ($4/mo): 200 GB permanent drive storage, and single transfers up to 50 GB. If you are on the Free tier, a 10 GB file will exceed the 2 GB limit, so you would need to upgrade to Pro!"

    elif any(k in q for k in ["burn", "shred", "destroy", "self-destruct", "one-time", "delete link"]):
        reply = "Burn-on-Read completely erases your file. When you choose '1 (Burn on Read 🔥)' under Security, the file is deleted from our cloud servers the second your recipient finishes downloading it. After that, the link will never work again."

    elif any(k in q for k in ["safe", "security", "encrypt", "privacy", "hack", "zero-knowledge", "aes", "key"]):
        reply = "Zephyr is 100% zero-knowledge. Your files are locked with AES-256 encryption right inside your web browser before they are uploaded. We do not keep your passwords or keys, so even if our databases were breached, your files cannot be read."

    elif any(k in q for k in ["otp", "code", "verify", "verification", "lockout", "timer", "minutes", "brevo", "email"]):
        reply = "When you send files via email, we send a 6-digit verification code to prove it's really you. The code stays valid for 3 minutes. If you type the wrong code 4 times in a row, the system locks transfers for 30 minutes to prevent spam."

    elif any(k in q for k in ["sign", "e-sign", "contract", "agreement", "draw", "signature", "pdf"]):
        reply = "You can sign documents or send them to others at /sign. Just upload a PDF or image, place your signature box, and either sign it yourself or create a link for someone else. Once the other person signs, you'll receive an email notification with the signed copy."

    elif any(k in q for k in ["deposit", "request", "receive", "client", "drop", "kiosk"]):
        reply = "Want to receive files from clients? Click the 'Request' button at the top of the page to generate a Client Drop link. Anyone with the link can upload large files directly into your personal vault without needing to create an account, and your private files stay hidden."

    elif any(k in q for k in ["folder", "zip", "jszip", "directory", "multiple", "tree"]):
        reply = "You can upload entire folders or multiple files at once. Zephyr automatically compresses them into a single clean .zip file right in your browser before uploading, keeping your folder structure intact."

    elif any(k in q for k in ["keep me signed in", "remember", "session", "logout", "login", "device"]):
        reply = "On the sign-in page, checking 'Keep me signed in' saves your login on that device across browser restarts. If you leave it unchecked, Zephyr protects your privacy by asking you to sign in again whenever you open a new browser session."

    elif any(k in q for k in ["human", "support", "help", "contact", "email", "priyam", "developer", "agent", "bug"]):
        reply = "Need help from a real person or want to report a bug? You can reach human support directly by emailing Priyam Rana at priyamrana069@gmail.com."

    elif any(k in q for k in ["hi", "hello", "hey", "greetings"]):
        reply = "Hi there! I'm Zephyr Copilot. I can help you with file transfers, vault storage, password resets, E-Sign, and privacy features. What would you like to know?"

    else:
        reply = "I'm here to help with all things Zephyr—like how our zero-knowledge encryption works, storage limits, E-Sign, Burn-on-Read, or password resets. If you have a specific question or need manual help, feel free to email human support at priyamrana069@gmail.com!"

    return {"reply": reply}

# ----------------- Brand Assets & Favicon -----------------
@app.get("/favicon.ico", include_in_schema=False)
async def site_favicon():
    return Response(content=SUPERSONIC_FAVICON_SVG, media_type="image/svg+xml")

# ----------------- UI Pages -----------------
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

# ----------------- E-Sign Document & Envelope Dashboard API -----------------
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
    doc_id = uuid.uuid4().hex[:10]
    body = await request.body()
    content_type = request.headers.get("content-type", "application/pdf")
    s3_key = f"sign_docs/{doc_id}/{filename}"
    
    s3_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=s3_key,
        Body=body,
        ContentType=content_type
    )

    conn = get_db()
    cursor = conn.cursor()

    resolved_creator = (creator_email or "").strip()
    if not resolved_creator and user_id:
        cursor.execute("SELECT email FROM users WHERE user_id = %s", (user_id,))
        u = cursor.fetchone()
        if u and u.get("email"):
            resolved_creator = u["email"]

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

# ----------------- Zephyr Drive Core API -----------------
@app.get("/api/drive/quota")
async def get_drive_quota(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT tier, storage_used_bytes, storage_quota_bytes FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()
    conn.close()

    if not user:
        return {"tier": "free", "used_bytes": 0, "quota_bytes": 5368709120}

    quota = user.get("storage_quota_bytes") or (214748364800 if user.get("tier") == "pro" else 5368709120)
    return {
        "tier": user.get("tier", "free"),
        "used_bytes": user.get("storage_used_bytes", 0) or 0,
        "quota_bytes": quota
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
        quota = user.get("storage_quota_bytes") or 5368709120

    if (used + file_size) > quota:
        conn.close()
        raise HTTPException(status_code=403, detail="Drive storage quota exceeded. Upgrade to Pro for 200 GB.")

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
        quota = owner.get("storage_quota_bytes") or 5368709120
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
            print(f"[BREVO SKIPPED]: Missing key or address ({to_address})", flush=True)
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
                print(f"[BREVO SUCCESS] Dispatched notification to {to_address} (Status {resp.status})", flush=True)
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

# ----------------- Ephemeral Transfers -----------------
class CreateShareRequest(BaseModel):
    filename: str
    filesize_mb: float
    password: Optional[str] = None
    expiry_hours: int = 24
    max_downloads: int = 0
    user_id: Optional[str] = None

class DownloadPayload(BaseModel):
    password: Optional[str] = None

@app.get("/share/{share_id}", response_class=HTMLResponse)
async def share_page(request: Request, share_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Vault transfer link not found or expired.")

    max_downloads = row.get("max_downloads", 0) or 0
    if max_downloads > 0 and row["downloads"] >= max_downloads:
        raise HTTPException(status_code=410, detail="This link reached its maximum download limit and was shredded.")

    if row["expiry_hours"] != 0:
        expires_at = row["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if datetime.utcnow().astimezone() > expires_at:
            raise HTTPException(status_code=410, detail="This vault link has expired.")

    return render_template("download.html", request, {
        "share_id": share_id,
        "filename": row["filename"],
        "filesize": row["filesize_mb"],
        "downloads": row["downloads"],
        "has_password": bool(row["password_hash"])
    })

@app.post("/api/create-share")
async def create_share(payload: CreateShareRequest):
    conn = get_db()
    cursor = conn.cursor()
    user_tier = "free"
    if payload.user_id:
        cursor.execute("SELECT tier FROM users WHERE user_id = %s", (payload.user_id,))
        user_row = cursor.fetchone()
        if user_row and user_row.get("tier"):
            user_tier = user_row["tier"]

    max_mb = 50000 if user_tier == "pro" else 2048
    if payload.filesize_mb > max_mb:
        conn.close()
        raise HTTPException(status_code=400, detail=f"File exceeds limit for {user_tier.upper()} tier.")

    created_at = datetime.utcnow()
    expires_at = created_at + timedelta(days=36500) if payload.expiry_hours == 0 else created_at + timedelta(hours=payload.expiry_hours)
    share_id = uuid.uuid4().hex[:8]
    s3_key = f"transfers/{share_id}/{payload.filename}"
    password_hash = hashlib.sha256(payload.password.encode()).hexdigest() if payload.password else None

    cursor.execute("""
        INSERT INTO shares (id, filename, filesize_mb, s3_key, password_hash, expiry_hours, max_downloads, created_at, expires_at, downloads, user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s)
    """, (share_id, payload.filename, payload.filesize_mb, s3_key, password_hash, payload.expiry_hours, payload.max_downloads, created_at, expires_at, payload.user_id))
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
    s3_client.put_object(Bucket=R2_BUCKET_NAME, Key=row["s3_key"], Body=file_bytes, ContentType=request.headers.get("content-type", "application/octet-stream"))
    return {"status": "success", "share_id": share_id}

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
    return {"tier": row.get("tier", "free"), "email": row.get("email"), "user_id": row.get("user_id")}

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

    print(f"[LEMON SQUEEZY WEBHOOK] Event: {event_name} | Email: {user_email} | User ID: {user_id}", flush=True)

    conn = get_db()
    cursor = conn.cursor()

    # 1. If user_id wasn't in custom_data, resolve it from Supabase auth.users by email
    if not user_id and user_email:
        try:
            cursor.execute("SELECT id FROM auth.users WHERE LOWER(email) = LOWER(%s)", (user_email.strip(),))
            auth_row = cursor.fetchone()
            if auth_row and auth_row.get("id"):
                user_id = str(auth_row["id"])
        except Exception as e:
            print(f"[AUTH LOOKUP NOTICE]: {e}", flush=True)

    # 2. UPGRADE: Order or subscription successful
    if event_name in ["order_created", "subscription_created", "subscription_resumed", "subscription_payment_success"]:
        if user_id:
            cursor.execute("""
                INSERT INTO users (user_id, email, tier, storage_quota_bytes)
                VALUES (%s, %s, 'pro', 214748364800)
                ON CONFLICT (user_id) DO UPDATE SET 
                    tier = 'pro', 
                    storage_quota_bytes = 214748364800, 
                    email = COALESCE(EXCLUDED.email, users.email)
            """, (user_id, user_email))
        elif user_email:
            cursor.execute("""
                UPDATE users 
                SET tier = 'pro', storage_quota_bytes = 214748364800 
                WHERE LOWER(email) = LOWER(%s)
            """, (user_email.strip(),))
        conn.commit()

    # 3. DOWNGRADE: Subscription lapsed or cancelled
    elif event_name in ["subscription_cancelled", "subscription_expired", "subscription_paused"]:
        if user_id:
            cursor.execute("""
                UPDATE users 
                SET tier = 'free', storage_quota_bytes = 5368709120 
                WHERE user_id = %s
            """, (user_id,))
        elif user_email:
            cursor.execute("""
                UPDATE users 
                SET tier = 'free', storage_quota_bytes = 5368709120 
                WHERE LOWER(email) = LOWER(%s)
            """, (user_email.strip(),))
        conn.commit()

    conn.close()
    return {"status": "received"}