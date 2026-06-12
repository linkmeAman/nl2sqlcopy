from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

import jwt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an NL2SQL MCP test JWT.")
    parser.add_argument("--tenant-id", default="tenant_1")
    parser.add_argument("--user-id", default="user_1")
    parser.add_argument("--role", default="readonly")
    parser.add_argument("--hours", type=int, default=24)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise SystemExit("JWT_SECRET is required")
    if len(secret) < 16:
        raise SystemExit("JWT_SECRET must be at least 16 characters")

    now = datetime.now(timezone.utc)
    payload = {
        "tenant_id": args.tenant_id,
        "user_id": args.user_id,
        "sub": args.user_id,
        "role": args.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=args.hours)).timestamp()),
    }
    print(jwt.encode(payload, secret, algorithm=os.environ.get("JWT_ALGORITHM", "HS256")))


if __name__ == "__main__":
    main()
