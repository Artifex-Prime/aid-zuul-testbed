#!/usr/bin/env python3
"""Bazel credential helper: a rotating, audience-bound Kubernetes token, never a signing key."""
import base64
import datetime
import json
import os
import sys
import time


def response(path):
    with open(path, encoding="utf-8") as stream:
        token = stream.read().strip()
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if "aid-build-farm" not in claims["aud"] or claims["exp"] <= time.time() + 60:
        raise ValueError("token expired or wrong audience")
    expiry = datetime.datetime.fromtimestamp(claims["exp"] - 60, datetime.timezone.utc)
    return {"headers": {"Authorization": ["Bearer " + token]}, "expires": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["get"]:
            raise ValueError("usage: zuul-farm-credential get")
        sys.stdin.read()
        print(json.dumps(response(os.environ.get("ZUUL_FARM_TOKEN_FILE", "/var/run/farm/token"))))
    except Exception:
        # Never print an exception that might contain the token or its contents.
        print("zuul-farm-credential: no valid farm token", file=sys.stderr)
        sys.exit(1)
