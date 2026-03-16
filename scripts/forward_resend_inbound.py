#!/usr/bin/env python3
"""Poll Resend Receiving API and forward new inbound emails to SMTP target."""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from email_utils import forward_resend_inbound_to_smtp_user  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward inbound Resend emails to SMTP target")
    parser.add_argument("--limit", type=int, default=25, help="Maximum inbound emails to inspect per run")
    args = parser.parse_args()

    result = forward_resend_inbound_to_smtp_user(limit=args.limit)
    print(json.dumps(result, ensure_ascii=False))

    if result.get("failed", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
