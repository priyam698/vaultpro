import os
import boto3
from dotenv import load_dotenv

# Load variables from .env
load_dotenv()

# Initialize S3 client for Cloudflare R2
s3 = boto3.client(
    service_name="s3",
    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
    region_name="auto",
)

bucket = os.getenv("R2_BUCKET_NAME")
test_file = "hello_vault.txt"
content = b"Welcome to the Premium Storage Engine!"

print(f"Connecting to bucket: {bucket}...")

# 1. Upload a test object
s3.put_object(
    Bucket=bucket,
    Key=test_file,
    Body=content,
    ContentType="text/plain"
)
print(f"Uploaded '{test_file}' successfully!")

# 2. List objects in bucket
response = s3.list_objects_v2(Bucket=bucket)
files = [item["Key"] for item in response.get("Contents", [])]
print(f"Bucket contents: {files}")