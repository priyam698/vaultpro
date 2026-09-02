import os
import uuid
from datetime import datetime, timedelta
import boto3
from botocore.config import Config
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session

load_dotenv()

# Database Connection (Supabase in production, SQLite locally)
RAW_DB_URL = os.getenv("DATABASE_URL", "sqlite:///./vault.db")

if RAW_DB_URL.startswith("postgres://"):
    RAW_DB_URL = RAW_DB_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if RAW_DB_URL.startswith("sqlite") else {}

engine = create_engine(RAW_DB_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class ShareRecord(Base):
    __tablename__ = "shares"

    id = Column(String, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    file_key = Column(String, nullable=False)
    filesize_mb = Column(Float, default=0.0)
    password = Column(String, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    downloads = Column(Integer, default=0)

# Create tables automatically
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Cloudflare R2 Client
s3 = boto3.client(
    service_name="s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
    config=Config(signature_version="s3v4"),
    region_name="auto",
)

BUCKET = os.getenv("R2_BUCKET_NAME")

app = FastAPI(title="VaultPro Engine")
templates = Jinja2Templates(directory="templates")

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
def create_share(req: CreateShareRequest, db: Session = Depends(get_db)):
    try:
        share_id = str(uuid.uuid4())[:8]
        clean_name = req.filename.replace(" ", "_")
        object_key = f"vault/{share_id}_{clean_name}"
        expires_at = datetime.utcnow() + timedelta(hours=req.expiry_hours)

        record = ShareRecord(
            id=share_id,
            filename=clean_name,
            file_key=object_key,
            filesize_mb=req.filesize_mb,
            password=req.password if req.password else None,
            expires_at=expires_at,
            downloads=0
        )
        db.add(record)
        db.commit()

        upload_url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={"Bucket": BUCKET, "Key": object_key},
            ExpiresIn=900
        )
        return {"upload_url": upload_url, "share_id": share_id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/share/{share_id}", response_class=HTMLResponse)
async def serve_share_page(request: Request, share_id: str, db: Session = Depends(get_db)):
    record = db.query(ShareRecord).filter(ShareRecord.id == share_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="File link not found.")

    if datetime.utcnow() > record.expires_at:
        raise HTTPException(status_code=410, detail="This secure link has expired.")

    return templates.TemplateResponse(
        request=request,
        name="download.html",
        context={
            "share_id": record.id,
            "filename": record.filename,
            "filesize": record.filesize_mb,
            "is_locked": bool(record.password),
            "downloads": record.downloads,
        }
    )

@app.post("/api/unlock-download")
def unlock_download(req: UnlockRequest, db: Session = Depends(get_db)):
    record = db.query(ShareRecord).filter(ShareRecord.id == req.share_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="File not found.")

    if record.password and record.password != req.password:
        raise HTTPException(status_code=401, detail="Incorrect vault passcode.")

    record.downloads += 1
    db.commit()

    download_url = s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": BUCKET, "Key": record.file_key},
        ExpiresIn=3600
    )
    return {"download_url": download_url}