"""
manual_auth.py
==============
Exchanges a Schwab authorization code (or the full redirected URL)
for tokens, creates the encrypted vault file, and pushes it to the VM.
Zero SSH tunnel or local server required.
"""

import sys
import os
import urllib.parse
import base64
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv(Path(__file__).parent / ".env")

CLIENT_ID = os.getenv("SCHWAB_CLIENT_ID")
CLIENT_SECRET = os.getenv("SCHWAB_CLIENT_SECRET")
PASSPHRASE = os.getenv("VAULT_PASSPHRASE")

if not CLIENT_ID or not CLIENT_SECRET or not PASSPHRASE:
    print("ERROR: Missing credentials in .env")
    sys.exit(1)

def exchange_and_save(raw_input_url_or_code, redirect_uri="https://127.0.0.1:5556"):
    # If a full URL was provided, extract the code and check redirect_uri
    raw = raw_input_url_or_code.strip()
    if "code=" in raw:
        parsed = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(parsed.query)
        code = qs.get("code", [raw])[0]
        # Infer redirect_uri from the URL schema and host
        if parsed.scheme and parsed.netloc:
            redirect_uri = f"{parsed.scheme}://{parsed.netloc}"
    else:
        code = raw

    # Decode any %40 to @
    code = urllib.parse.unquote(code)
    print(f"Extracted auth code (length {len(code)})")
    print(f"Using redirect_uri: {redirect_uri}")

    # Basic auth header
    creds = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    b64_creds = base64.b64encode(creds).decode("utf-8")
    headers = {
        "Authorization": f"Basic {b64_creds}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }

    print("Sending token exchange request to Schwab API...")
    resp = requests.post("https://api.schwabapi.com/v1/oauth/token", headers=headers, data=data, timeout=15)
    
    if resp.status_code != 200:
        print(f"Exchange failed! Status: {resp.status_code}")
        print(f"Response: {resp.text}")
        # Try alternate redirect_uri if 400
        alt_uri = "https://127.0.0.1" if ":5556" in redirect_uri else "https://127.0.0.1:5556"
        print(f"Attempting fallback exchange with alternate redirect_uri: {alt_uri}...")
        data["redirect_uri"] = alt_uri
        resp = requests.post("https://api.schwabapi.com/v1/oauth/token", headers=headers, data=data, timeout=15)
        if resp.status_code != 200:
            print(f"Fallback exchange also failed! Status: {resp.status_code}")
            print(f"Response: {resp.text}")
            return False
        redirect_uri = alt_uri

    tokens = resp.json()
    print("Token exchange SUCCESSFUL!")

    # Encrypt and save to schwab_tokens_vault.json
    from core.auth import SecurityVault
    vault_path = Path(__file__).parent / "schwab_tokens_vault.json"
    vault = SecurityVault(str(vault_path), passphrase=PASSPHRASE)
    
    payload = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "id_token": tokens.get("id_token"),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in", 1800),
        "obtained_at": time.time(),
    }
    vault.save(payload)
    print(f"Vault encrypted and saved successfully to {vault_path} (size: {vault_path.stat().st_size} bytes)")
    return True

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python manual_auth.py '<url_or_code>'")
        sys.exit(1)
    url_or_code = sys.argv[1]
    success = exchange_and_save(url_or_code)
    sys.exit(0 if success else 1)
