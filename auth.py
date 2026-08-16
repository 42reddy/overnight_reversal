"""
auth.py  —  Upstox headless TOTP login
────────────────────────────────────────
Uses the `upstox-totp` community package for clean headless auth.
Caches the daily token to disk. Token valid until 3:30 AM next day.

Upstox's SANDBOX environment only serves order/portfolio APIs — it has no
market data (no historical candles, no quotes) at all, in or out of market
hours. So when SANDBOX is enabled, bot.py pairs a sandbox order client
(get_client, mode="sandbox") with a second LIVE-mode client used only for
data (get_client(cfg, mode="live")), which needs real mobile/password/pin/
totp_secret in config.ini. Orders still only ever go through the sandbox
client, so nothing gets placed for real.

Install: pip install upstox-totp upstox-python-sdk
"""

import json
import logging
import os
import time
from configparser import ConfigParser
from datetime import date

import net_ipv4  # noqa: F401 — pins outbound calls to IPv4 for Upstox's static-IP whitelist
import upstox_client
from upstox_totp import UpstoxTOTP
from upstox_totp.models import AccessTokenData, AccessTokenResponse

logger = logging.getLogger(__name__)

# upstox-totp 1.0.8 (latest on PyPI) declares AccessTokenData.poa as a
# required bool, but Upstox's live API response no longer includes "poa" —
# validation fails with "data.poa Field required" on every login. Patch the
# field to be optional until upstream fixes it. AccessTokenResponse is the
# generic wrapper (ResponseBase[AccessTokenData]) and caches its own schema
# snapshot at import time, so it must be rebuilt too or the patch won't
# propagate to it.
AccessTokenData.model_fields["poa"].default = None
AccessTokenData.model_fields["poa"].annotation = bool | None
AccessTokenData.model_rebuild(force=True)
AccessTokenResponse.model_rebuild(force=True)


def _token_file_for(cfg: ConfigParser, mode: str) -> str:
    """Sandbox and live tokens are cached separately so running one doesn't
    clobber the other's cache (both may be in use in the same process)."""
    base = cfg["PATHS"]["token_file"]
    root, ext = os.path.splitext(base)
    return f"{root}_{mode}{ext}"


def get_client(cfg: ConfigParser, mode: str = None) -> tuple[upstox_client.ApiClient, str]:
    """
    Returns (ApiClient, access_token).

    mode: "sandbox" or "live" — overrides [SANDBOX] enabled in config.ini.
    None (default) follows [SANDBOX] enabled as before.

    The ApiClient is pre-configured and ready to pass to any Upstox API class.
    """
    sandbox = (mode == "sandbox") if mode is not None else \
        cfg["SANDBOX"].getboolean("enabled", fallback=True)
    token_file = _token_file_for(cfg, "sandbox" if sandbox else "live")
    os.makedirs(os.path.dirname(token_file), exist_ok=True)

    today        = date.today().isoformat()
    access_token = None

    # ── Try cached token ─────────────────────────────────────────
    if os.path.exists(token_file):
        try:
            with open(token_file) as f:
                cache = json.load(f)
            if cache.get("date") == today:
                access_token = cache["access_token"]
                logger.info("Using cached access_token from today")
        except Exception as e:
            logger.warning(f"Token cache read failed: {e}")

    # ── Fresh login if needed ────────────────────────────────────
    if not access_token:
        if sandbox:
            # 1. Pull the manual token from env var or config
            access_token = os.environ.get("UPSTOX_SANDBOX_TOKEN") or cfg["UPSTOX"].get("sandbox_token")
            if not access_token:
                raise RuntimeError("SANDBOX ENABLED: set UPSTOX_SANDBOX_TOKEN env var or add 'sandbox_token' in config.ini.")

            # 2. Update the cache
            with open(token_file, "w") as f:
                json.dump({"date": today, "access_token": access_token}, f)

            logger.info("Sandbox token applied and cached to disk")

        else:
            # LIVE MODE: Fresh TOTP login
            logger.info("No valid cached token — performing fresh TOTP login for LIVE mode")

            # upstox-totp reads credentials from env vars.
            # Env vars take priority (cloud secrets); config.ini values are local fallbacks.
            os.environ.setdefault("UPSTOX_USERNAME",     cfg["UPSTOX"].get("mobile", ""))
            os.environ.setdefault("UPSTOX_PASSWORD",     cfg["UPSTOX"].get("pin", ""))
            os.environ.setdefault("UPSTOX_PIN_CODE",     cfg["UPSTOX"].get("pin", ""))
            os.environ.setdefault("UPSTOX_TOTP_SECRET",  cfg["UPSTOX"].get("totp_secret", ""))
            os.environ.setdefault("UPSTOX_CLIENT_ID",    cfg["UPSTOX"].get("client_id", ""))
            os.environ.setdefault("UPSTOX_CLIENT_SECRET",cfg["UPSTOX"].get("client_secret", ""))
            os.environ.setdefault("UPSTOX_REDIRECT_URI", cfg["UPSTOX"].get("redirect_uri", ""))

            for attempt in range(1, 4):
                try:
                    upx      = UpstoxTOTP()
                    response = upx.app_token.get_access_token()
                    if not response.success or not response.data:
                        raise RuntimeError(f"upstox-totp returned failure: {response}")

                    access_token = response.data.access_token
                    logger.info(f"Logged in as {response.data.user_name} ({response.data.user_id})")
                    break
                except Exception as e:
                    logger.error(f"Login attempt {attempt} failed: {e}")
                    if attempt < 3:
                        time.sleep(10)
            else:
                raise RuntimeError("All login attempts failed — check credentials in config.ini")

            # Cache the live token
            with open(token_file, "w") as f:
                json.dump({"date": today, "access_token": access_token}, f)
            logger.info("access_token cached to disk")

    # ── Build ApiClient ──────────────────────────────────────────
    configuration = upstox_client.Configuration(sandbox=sandbox)
    configuration.access_token = access_token

    if sandbox:
        logger.info("★ SANDBOX MODE — no real orders will be placed ★")
    else:
        logger.info("★ LIVE MODE — real orders will be placed ★")

    api_client = upstox_client.ApiClient(configuration)
    return api_client, access_token


def get_analytics_client(cfg: ConfigParser) -> upstox_client.ApiClient:
    """
    Read-only client built from Upstox's long-lived (1-year) Analytics
    Access Token — no TOTP login involved, it's generated once by hand in
    the Upstox Developer Console and pasted into .env.

    Covers Market Data and Real-time/Streaming APIs (and, once a static IP
    is registered, Portfolio and Account & Funds read access). It cannot
    place, modify, or cancel orders — use get_client() for that.
    """
    token = os.environ.get("UPSTOX_ANALYTICS_TOKEN") or cfg["UPSTOX"].get("analytics_token")
    if not token:
        raise RuntimeError("Set UPSTOX_ANALYTICS_TOKEN env var or 'analytics_token' in config.ini "
                            "to use the Analytics Access Token for market data.")
    configuration = upstox_client.Configuration(sandbox=False)
    configuration.access_token = token
    return upstox_client.ApiClient(configuration)
