from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error


API_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def load_api_key() -> str:
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        key = os.getenv(env_name, "").strip()
        if key:
            return key
    return ""


def main() -> int:
    api_key = load_api_key()
    if not api_key:
        print("ERROR: Set GEMINI_API_KEY or GOOGLE_API_KEY in your environment.")
        return 1

    url = f"{API_URL}?key={api_key}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"HTTP {e.code}: {body[:800]}")
        return 2
    except Exception as e:
        print(f"Request failed: {e}")
        return 3

    models = data.get("models", [])
    if not models:
        print("No models returned.")
        return 0

    # Print a compact table for quick scanning.
    print("name\tdisplayName\tsupportedGenerationMethods")
    for m in models:
        name = str(m.get("name", ""))
        display = str(m.get("displayName", ""))
        methods = ",".join(m.get("supportedGenerationMethods", []) or [])
        print(f"{name}\t{display}\t{methods}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
