from __future__ import annotations

import json
import tempfile
from pathlib import Path

from minio import Minio
from PIL import Image

from app.core.config import settings


def minio_enabled() -> bool:
    return bool(settings.minio_endpoint and settings.minio_access_key and settings.minio_secret_key)


def _client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_use_ssl,
    )


def _ensure_public_read_bucket(client: Minio, bucket: str) -> None:
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            }
        ],
    }
    client.set_bucket_policy(bucket, json.dumps(policy))


def _prepare_optimized_image(local_path: str, object_name: str) -> tuple[str, str, str]:
    """
    Returns: (upload_path, upload_object_name, content_type)
    """
    path = Path(local_path)
    if not settings.minio_compress_enabled:
        ext = path.suffix.lower() or ".png"
        content_type = "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else "image/jpeg")
        return str(path), object_name, content_type

    fmt = (settings.minio_compress_format or "WEBP").strip().upper()
    quality = max(40, min(95, int(settings.minio_compress_quality)))
    max_w = max(640, int(settings.minio_max_width))

    target_ext = ".webp" if fmt == "WEBP" else ".jpg"
    target_ct = "image/webp" if fmt == "WEBP" else "image/jpeg"
    target_object = str(Path(object_name).with_suffix(target_ext))

    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > max_w:
            ratio = max_w / float(im.width)
            new_size = (max_w, max(1, int(im.height * ratio)))
            im = im.resize(new_size, Image.Resampling.LANCZOS)
        fd, tmp = tempfile.mkstemp(suffix=target_ext)
        Path(tmp).unlink(missing_ok=True)
        out_path = str(Path(tmp))
        save_kwargs = {"quality": quality, "optimize": True}
        if fmt == "WEBP":
            save_kwargs["method"] = 6
        im.save(out_path, format=fmt, **save_kwargs)
    return out_path, target_object, target_ct


def upload_generated_image(local_path: str, object_name: str) -> str | None:
    if not minio_enabled():
        return None
    path = Path(local_path)
    if not path.exists():
        return None

    client = _client()
    bucket = settings.minio_bucket
    found = client.bucket_exists(bucket)
    if not found:
        client.make_bucket(bucket)
    _ensure_public_read_bucket(client, bucket)

    upload_path, upload_object, content_type = _prepare_optimized_image(str(path), object_name)
    try:
        client.fput_object(
            bucket_name=bucket,
            object_name=upload_object,
            file_path=upload_path,
            content_type=content_type,
        )
    finally:
        if upload_path != str(path):
            Path(upload_path).unlink(missing_ok=True)

    if settings.minio_public_base_url:
        base = settings.minio_public_base_url.rstrip("/")
        return f"{base}/{bucket}/{upload_object}"
    return f"http://{settings.minio_endpoint}/{bucket}/{upload_object}"
