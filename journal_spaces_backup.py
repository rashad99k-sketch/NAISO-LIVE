"""Encrypted PostgreSQL journal backup to private DigitalOcean Spaces.

Usage:
  python journal_spaces_backup.py backup
  python journal_spaces_backup.py restore --object-key <key> --output /secure/path/journal.dump

Restore only decrypts the dump to a local file. It never connects to or overwrites
a database. Review and restore into a disposable database manually first.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _cipher() -> AESGCM:
    raw = base64.b64decode(_required("BACKUP_ENCRYPTION_KEY"), validate=True)
    if len(raw) != 32:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must be base64-encoded 32-byte key")
    return AESGCM(raw)


def _s3():
    region = _required("DO_SPACES_REGION")
    endpoint = os.getenv("DO_SPACES_ENDPOINT", f"https://{region}.digitaloceanspaces.com")
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=_required("DO_SPACES_ACCESS_KEY_ID"),
        aws_secret_access_key=_required("DO_SPACES_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def backup() -> str:
    dsn = _required("DATABASE_URL")
    bucket = _required("DO_SPACES_BUCKET")
    cipher = _cipher()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(prefix="ggp-journal-backup-") as tmp:
        dump_path = Path(tmp) / "journal.dump"
        # Password is supplied via PGPASSFILE by the platform/operator; never
        # put credentials in command arguments or log output.
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(dump_path), dsn],
            check=True, timeout=int(os.getenv("PG_DUMP_TIMEOUT_SEC", "180")),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        plaintext = dump_path.read_bytes()
        sha = hashlib.sha256(plaintext).hexdigest()
        nonce = os.urandom(12)
        aad = f"ggp-trade-journal:{stamp}:v1".encode()
        encrypted = b"GGPJ1" + nonce + cipher.encrypt(nonce, plaintext, aad)
        key = f"trade-journal/{stamp}-{uuid.uuid4().hex}.dump.ggpenc"
        client = _s3()
        client.put_object(Bucket=bucket, Key=key, Body=encrypted,
                          ContentType="application/octet-stream", ACL="private")
        manifest = {
            "format": "GGPJ1-AES256GCM",
            "created_utc": stamp,
            "schema_version": 1,
            "object_key": key,
            "encrypted_sha256": hashlib.sha256(encrypted).hexdigest(),
            "plaintext_sha256": sha,
            "plaintext_bytes": len(plaintext),
        }
        manifest_key = key + ".manifest.json"
        client.put_object(Bucket=bucket, Key=manifest_key,
                          Body=json.dumps(manifest, sort_keys=True, indent=2).encode(),
                          ContentType="application/json", ACL="private")
        print(json.dumps({"ok": True, "object_key": key,
                          "manifest_key": manifest_key, "sha256": sha}))
        return key


def restore(object_key: str, output: str) -> None:
    bucket = _required("DO_SPACES_BUCKET")
    response = _s3().get_object(Bucket=bucket, Key=object_key)
    encrypted = response["Body"].read()
    if len(encrypted) < 34 or encrypted[:5] != b"GGPJ1":
        raise RuntimeError("backup format/header is invalid")
    nonce, ciphertext = encrypted[5:17], encrypted[17:]
    # Timestamp is authenticated from the adjacent manifest; require it rather
    # than accepting unauthenticated metadata.
    manifest_key = object_key + ".manifest.json"
    manifest = json.loads(_s3().get_object(Bucket=bucket, Key=manifest_key)["Body"].read())
    stamp = str(manifest["created_utc"])
    aad = f"ggp-trade-journal:{stamp}:v1".encode()
    plaintext = _cipher().decrypt(nonce, ciphertext, aad)
    if hashlib.sha256(plaintext).hexdigest() != manifest["plaintext_sha256"]:
        raise RuntimeError("decrypted backup checksum does not match manifest")
    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing restore file: {target}")
    target.write_bytes(plaintext)
    print(json.dumps({"ok": True, "restored_file": str(target),
                      "bytes": len(plaintext), "sha256": manifest["plaintext_sha256"]}))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup")
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--object-key", required=True)
    restore_parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            backup()
        else:
            restore(args.object_key, args.output)
        return 0
    except Exception as exc:
        print(f"BACKUP_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
