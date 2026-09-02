import os
import sqlite3
import uuid
from datetime import datetime, timedelta
import boto3
from botocore.config import Config
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="VaultPro Engine")
templates = Jinja2Templates(directory="templates")

# Initialize Database
DB_PATH = "vault.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                file_key TEXT NOT NULL,
                filesize_mb REAL DEFAULT 0,
                password TEXT,
                expires_at TIMESTAMP NOT NULL,
                downloads INTEGER DEFAULT 0
            )
        """)
init_db()

# Configure S3 client for Cloudflare R2
s3 = boto3.client(
    service_name="s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
    config=Config(signature_version="s3v4"),
    region_name="auto",
)

BUCKET = os.getenv("R2_BUCKET_NAME")

class CreateShareRequest(BaseModel):
    filename: str
    filesize_mb: float
    password: str | None = None
    expiry_hours: int = 24

class UnlockRequest(BaseModel):
    share_id: str
    password: str | None = None

@app.get("/", response_class=HTMLResponse)
async def serve_home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.post("/api/create-share")
def create_share(req: CreateShareRequest):
    try:
        share_id = str(uuid.uuid4())[:8]  # Clean 8-character link ID
        clean_name = req.filename.replace(" ", "_")
        object_key = f"vault/{share_id}_{clean_name}"
        
        expires_at = datetime.utcnow() + timedelta(hours=req.expiry_hours)

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO shares (id, filename, file_key, filesize_mb, password, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (share_id, clean_name, object_key, req.filesize_mb, req.password if req.password else None, expires_at)
            )

        # Generate 15-minute presigned upload URL for direct browser transmission
        upload_url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={"Bucket": BUCKET, "Key": object_key},
            ExpiresIn=900
        )
        return {"upload_url": upload_url, "share_id": share_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/share/{share_id}", response_class=HTMLResponse)
async def serve_share_page(request: Request, share_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM shares WHERE id = ?", (share_id,))
        record = cur.fetchone()

    if not record:
        raise HTTPException(status_code=404, detail="File link not found.")

    expires_at = datetime.strptime(record["expires_at"], "%Y-%m-%d %H:%M:%S.%f") if "." in record["expires_at"] else datetime.strptime(record["expires_at"], "%Y-%m-%d %H:%M:%S")
    if datetime.utcnow() > expires_at:
        raise HTTPException(status_code=410, detail="This secure link has expired.")

    return templates.TemplateResponse(
        request=request,
        name="download.html",
        context={
            "share_id": record["id"],
            "filename": record["filename"],
            "filesize": record["filesize_mb"],
            "is_locked": bool(record["password"]),
            "downloads": record["downloads"],
        }
    )

@app.post("/api/unlock-download")
def unlock_download(req: UnlockRequest):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM shares WHERE id = ?", (req.share_id,))
        record = cur.fetchone()

    if not record:
        raise HTTPException(status_code=404, detail="File not found.")

    if record["password"] and record["password"] != req.password:
        raise HTTPException(status_code=401, detail="Incorrect vault passcode.")

    # Increment counter
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE shares SET downloads = downloads + 1 WHERE id = ?", (req.share_id,))

    download_url = s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": BUCKET, "Key": record["file_key"]},
        ExpiresIn=3600  # 1 hour active link
    )
    return {"download_url": download_url}