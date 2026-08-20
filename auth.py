"""One-shot EVE SSO auth flow for AssetLister.

Runs a local listener on the configured callback port, opens the auth URL
in the default browser (also prints it), then exchanges the returned code
for tokens. Stores the refresh token in tokens.json (gitignored).

Run:  py auth.py
"""

import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

# Force unbuffered stdout so the URL always shows immediately
sys.stdout.reconfigure(line_buffering=True)

CONFIG_FILE = "config.json"
TOKENS_FILE = "tokens.json"

AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
VERIFY_URL = "https://login.eveonline.com/oauth/verify"

SCOPES = ["esi-assets.read_assets.v1"]

UA = "AssetLister github.com/emabe390/AssetLister"


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    captured = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            CallbackHandler.captured["code"] = qs["code"][0]
            self.send_response(200)
            body = b"<html><body><h2>Auth successful!</h2>You can close this tab.</body></html>"
        else:
            CallbackHandler.captured["error"] = qs.get("error", ["unknown"])[0]
            self.send_response(400)
            body = b"<html><body><h2>Auth failed</h2></body></html>"
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # silence request logging


def http_json(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {url}")
        print(f"Response body: {body}")
        raise


def main():
    cfg = load_config()
    client_id = cfg["client_id"]
    client_secret = cfg.get("client_secret", "")
    port = cfg["callback_port"]

    # PKCE
    code_verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    state = secrets.token_urlsafe(24)

    redirect_uri = f"http://localhost:{port}/"
    params = {
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    server = http.server.HTTPServer(("127.0.0.1", port), CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print()
    print("=" * 70)
    print("Opening your browser for EVE SSO login...")
    print("If it did not open, paste this URL into a browser manually:")
    print()
    print(auth_url)
    print()
    print(f"Waiting for callback on http://localhost:{port}/ (5 min timeout)...")
    print("=" * 70)
    print(flush=True)

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass  # URL is printed above as fallback

    thread.join(timeout=300)
    server.server_close()

    if "error" in CallbackHandler.captured:
        print(f"Auth failed: {CallbackHandler.captured['error']}")
        sys.exit(1)
    if "code" not in CallbackHandler.captured:
        print("Timed out or no code received.")
        sys.exit(1)

    code = CallbackHandler.captured["code"]

    # Exchange code for tokens (confidential client: basic auth + PKCE verifier)
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    token_data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }).encode()
    tokens = http_json(
        TOKEN_URL,
        data=token_data,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
        },
        method="POST",
    )

    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    # Verify -> character identity
    verify = http_json(
        VERIFY_URL,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": UA},
    )
    char_name, char_id = verify["CharacterName"], verify["CharacterID"]
    print(f"\nAuthenticated as: {char_name} (id {char_id})")
    print(f"Scopes: {verify.get('Scopes')}")

    if char_name != cfg.get("character_name"):
        print(f"WARNING: expected {cfg.get('character_name')}, got {char_name}")

    # Persist
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "character_id": char_id,
                "character_name": char_name,
                "refresh_token": refresh_token,
                "access_token": access_token,
            },
            f,
            indent=4,
        )
    cfg["character_id"] = char_id
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)

    print(f"\nSaved refresh token to {TOKENS_FILE} and updated {CONFIG_FILE}.")
    print("Done. You can now run update.py.")


if __name__ == "__main__":
    main()
