import os
import uuid
import hashlib
import traceback
from datetime import datetime, timedelta
from typing import Optional

import boto3
from botocore.config import Config
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import psycopg2
from psycopg2.extras import RealDictCursor

# Absolute Directory Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="VaultPro API", version="1.0.0")

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

# Database Connection (Supabase PostgreSQL)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

def get_db():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL environment variable is missing.")
    # Support connection strings formatted with postgres:// or postgresql://
    conn_str = DATABASE_URL
    if conn_str.startswith("postgres://"):
        conn_str = conn_str.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(conn_str, cursor_factory=RealDictCursor)
    return conn

# Cloudflare R2 Configuration
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

class CreateShareRequest(BaseModel):
    filename: str
    filesize_mb: float
    password: Optional[str] = None
    expiry_hours: int = 24
    user_id: Optional[str] = None

class DownloadPayload(BaseModel):
    password: Optional[str] = None

# UI Routes
@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    return render_template("index.html", request, {
        "supabase_url": SUPABASE_URL,
        "supabase_anon": SUPABASE_ANON_KEY
    })

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return render_template("dashboard.html", request, {
        "supabase_url": SUPABASE_URL,
        "supabase_anon": SUPABASE_ANON_KEY
    })

@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    auth_file = os.path.join(TEMPLATES_DIR, "auth.html")
    if os.path.exists(auth_file):
        return render_template("auth.html", request, {
            "supabase_url": SUPABASE_URL,
            "supabase_anon": SUPABASE_ANON_KEY
        })
    return render_template("index.html", request)

@app.get("/share/{share_id}", response_class=HTMLResponse)
async def share_page(request: Request, share_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s", (share_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Vault transfer link not found or expired.")

    if row["expiry_hours"] != 0:
        expires_at = row["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if datetime.utcnow().astimezone() > expires_at:
            raise HTTPException(status_code=410, detail="This vault link has expired.")

    has_password = bool(row["password_hash"])

    return render_template("download.html", request, {
        "share_id": share_id,
        "filename": row["filename"],
        "filesize": row["filesize_mb"],
        "downloads": row["downloads"],
        "has_password": has_password
    })

# API Endpoints
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
    if payload.expiry_hours == 0:
        expires_at = created_at + timedelta(days=36500)
    else:
        max_hours = 720 if user_tier == "pro" else 72
        if payload.expiry_hours > max_hours:
            payload.expiry_hours = max_hours
        expires_at = created_at + timedelta(hours=payload.expiry_hours)

    share_id = uuid.uuid4().hex[:8]
    s3_key = f"transfers/{share_id}/{payload.filename}"
    
    password_hash = None
    if payload.password:
        password_hash = hashlib.sha256(payload.password.encode()).hexdigest()

    cursor.execute("""
        INSERT INTO shares (id, filename, filesize_mb, s3_key, password_hash, expiry_hours, created_at, expires_at, downloads, user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s)
    """, (
        share_id,
        payload.filename,
        payload.filesize_mb,
        s3_key,
        password_hash,
        payload.expiry_hours,
        created_at,
        expires_at,
        payload.user_id
    ))
    conn.commit()
    conn.close()

    return {
        "share_id": share_id,
        "expires_at": expires_at.isoformat()
    }

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
    content_type = request.headers.get("content-type", "application/octet-stream")

    if not R2_ENDPOINT or not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY:
        raise HTTPException(status_code=500, detail="Cloudflare R2 storage credentials missing.")

    try:
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=row["s3_key"],
            Body=file_bytes,
            ContentType=content_type
        )
    except Exception as e:
        print(f"R2 ERROR:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"R2 storage error: {str(e)}")

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
        raise HTTPException(status_code=404, detail="File share link not found or expired.")

    if row["expiry_hours"] != 0:
        expires_at = row["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if datetime.utcnow().astimezone() > expires_at:
            conn.close()
            raise HTTPException(status_code=410, detail="This transfer link has expired.")

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
        raise HTTPException(status_code=500, detail=f"Presign failed: {str(e)}")

    cursor.execute("UPDATE shares SET downloads = downloads + 1 WHERE id = %s", (share_id,))
    conn.commit()
    conn.close()

    return {"download_url": download_url}

@app.get("/api/user-profile")
async def get_user_profile(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return {"tier": "free", "user_id": user_id}
    return {"tier": row["tier"], "email": row["email"], "user_id": row["user_id"]}

@app.get("/api/user-shares")
async def get_user_shares(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "filename": r["filename"],
            "filesize_mb": r["filesize_mb"],
            "downloads": r["downloads"],
            "expires_at": "Never" if r["expiry_hours"] == 0 else str(r["expires_at"]),
            "created_at": str(r["created_at"])
        }
        for r in rows
    ]

@app.delete("/api/shares/{share_id}")
async def delete_user_share(share_id: str, user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shares WHERE id = %s AND user_id = %s", (share_id, user_id))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Vault not found or unauthorized.")

    try:
        s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=row["s3_key"])
    except Exception:
        pass

    cursor.execute("DELETE FROM shares WHERE id = %s", (share_id,))
    conn.commit()
    conn.close()

    return {"status": "deleted"}

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
                VALUES (%s, %s, 'pro')
                ON CONFLICT (user_id) DO UPDATE SET tier = 'pro', email = EXCLUDED.email
            """, (user_id, user_email))
            conn.commit()
            conn.close()

    return {"status": "received"}