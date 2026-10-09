"""
core/auth.py
============
Security Vault and OAuth lifecycle manager for the Schwab Day-Trading Engine.

Components
----------
SecurityVault
    AES-GCM-256 authenticated encryption of schwab_tokens_vault.json.
    Key derivation: PBKDF2HMAC(HMAC-SHA256, 600 000 iterations).

SchwabAuthManager
    Manages the 30-minute access-token refresh cycle.
    Persists and reloads tokens through SecurityVault.

WeekendOAuthDaemon
    Every Saturday at 10:00 AM EDT spins up a local HTTPS loopback server
    on 127.0.0.1:5556, captures the inbound authorization code (decoding
    the browser's %40 → @), and exchanges it for a fresh 7-day refresh token.

All configuration is sourced from config.yaml:
    auth.callback_host / auth.callback_port
    auth.pbkdf2_iterations
    auth.access_token_ttl_sec
    auth.refresh_token_ttl_sec
    auth.weekend_reauth_day / auth.weekend_reauth_time
    auth.vault_file
    api.token_url
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import pytz
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

class AuthCircuitBreakerError(RuntimeError):
    """Raised when the authentication circuit breaker is open (AUTH_LOCKED)."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_NONCE_BYTES = 12   # 96-bit nonce for AES-GCM (NIST recommendation)
_SALT_BYTES  = 16   # 128-bit PBKDF2 salt
_KEY_BYTES   = 32   # 256-bit AES key
_ENCODING    = "utf-8"
_EDT         = pytz.timezone("America/New_York")


# ===========================================================================
# SecurityVault
# ===========================================================================

class SecurityVault:
    """
    Encrypts and decrypts the token vault file using AES-GCM-256.

    Layout of the vault file on disk (JSON):
        {
            "salt":       "<base64-encoded 16-byte salt>",
            "nonce":      "<base64-encoded 12-byte nonce>",
            "ciphertext": "<base64-encoded ciphertext + 16-byte GCM tag>"
        }

    The 256-bit symmetric key is derived from the user-supplied passphrase
    via PBKDF2HMAC(HMAC-SHA256, iterations=600 000, salt=random 16 bytes).
    A fresh salt and nonce are generated on every write, so two identical
    plaintext payloads produce different ciphertexts.
    """

    def __init__(self, passphrase: str, iterations: int, vault_path: Path) -> None:
        """
        Args:
            passphrase:  Master passphrase used for key derivation.
            iterations:  PBKDF2 iteration count (spec: 600 000).
            vault_path:  Absolute path to the JSON vault file on disk.
        """
        self._passphrase_bytes: bytes = passphrase.encode(_ENCODING)
        self._iterations: int = iterations
        self._vault_path: Path = Path(vault_path)
        logger.info(
            "SecurityVault ready — vault=%s, pbkdf2_iterations=%d",
            self._vault_path, self._iterations,
        )

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------

    def _derive_key(self, salt: bytes) -> bytes:
        """Derive a 256-bit AES key from the passphrase and salt."""
        kdf = PBKDF2HMAC(
            algorithm=SHA256(),
            length=_KEY_BYTES,
            salt=salt,
            iterations=self._iterations,
        )
        return kdf.derive(self._passphrase_bytes)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def save(self, payload: dict) -> None:
        """
        Serialize *payload* to JSON, encrypt with AES-GCM-256, and write
        the result to the vault file.

        A fresh random salt and nonce are generated on every call.
        """
        plaintext: bytes = json.dumps(payload, indent=2).encode(_ENCODING)

        salt  = os.urandom(_SALT_BYTES)
        nonce = os.urandom(_NONCE_BYTES)
        key   = self._derive_key(salt)

        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)

        vault_data = {
            "salt":       base64.b64encode(salt).decode(_ENCODING),
            "nonce":      base64.b64encode(nonce).decode(_ENCODING),
            "ciphertext": base64.b64encode(ciphertext).decode(_ENCODING),
        }

        self._vault_path.parent.mkdir(parents=True, exist_ok=True)
        self._vault_path.write_text(
            json.dumps(vault_data, indent=2), encoding=_ENCODING
        )
        logger.info("SecurityVault: tokens persisted to %s", self._vault_path)

    def load(self) -> dict:
        """
        Read the vault file, decrypt the ciphertext, and return the
        original payload dict.

        Raises:
            FileNotFoundError: Vault file does not exist yet.
            ValueError:        Decryption failed (wrong passphrase or tampered file).
        """
        if not self._vault_path.exists() or self._vault_path.stat().st_size == 0:
            raise FileNotFoundError(
                f"Token vault not found or empty at {self._vault_path}. "
                "Run the initial OAuth authorization flow first."
            )

        raw = json.loads(self._vault_path.read_text(encoding=_ENCODING))
        salt       = base64.b64decode(raw["salt"])
        nonce      = base64.b64decode(raw["nonce"])
        ciphertext = base64.b64decode(raw["ciphertext"])

        key = self._derive_key(salt)
        aesgcm = AESGCM(key)

        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=None)
        except Exception as exc:
            raise ValueError(
                "SecurityVault: decryption failed — wrong passphrase or corrupted vault."
            ) from exc

        payload = json.loads(plaintext.decode(_ENCODING))
        logger.info("SecurityVault: tokens loaded successfully from %s", self._vault_path)
        return payload


