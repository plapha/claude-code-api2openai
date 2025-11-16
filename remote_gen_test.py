import os
import uuid
import hashlib
import requests
import json

API_URL = os.getenv("UPSTREAM_API_URL", "https://fizzlycode.com/api/v1/messages?beta=true")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY")


def _build_headers():
    if not UPSTREAM_API_KEY:
        raise SystemExit("Set UPSTREAM_API_KEY before running this smoke test.")
    return {
        "accept": "application/json",
        "anthropic-beta": "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14",
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-version": "2023-06-01",
        "authorization": f"Bearer {UPSTREAM_API_KEY}",
        "content-type": "application/json",
        "user-agent": "claude-cli/2.0.25 (external, cli)",
        "x-app": "cli"
    }


def main():
    user_hash = hashlib.sha256(b"server-test").hexdigest()
    user_id = f"user_{user_hash}_account__session_{uuid.uuid4()}"
    body = {
        "model": os.getenv("DEFAULT_MODEL", "claude-3-5-sonnet-latest"),
        "messages": [{
            "role": "user",
            "content": [{"type": "text", "text": "ping"}]
        }],
        "system": [{
            "type": "text",
            "text": "You are Claude Code, Anthropic's official CLI for Claude."
        }],
        "metadata": {"user_id": user_id},
        "tools": [],
        "max_tokens": 128,
        "stream": False
    }

    print(f"Requesting {API_URL} with user_id {user_id}")
    resp = requests.post(API_URL, headers=_build_headers(), json=body, timeout=30)
    print("Status:", resp.status_code)
    try:
        print(json.dumps(resp.json(), ensure_ascii=False)[:500])
    except ValueError:
        print(resp.text[:500])


if __name__ == "__main__":
    main()
