"""
SenseCritiq — Cloudflare R2 storage service.
S3-compatible uploads and pre-signed URL generation.
"""

import os
import boto3
from botocore.config import Config

R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "sensecritiq-uploads")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL", "")


def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def upload_file(file_bytes: bytes, key: str, content_type: str = "application/octet-stream") -> str:
    """
    Upload bytes to R2. Returns the R2 object key.
    key format: uploads/{session_id}.{ext}
    """
    client = _get_client()
    client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
    )
    return key


def get_presigned_url(key: str, expires_in: int = 3600) -> str:
    """
    Generate a pre-signed URL for downloading a file from R2.
    expires_in: seconds until URL expires (default 1 hour)
    """
    client = _get_client()
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": key},
        ExpiresIn=expires_in,
    )
    return url


def download_file(key: str) -> bytes:
    """Download a file from R2 and return its bytes."""
    client = _get_client()
    response = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
    return response["Body"].read()


CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def content_type_for(filename: str) -> str:
    from pathlib import Path
    suffix = Path(filename).suffix.lower()
    return CONTENT_TYPES.get(suffix, "application/octet-stream")