# ===========================================================================
# SchwabAuthManager
# ===========================================================================

class SchwabAuthManager:
    """
    Manages the Schwab OAuth 2.0 token lifecycle.

    Responsibilities
    ----------------
    - Load tokens from SecurityVault on startup.
    - Proactively refresh the access token every 30 minutes
      (configurable via auth.access_token_ttl_sec) before it expires.
    - Persist updated tokens back to the vault after every refresh.
    - Expose a `get_access_token()` method that always returns a valid token,
      blocking briefly if a refresh is in progress.

    Token refresh endpoint:
        POST https://api.schwabapi.com/v1/oauth/token
        Authorization: Basic <base64(client_id:client_secret)>
        Body: grant_type=refresh_token&refresh_token=<token>
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        vault: SecurityVault,
        cfg: dict,
    ) -> None:
        """
        Args:
            client_id:     Schwab Developer App client ID.
            client_secret: Schwab Developer App client secret.
            vault:         Initialised SecurityVault instance.
            cfg:           Top-level config dict from config.yaml.
        """
        self._client_id     = client_id
        self._client_secret = client_secret
        self._vault         = vault

        auth_cfg = cfg.get("auth", {})
        api_cfg  = cfg.get("api",  {})

        self._token_url:          str = api_cfg.get("token_url", "https://api.schwabapi.com/v1/oauth/token")
        self._access_ttl:         int = int(auth_cfg.get("access_token_ttl_sec", 1800))
        self._refresh_ttl:        int = int(auth_cfg.get("refresh_token_ttl_sec", 604800))

        # In-memory token store
        self._access_token:        Optional[str]   = None
        self._refresh_token:       Optional[str]   = None
        self._access_token_expiry: Optional[float] = None  # monotonic timestamp
        self._refresh_issued_at:   Optional[float] = None  # epoch seconds (refresh token issuance)

        self._lock = threading.Lock()
        self.auth_circuit_open: bool = False
        self.auth_circuit_failure_count: int = 0
        self._last_trip_time: Optional[float] = None
        self._circuit_cooldown_seconds: float = 300.0

        # Background refresh thread
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        logger.info(
            "SchwabAuthManager initialised — token_url=%s, access_ttl=%ds",
            self._token_url, self._access_ttl,
        )

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def load_tokens(self) -> None:
        """Load tokens from vault into memory. Must be called before start()."""
        payload = self._vault.load()
        with self._lock:
            self._access_token        = payload["access_token"]
            self._refresh_token       = payload["refresh_token"]
            # Conservatively assume the saved token has half TTL remaining
            self._access_token_expiry = time.monotonic() + (self._access_ttl / 2)
            issued = payload.get("refresh_token_issued_at") or payload.get("obtained_at")
            self._refresh_issued_at   = float(issued) if issued else None
        logger.info("SchwabAuthManager: tokens loaded from vault.")

    def start(self) -> None:
        """
        Start the background refresh daemon thread.
        The thread wakes up every 60 seconds and proactively refreshes
        the access token when it has less than 5 minutes of TTL remaining.
        """
        self._stop_event.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="SchwabAuthRefreshDaemon",
            daemon=True,
        )
        self._refresh_thread.start()
        logger.info("SchwabAuthManager: refresh daemon started.")

    def stop(self) -> None:
        """Signal the background refresh daemon to stop."""
        self._stop_event.set()
        if self._refresh_thread:
            self._refresh_thread.join(timeout=5)
        logger.info("SchwabAuthManager: refresh daemon stopped.")

    # ------------------------------------------------------------------
    # Token access
    # ------------------------------------------------------------------

    def get_access_token(self) -> str:
        """
        Return a valid access token, refreshing synchronously if expired.

        This is the primary method called by REST and WebSocket clients.
        Thread-safe.
        """
        with self._lock:
            if self.auth_circuit_open:
                if self._last_trip_time is not None and (time.monotonic() - self._last_trip_time) >= self._circuit_cooldown_seconds:
                    logger.info("SchwabAuthManager: circuit breaker cooldown elapsed. Entering HALF-OPEN state for retry.")
                else:
                    raise AuthCircuitBreakerError(
                        "SchwabAuthManager: authentication circuit breaker is OPEN (AUTH_LOCKED). "
                        "Automated outbound calls suppressed until reset via /api/auth/reset or re-authorization."
                    )
            if self._is_token_expired() or self.auth_circuit_open:
                logger.warning(
                    "SchwabAuthManager: access token expired or circuit half-open — synchronous refresh."
                )
                self._do_refresh()
            return self._access_token  # type: ignore[return-value]

    def has_refresh_token(self) -> bool:
        """Return True if a valid refresh token is currently held in memory."""
        with self._lock:
            return self._refresh_token is not None and len(self._refresh_token.strip()) > 0

    def refresh_seconds_remaining(self) -> Optional[float]:
        """
        Seconds until the refresh token expires (issuance + refresh_token_ttl_sec),
        or None if the issuance time is unknown (legacy vault without a timestamp).
        """
        with self._lock:
            if self._refresh_issued_at is None:
                return None
            return (self._refresh_issued_at + self._refresh_ttl) - time.time()

    def force_refresh(self) -> None:
        """Blocking refresh used at startup to validate the vault before trading."""
        with self._lock:
            self._do_refresh()

    def refresh_access_token(self) -> float:
        """
        Synchronously refreshes the access token and returns remaining TTL in seconds.
        """
        self.force_refresh()
        with self._lock:
            if self._access_token_expiry is not None:
                ttl = float(self._access_token_expiry - time.monotonic())
                return max(0.0, ttl)
            return float(self._access_ttl)

    # ------------------------------------------------------------------
    # Initial authorization code exchange (first-run / weekend reauth)
    # ------------------------------------------------------------------

    def exchange_authorization_code(self, auth_code: str, redirect_uri: str) -> None:
        """
        Exchange a fresh authorization code for access + refresh tokens.
        Persists the result to the vault.

        The authorization code may arrive with a trailing '@' encoded as '%40'.
        This method decodes that automatically.

        Args:
            auth_code:    Raw authorization code string from the callback URL.
            redirect_uri: Must match the registered callback URI exactly.
        """
        # Spec: decode %40 back to @ before transmission
        decoded_code = urllib.parse.unquote(auth_code)
        logger.info("SchwabAuthManager: exchanging authorization code for tokens.")

        response = requests.post(
            self._token_url,
            headers=self._basic_auth_header(),
            data={
                "grant_type":   "authorization_code",
                "code":          decoded_code,
                "redirect_uri":  redirect_uri,
            },
            timeout=15,
        )
        response.raise_for_status()
        with self._lock:
            self.auth_circuit_open = False
            self.auth_circuit_failure_count = 0
        self._ingest_token_response(response.json())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _basic_auth_header(self) -> dict:
        """Build the HTTP Basic Authentication header."""
        credentials = f"{self._client_id}:{self._client_secret}"
        encoded = base64.b64encode(credentials.encode(_ENCODING)).decode(_ENCODING)
        return {"Authorization": f"Basic {encoded}"}

    def _is_token_expired(self) -> bool:
        """True if the access token has 5 minutes or less remaining."""
        if self._access_token is None or self._access_token_expiry is None:
            return True
        return time.monotonic() >= (self._access_token_expiry - 300)  # 5-min pre-expiry

    def reset_circuit_breaker(self) -> None:
        """Manually reset the authentication circuit breaker."""
        with self._lock:
            was_open = self.auth_circuit_open
            self.auth_circuit_open = False
            self.auth_circuit_failure_count = 0
            logger.info("SchwabAuthManager: authentication circuit breaker reset manually.")
            if was_open:
                try:
                    from core.notifier import send_alert
                    send_alert(
                        title="Schwab Developer Access Restored!",
                        message="Authentication circuit breaker manually reset to CLOSED. Automated broker calls restored.",
                        priority="urgent",
                        tags=["white_check_mark", "lock"],
                    )
                except Exception:
                    pass

    def _do_refresh(self, backoff_sec: float = 10.0) -> None:
        """
        Perform a blocking refresh_token grant with exponential backoff circuit breaker.
        MUST be called while holding self._lock, or at startup.

        If token endpoint returns a 4xx error (including 401 Unauthorized):
        - Attempt refresh at most 2 times with a 10s backoff.
        - If both fail, trip the breaker: set auth_circuit_open = True,
          transition system to AUTH_LOCKED, and suppress further outbound calls.
        """
        if self.auth_circuit_open:
            if self._last_trip_time is not None and (time.monotonic() - self._last_trip_time) >= self._circuit_cooldown_seconds:
                logger.info("SchwabAuthManager: testing circuit in HALF-OPEN state.")
            else:
                raise AuthCircuitBreakerError(
                    "SchwabAuthManager: authentication circuit breaker is OPEN (AUTH_LOCKED). "
                    "Refresh suppressed until manual reset via /api/auth/reset or new OAuth code."
                )

        if not self._refresh_token:
            raise RuntimeError(
                "SchwabAuthManager: no refresh token available. "
                "Run the initial authorization flow."
            )

        logger.info("SchwabAuthManager: refreshing access token via refresh_token grant.")
        max_attempts = 2

        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    self._token_url,
                    headers=self._basic_auth_header(),
                    data={
                        "grant_type":    "refresh_token",
                        "refresh_token":  self._refresh_token,
                    },
                    timeout=15,
                )

                if 400 <= response.status_code < 500:
                    logger.warning(
                        "SchwabAuthManager: HTTP %d (4xx) during token refresh (attempt %d/%d).",
                        response.status_code, attempt, max_attempts,
                    )
                    if attempt < max_attempts:
                        logger.info("SchwabAuthManager: backing off for %.1fs before retry...", backoff_sec)
                        time.sleep(backoff_sec)
                        continue
                    else:
                        self.auth_circuit_open = True
                        self.auth_circuit_failure_count += 1
                        self._last_trip_time = time.monotonic()
                        logger.critical(
                            "SchwabAuthManager: token refresh failed %d consecutive times with 4xx errors. "
                            "TRIPPING AUTH CIRCUIT BREAKER (AUTH_LOCKED).",
                            max_attempts,
                        )
                        try:
                            from core.notifier import send_alert
                            send_alert(
                                title="Auth Circuit Breaker TRIPPED!",
                                message=f"OAuth token refresh failed {max_attempts} consecutive times (HTTP {response.status_code}). Engine locked.",
                                priority="urgent",
                                tags=["rotating_light", "warning"],
                            )
                        except Exception:
                            pass
                        raise AuthCircuitBreakerError(
                            f"OAuth token refresh returned HTTP {response.status_code}. "
                            "Auth circuit breaker TRIPPED (AUTH_LOCKED)."
                        )

                response.raise_for_status()
                
                # Check if we are recovering from an open circuit
                if self.auth_circuit_open:
                    logger.critical("SchwabAuthManager: Auth restored. Circuit closed.")
                    try:
                        from core.notifier import send_alert
                        send_alert(
                            title="Schwab Developer Access Restored!",
                            message="Authentication circuit breaker recovered to CLOSED / HALF-OPEN state. Automated broker calls restored.",
                            priority="urgent",
                            tags=["white_check_mark", "lock"],
                        )
                    except Exception:
                        pass
                
                self.auth_circuit_open = False
                self.auth_circuit_failure_count = 0
                self._last_trip_time = None
                self._ingest_token_response(response.json())
                return

            except requests.HTTPError as exc:
                if exc.response is not None and 400 <= exc.response.status_code < 500:
                    if attempt < max_attempts:
                        logger.info("SchwabAuthManager: backing off for %.1fs before retry...", backoff_sec)
                        time.sleep(backoff_sec)
                        continue
                    else:
                        self.auth_circuit_open = True
                        self.auth_circuit_failure_count += 1
                        self._last_trip_time = time.monotonic()
                        logger.critical(
                            "SchwabAuthManager: auth circuit breaker TRIPPED (AUTH_LOCKED) after %d consecutive 4xx errors: %s",
                            max_attempts, exc,
                        )
                        try:
                            from core.notifier import send_alert
                            send_alert(
                                title="Auth Circuit Breaker TRIPPED!",
                                message=f"Auth circuit breaker TRIPPED after {max_attempts} consecutive 4xx errors: {exc}",
                                priority="urgent",
                                tags=["rotating_light", "warning"],
                            )
                        except Exception:
                            pass
                        raise AuthCircuitBreakerError(
                            f"OAuth token refresh returned HTTP {exc.response.status_code}. "
                            "Auth circuit breaker TRIPPED (AUTH_LOCKED)."
                        ) from exc
                else:
                    raise

    def _ingest_token_response(self, token_data: dict) -> None:
        """
        Parse the OAuth token response and update in-memory state + vault.
        Safe to call with or without the lock held (load_tokens calls it
        without the lock during init).
        """
        previous_refresh    = self._refresh_token
        self._access_token  = token_data["access_token"]
        self._refresh_token = token_data.get("refresh_token", self._refresh_token)
        expires_in          = int(token_data.get("expires_in", self._access_ttl))
        self._access_token_expiry = time.monotonic() + expires_in
        if self._refresh_issued_at is None or self._refresh_token != previous_refresh:
            self._refresh_issued_at = time.time()

        vault_payload = {
            "access_token":  self._access_token,
            "refresh_token": self._refresh_token,
            "persisted_at":  datetime.utcnow().isoformat() + "Z",
            "expires_in":    expires_in,
            "refresh_token_issued_at": self._refresh_issued_at,
        }
        self._vault.save(vault_payload)
        logger.info(
            "SchwabAuthManager: access token refreshed, expires in %ds.",
            expires_in,
        )

    def _refresh_loop(self) -> None:
        """Background daemon: poll every 60 seconds and refresh when needed."""
        while not self._stop_event.wait(timeout=60):
            if self.auth_circuit_open:
                logger.debug("SchwabAuthManager: refresh loop idle while auth circuit breaker is OPEN.")
                continue
            try:
                with self._lock:
                    # Skip refresh attempt if no refresh token yet (pre-OAuth state)
                    if self._refresh_token is None:
                        continue
                    if self._is_token_expired():
                        self._do_refresh()
            except AuthCircuitBreakerError as exc:
                logger.error(
                    "SchwabAuthManager: circuit breaker tripped in background refresh: %s", exc
                )
            except requests.HTTPError as exc:
                logger.error(
                    "SchwabAuthManager: HTTP error during background refresh: %s", exc
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "SchwabAuthManager: unexpected error in refresh loop: %s", exc
                )



# ===========================================================================
# WeekendOAuthDaemon
# ===========================================================================

class WeekendOAuthDaemon:
    """
    Weekend OAuth Maintenance Daemon.

    Every Saturday at 10:00 AM EDT, the Schwab refresh token (7-day TTL)
    must be rotated by completing a full Authorization Code Grant flow.
    This daemon:
        1. Calculates the next Saturday 10:00 AM EDT.
        2. Sleeps until that moment.
        3. Spins up a local HTTPS loopback server on 127.0.0.1:5556.
        4. Logs a browser URL for the operator to visit.
        5. Captures the inbound ?code=<auth_code> callback.
        6. Decodes %40 → @ and calls auth_manager.exchange_authorization_code().
        7. Shuts down the loopback server and schedules the next rotation.

    Configuration sources:
        auth.callback_host         → "127.0.0.1"
        auth.callback_port         → 5556
        auth.weekend_reauth_day    → "Saturday"
        auth.weekend_reauth_time   → "10:00:00"
        api.callback_url           → "https://127.0.0.1:5556"
    """

    def __init__(
        self,
        auth_manager: SchwabAuthManager,
        client_id: str,
        cfg: dict,
        ssl_cert_path: Optional[str] = None,
        ssl_key_path: Optional[str] = None,
    ) -> None:
        """
        Args:
            auth_manager:   The live SchwabAuthManager to hand tokens to.
            client_id:      Schwab app Client ID (for building the auth URL).
            cfg:            Top-level config dict from config.yaml.
            ssl_cert_path:  Path to the self-signed TLS certificate (PEM).
                            If None, the server runs over plain HTTP (dev only).
            ssl_key_path:   Path to the corresponding private key (PEM).
        """
        self._auth_manager = auth_manager
        self._client_id    = client_id
        self._ssl_cert     = ssl_cert_path
        self._ssl_key      = ssl_key_path

        auth_cfg = cfg.get("auth", {})
        api_cfg  = cfg.get("api",  {})

        self._host:          str = auth_cfg.get("callback_host", "127.0.0.1")
        self._port:          int = int(auth_cfg.get("callback_port", 5556))
        self._reauth_day:    str = auth_cfg.get("weekend_reauth_day", "Saturday")
        self._reauth_time:   str = auth_cfg.get("weekend_reauth_time", "10:00:00")
        self._redirect_uri:  str = api_cfg.get("callback_url", f"https://127.0.0.1:{self._port}")
        self._base_url:      str = api_cfg.get("base_url", "https://api.schwabapi.com")

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        logger.info(
            "WeekendOAuthDaemon initialised — will trigger every %s at %s EDT on %s:%d",
            self._reauth_day, self._reauth_time, self._host, self._port,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._daemon_loop,
            name="WeekendOAuthDaemon",
            daemon=True,
        )
        self._thread.start()
        logger.info("WeekendOAuthDaemon: daemon thread started.")

    def stop(self) -> None:
        """Stop the daemon thread cleanly."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("WeekendOAuthDaemon: daemon thread stopped.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_trigger_time(self) -> datetime:
        """
        Calculate the next wall-clock datetime for the scheduled re-auth.

        Returns the next occurrence of `self._reauth_day` at `self._reauth_time`
        EDT. If today is the correct day but the window is in the past, returns
        the same day next week.
        """
        day_map = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6,
        }
        target_weekday = day_map[self._reauth_day]
        h, m, s = [int(x) for x in self._reauth_time.split(":")]

        now_edt = datetime.now(_EDT)
        days_ahead = (target_weekday - now_edt.weekday()) % 7

        candidate = now_edt.replace(hour=h, minute=m, second=s, microsecond=0)
        if days_ahead == 0 and now_edt >= candidate:
            days_ahead = 7  # Already past this week's window; schedule next week

        return candidate + timedelta(days=days_ahead)

    def _sleep_until(self, target: datetime) -> bool:
        """
        Sleep in short intervals until `target` or until stop is requested.

        Returns:
            True  — woke up naturally at the target time.
            False — stop_event was set; caller should exit.
        """
        while True:
            now = datetime.now(_EDT)
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                return True
            if self._stop_event.wait(timeout=min(remaining, 60)):
                return False  # Stop requested

    def _build_auth_url(self) -> str:
        """Construct the Schwab OAuth authorization URL."""
        params = urllib.parse.urlencode({
            "client_id":     self._client_id,
            "redirect_uri":  self._redirect_uri,
            "response_type": "code",
        })
        return f"{self._base_url}/v1/oauth/authorize?{params}"

    def _capture_auth_code(self) -> Optional[str]:
        """
        Start a single-request HTTP server on the loopback address.
        Waits for the OAuth callback, extracts and returns the raw auth code.
        Returns None if an error occurs.
        """
        captured_code: list[Optional[str]] = [None]
        server_ready  = threading.Event()
        server_done   = threading.Event()

        class _CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # Silence default server logging
                logger.debug("OAuthCallback: " + fmt, *args)

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)

                if "code" in params:
                    # Spec: decode %40 → @ before use
                    raw_code = params["code"][0]
                    decoded  = urllib.parse.unquote(raw_code)
                    captured_code[0] = decoded
                    logger.info("WeekendOAuthDaemon: authorization code captured.")
                    body = b"<html><body><h2>Authorization successful. You may close this tab.</h2></body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Missing authorization code.")

                server_done.set()

        httpd = HTTPServer((self._host, self._port), _CallbackHandler)

        # Optionally wrap with TLS
        if self._ssl_cert and self._ssl_key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=self._ssl_cert, keyfile=self._ssl_key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            logger.info(
                "WeekendOAuthDaemon: HTTPS loopback server listening on %s:%d",
                self._host, self._port,
            )
        else:
            logger.warning(
                "WeekendOAuthDaemon: TLS cert/key not provided — HTTP loopback only. "
                "Use HTTPS in production (Schwab requires HTTPS callback URI)."
            )

        server_thread = threading.Thread(
            target=lambda: (server_ready.set(), httpd.handle_request()),
            daemon=True,
        )
        server_thread.start()
        server_ready.wait(timeout=2)

        auth_url = self._build_auth_url()
        logger.info(
            "\n"
            "=====================================================================\n"
            "  WeekendOAuthDaemon: ACTION REQUIRED\n"
            "  Open the following URL in your browser to re-authorize:\n"
            "  %s\n"
            "=====================================================================",
            auth_url,
        )

        # Wait up to 30 minutes for the operator to complete the browser flow
        if not server_done.wait(timeout=1800):
            logger.error(
                "WeekendOAuthDaemon: timed out waiting for OAuth callback (10 min)."
            )
            httpd.server_close()
            return None

        httpd.server_close()
        return captured_code[0]

    def _run_rotation(self) -> None:
        auth_code = self._capture_auth_code()
        if auth_code:
            try:
                self._auth_manager.exchange_authorization_code(
                    auth_code=auth_code,
                    redirect_uri=self._redirect_uri,
                )
                logger.info(
                    "WeekendOAuthDaemon: refresh token successfully rotated/saved."
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "WeekendOAuthDaemon: token exchange failed: %s", exc
                )
        else:
            logger.error("WeekendOAuthDaemon: no authorization code received.")

    def _daemon_loop(self) -> None:
        """Main daemon loop: sleep → capture → exchange → repeat."""
        # Retry initial auth every 30 minutes until tokens arrive.
        # This ensures the engine re-opens the listener automatically
        # after each timeout so the operator doesn't need to restart.
        while not self._stop_event.is_set():
            if self._auth_manager._refresh_token is not None:
                break
            logger.info(
                "WeekendOAuthDaemon: initial auth required — opening listener."
            )
            self._run_rotation()
            if self._auth_manager._refresh_token is not None:
                break
            if not self._stop_event.is_set():
                logger.info(
                    "WeekendOAuthDaemon: auth not received. "
                    "Re-opening listener in 30 minutes..."
                )
                self._stop_event.wait(timeout=1800)

        # Normal weekly rotation loop once tokens are established
        while not self._stop_event.is_set():
            next_run = self._next_trigger_time()
            logger.info(
                "WeekendOAuthDaemon: next token rotation scheduled for %s EDT",
                next_run.strftime("%Y-%m-%d %H:%M:%S %Z"),
            )

            if not self._sleep_until(next_run):
                break  # Stop was requested

            logger.info("WeekendOAuthDaemon: token rotation window reached.")
            self._run_rotation()
