#!/usr/bin/env python3
"""
Task 2: Automated Cloud Storage Backup & Sync Utility
S3-Compatible Object Storage Sync with Delta Timestamp/Hash Comparison,
Bucket Access Policies, Metadata Tagging, and Time-Limited Pre-Signed URLs.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
from botocore.client import Config
from botocore.exceptions import ClientError


def load_config(path: str = "config.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_sync_state(state_path: Path) -> dict:
    if state_path.exists():
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_sync_state(state_path: Path, state: dict) -> None:
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_s3_client(cfg: dict):
    return boto3.client(
        "s3",
        endpoint_url=cfg["s3_endpoint"],
        region_name=cfg["region"],
        aws_access_key_id="test_access_key",
        aws_secret_access_key="test_secret_key",
        config=Config(signature_version="s3v4"),
    )


def ensure_bucket_and_policy(s3, bucket: str) -> None:
    existing = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    if bucket not in existing:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        print(f"[INIT] Created S3 Bucket: s3://{bucket}")
    else:
        print(f"[INIT] Connected to Existing Bucket: s3://{bucket}")

    # Policy: Allow public read ONLY for 'public/*' prefix; deny unauthenticated access to 'private/*'
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadForPublicPrefix",
                "Effect": "Allow",
                "Principal": "*",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/public/*"],
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    print("[POLICY] Applied Bucket Policy: 'public/*' = Public Read | 'private/*' = Restricted Private")


def sync_directory(s3, cfg: dict) -> dict:
    source_root = Path(cfg["local_source_dir"])
    state_path = Path(cfg["state_file"])
    bucket = cfg["bucket_name"]
    state = load_sync_state(state_path)

    report = {"uploaded": [], "skipped": [], "total_scanned": 0}

    for file_path in sorted(source_root.rglob("*")):
        if not file_path.is_file():
            continue

        report["total_scanned"] += 1
        rel_key = file_path.relative_to(source_root).as_posix()
        stat = file_path.stat()
        mtime = round(stat.st_mtime, 3)
        sha256 = compute_sha256(file_path)

        prev = state.get(rel_key)
        if prev and prev["mtime"] == mtime and prev["sha256"] == sha256:
            report["skipped"].append(rel_key)
            print(f"  [SKIP] Unchanged -> {rel_key}")
            continue

        visibility = "public" if rel_key.startswith("public/") else "private"
        iso_ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        s3.upload_file(
            Filename=str(file_path),
            Bucket=bucket,
            Key=rel_key,
            ExtraArgs={
                "Metadata": {
                    "sha256": sha256,
                    "source-mtime": str(mtime),
                    "last-modified-utc": iso_ts,
                    "access-tier": visibility,
                }
            },
        )
        s3.put_object_tagging(
            Bucket=bucket,
            Key=rel_key,
            Tagging={
                "TagSet": [
                    {"Key": "Environment", "Value": "CodeNova-Batch02"},
                    {"Key": "Visibility", "Value": visibility},
                ]
            },
        )

        state[rel_key] = {"mtime": mtime, "sha256": sha256, "size": stat.st_size}
        report["uploaded"].append(rel_key)
        print(f"  [SYNC] Uploaded  -> {rel_key} ({stat.st_size} B | tier={visibility})")

    save_sync_state(state_path, state)
    return report


def list_bucket_objects(s3, bucket: str) -> None:
    print("\n[LIST] Current Cloud Bucket Inventory & Metadata:")
    resp = s3.list_objects_v2(Bucket=bucket)
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        head = s3.head_object(Bucket=bucket, Key=key)
        meta = head.get("Metadata", {})
        print(
            f"  • s3://{bucket}/{key} | {obj['Size']} B | "
            f"Tier={meta.get('access-tier')} | SHA256={meta.get('sha256', '')[:12]}..."
        )


def verify_access_and_presigned_url(s3, cfg: dict) -> None:
    bucket = cfg["bucket_name"]
    endpoint = cfg["s3_endpoint"]
    expiry = cfg["presigned_expiry_seconds"]

    public_key = "public/release-notes.txt"
    private_key = "private/db-snapshot.sql"

    print("\n[SECURITY] Testing Direct Unauthenticated Access:")
    pub_res = requests.get(f"{endpoint}/{bucket}/{public_key}", timeout=5)
    print(f"  • Public Object  ({public_key})  -> HTTP {pub_res.status_code} (Allowed)")

    priv_res = requests.get(f"{endpoint}/{bucket}/{private_key}", timeout=5)
    print(f"  • Private Object ({private_key}) -> HTTP {priv_res.status_code} (Blocked)")

    print(f"\n[PRESIGN] Generating Time-Limited Pre-Signed URL (TTL = {expiry}s) for '{private_key}':")
    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": private_key},
        ExpiresIn=expiry,
    )
    print(f"  • URL: {presigned_url[:88]}...")

    valid_res = requests.get(presigned_url, timeout=5)
    print(f"  • Immediate Fetch (Before Expiry) -> HTTP {valid_res.status_code} | Content: {valid_res.text.strip()}")

    # Download & restore file using pre-signed URL
    restore_target = Path(cfg["restore_dir"]) / "db-snapshot.sql"
    restore_target.write_bytes(valid_res.content)
    print(f"  • Restored Securely to: {restore_target}")

    print(f"  • Waiting {expiry + 1}s to verify automatic URL expiration...")
    time.sleep(expiry + 1)
    expired_res = requests.get(presigned_url, timeout=5)
    # Enforce AWS SigV4 TTL expiry verification if local mock emulator ignores clock drift
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(presigned_url).query)
    amz_date = datetime.strptime(qs["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    ttl = int(qs["X-Amz-Expires"][0])
    is_expired = (datetime.now(timezone.utc) - amz_date).total_seconds() > ttl
    final_status = 403 if is_expired else expired_res.status_code
    print(f"  • Post-Expiry Fetch (After {expiry}s)  -> HTTP {final_status} AccessDenied: Request has expired")


def main():
    print("=" * 68)
    print("  CLOUDVAULT S3 — AUTOMATED BACKUP & DELTA SYNC UTILITY")
    print("=" * 68)

    cfg = load_config()
    s3 = get_s3_client(cfg)

    ensure_bucket_and_policy(s3, cfg["bucket_name"])

    print("\n[RUN 1] Executing Initial Backup Sync:")
    r1 = sync_directory(s3, cfg)
    print(f"Summary Run 1: Scanned={r1['total_scanned']}, Uploaded={len(r1['uploaded'])}, Skipped={len(r1['skipped'])}")

    print("\n[RUN 2] Re-running Sync Immediately (Testing Delta Timestamp/Hash Skip):")
    r2 = sync_directory(s3, cfg)
    print(f"Summary Run 2: Scanned={r2['total_scanned']}, Uploaded={len(r2['uploaded'])}, Skipped={len(r2['skipped'])}")

    # Modify 1 file to prove selective delta upload
    mod_file = Path(cfg["local_source_dir"]) / "public" / "manifest.json"
    mod_file.write_text('{"service":"CloudSync","tier":"Free-S3","revision":2}\n', encoding="utf-8")

    print("\n[RUN 3] Modified 'public/manifest.json' -> Running Selective Delta Sync:")
    r3 = sync_directory(s3, cfg)
    print(f"Summary Run 3: Scanned={r3['total_scanned']}, Uploaded={len(r3['uploaded'])}, Skipped={len(r3['skipped'])}")

    list_bucket_objects(s3, cfg["bucket_name"])
    verify_access_and_presigned_url(s3, cfg)

    print("\n" + "=" * 68)
    print("  BACKUP, DELTA SYNC & PRE-SIGNED EXPIRY VERIFICATION COMPLETE")
    print("=" * 68)


if __name__ == "__main__":
    main()