import os
import uuid
import sqlite3
import hashlib
from datetime import datetime, timedelta
from typing import Optional

import boto3
from botocore.config import Config
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# Initialize FastAPI App
app = FastAPI(title="VaultPro API", version="1.0.0")

# Mount static folder if present
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# Cloudflare R2 / S3 Configuration
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "vaultpro-storage")

R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else None

s3_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4")
)

# Supabase Public Keys for Front-end Template Injection
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

# SQLite Database Setup
DB_PATH = "vault.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shares (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            filesize_mb REAL NOT NULL,
            s3_key TEXT NOT NULL,
            password_hash TEXT,
            expiry_hours INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            downloads INTEGER DEFAULT 0,
            user_id TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT,
            tier TEXT DEFAULT 'free',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Pydantic Schemas
class CreateShareRequest(BaseModel):
    filename: str
    filesize_mb: float
    password: Optional[str] = None
    expiry_hours: int = 24
    user_id: Optional[str] = None

class DownloadPayload(BaseModel):
    password: Optional[str] = None

# UI Page Routes
@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "supabase_url": SUPABASE_URL,
        "supabase_anon": SUPABASE_ANON_KEY
    })

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "supabase_url": SUPABASE_URL,
        "supabase_anon": SUPABASE_ANON_KEY
    })

@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    if os.path.exists("templates/auth.html"):
        return templates.TemplateResponse("auth.html", {
            "request": request,
            "supabase_url": SUPABASE_URL,
            "supabase_anon": SUPABASE_ANON_KEY
        })
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/share/{share_id}", response_class=HTMLResponse)
async def share_page(request: Request, share_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = ?", (share_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Vault transfer link not found or expired.")

    # Check expiration
    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.utcnow() > expires_at:
        raise HTTPException(status_code=410, detail="This vault has expired and is no longer available.")

    has_password = bool(row["password_hash"])

    return templates.TemplateResponse("download.html", {
        "request": request,
        "share_id": share_id,
        "filename": row["filename"],
        "filesize": row["filesize_mb"],
        "downloads": row["downloads"],
        "has_password": has_password
    })

# API Routes
@app.post("/api/create-share")
async def create_share(payload: CreateShareRequest):
    user_tier = "free"
    if payload.user_id:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT tier FROM users WHERE user_id = ?", (payload.user_id,))
        user_row = cursor.fetchone()
        conn.close()
        if user_row and user_row["tier"]:
            user_tier = user_row["tier"]

    # Enforce tier limits
    max_mb = 50000 if user_tier == "pro" else 2048
    if payload.filesize_mb > max_mb:
        raise HTTPException(status_code=400, detail=f"File exceeds maximum allowed size for {user_tier.upper()} tier ({max_mb/1024:.0f} GB).")

    max_hours = 720 if user_tier == "pro" else 72
    if payload.expiry_hours > max_hours:
        payload.expiry_hours = max_hours

    share_id = uuid.uuid4().hex[:8]
    s3_key = f"transfers/{share_id}/{payload.filename}"
    
    password_hash = None
    if payload.password:
        password_hash = hashlib.sha256(payload.password.encode()).hexdigest()

    created_at = datetime.utcnow()
    expires_at = created_at + timedelta(hours=payload.expiry_hours)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO shares (id, filename, filesize_mb, s3_key, password_hash, expiry_hours, created_at, expires_at, downloads, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
    """, (
        share_id,
        payload.filename,
        payload.filesize_mb,
        s3_key,
        password_hash,
        payload.expiry_hours,
        created_at.isoformat(),
        expires_at.isoformat(),
        payload.user_id
    ))
    conn.commit()
    conn.close()

    # Generate presigned direct upload URL to R2
    try:
        upload_url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": R2_BUCKET_NAME,
                "Key": s3_key,
                "ContentType": "application/octet-stream"
            },
            ExpiresIn=3600
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate upload URL: {str(e)}")

    return {
        "share_id": share_id,
        "upload_url": upload_url,
        "expires_at": expires_at.isoformat()
    }

@app.post("/share/{share_id}/download")
@app.post("/api/download/{share_id}")
async def process_download(share_id: str, payload: Optional[DownloadPayload] = None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = ?", (share_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="File share link not found or expired.")

    # Validate expiration
    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.utcnow() > expires_at:
        conn.close()
        raise HTTPException(status_code=410, detail="This transfer link has expired.")

    # Validate password if configured
    stored_hash = row["password_hash"]
    if stored_hash:
        user_pass = payload.password if payload else None
        if not user_pass:
            conn.close()
            raise HTTPException(status_code=401, detail="Passcode required to download this file.")
        
        computed_hash = hashlib.sha256(user_pass.encode()).hexdigest()
        if computed_hash != stored_hash and user_pass != stored_hash:
            conn.close()
            raise HTTPException(status_code=401, detail="Incorrect passcode entered.")

    # Generate presigned download URL
    try:
        download_url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET_NAME,
                "Key": row["s3_key"],
                "ResponseContentDisposition": f'attachment; filename="{row["filename"]}"'
            },
            ExpiresIn=3600
        )
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Storage presign failure: {str(e)}")

    # Update downloads
    cursor.execute("UPDATE shares SET downloads = downloads + 1 WHERE id = ?", (share_id,))
    conn.commit()
    conn.close()

    return {"download_url": download_url}

@app.get("/api/user-profile")
async def get_user_profile(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return {"tier": "free", "user_id": user_id}
    return {"tier": row["tier"], "email": row["email"], "user_id": row["user_id"]}

@app.get("/api/user-shares")
async def get_user_shares(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "filename": r["filename"],
            "filesize_mb": r["filesize_mb"],
            "downloads": r["downloads"],
            "expires_at": r["expires_at"],
            "created_at": r["created_at"]
        }
        for r in rows
    ]

@app.delete("/api/shares/{share_id}")
async def delete_user_share(share_id: str, user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = ? AND user_id = ?", (share_id, user_id))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Vault not found or unauthorized.")

    # Remove from storage
    try:
        s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=row["s3_key"])
    except Exception:
        pass

    cursor.execute("DELETE FROM shares WHERE id = ?", (share_id,))
    conn.commit()
    conn.close()

    return {"status": "deleted"}

# Lemon Squeezy Webhook Handler
@app.post("/api/webhook/lemonsqueezy")
async def lemon_webhook(request: Request):
    payload = await request.json()
    event_name = payload.get("meta", {}).get("event_name", "")

    if event_name in ["order_created", "subscription_created", "subscription_resumed"]:
        custom_data = payload.get("meta", {}).get("custom_data", {})
        user_id = custom_data.get("user_id")
        user_email = payload.get("data", {}).get("attributes", {}).get("user_email")

        if user_id:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (user_id, email, tier)
                VALUES (?, ?, 'pro')
                ON CONFLICT(user_id) DO UPDATE SET tier = 'pro', email = excluded.email
            """, (user_id, user_email))
            conn.commit()
            conn.close()

    return {"status": "received"}