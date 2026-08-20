"""
auth.py  —  Broker sessions: Kotak Neo (trading) + Upstox (market data)
─────────────────────────────────────────────────────────────────────────
Two independent brokers, two independent auth flows:

  get_kotak_client()    — Kotak Neo Trade API (neo_api_client / NeoAPI).
                           Headless TOTP + MPIN login, fresh every trading
                           day (the SDK keeps the resulting session tokens
                           inside the client object itself — there's no
                           portable access-token string to cache to disk
                           the way Upstox's is, so we just re-login each
                           run rather than half-caching one side of it).
                           This is the ONLY client that ever places, cancels,
                           or reads back real orders/positions.

  get_analytics_client() — Upstox's long-lived (1-year) Analytics Access
                           Token, generated once by hand in the Upstox
                           Developer Console and pasted into .env. Read-only,
                           covers Market Data + Real-time/Streaming APIs.
                           Used ONLY for prior-close/LTP pulls in
                           live_engine.py — never for orders. Since it's a
                           static long-lived token, there's no daily Upstox
                           login step anymore (the old full TOTP login for
                           order placement is gone along with the orders
                           themselves).

Install: pip install pyotp
         pip install "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client"
"""

import logging
import os
import time
from configparser import ConfigParser

import net_ipv4  # noqa: F401 — pins outbound calls to IPv4 (Upstox static-IP whitelist)
import pyotp
import upstox_client
from neo_api_client import NeoAPI

logger = logging.getLogger(__name__)


def _cfg_or_env(cfg: ConfigParser, section: str, env_var: str, ini_key: str) -> str:
    val = os.environ.get(env_var) or (cfg[section].get(ini_key, "") if cfg.has_section(section) else "")
    return val or ""


def get_kotak_client(cfg: ConfigParser) -> NeoAPI:
    """
    Fresh headless TOTP + MPIN login to Kotak Neo, every call. Returns a
    logged-in NeoAPI client ready for place_order / cancel_order /
    order_report / trade_report / positions.

    Credentials (env var takes priority over config.ini's [KOTAK] section,
    same pattern as the old Upstox login — env vars for cloud secrets,
    config.ini values as local fallbacks only):
        KOTAK_ACCESS_TOKEN    — the single token from Neo app's
            More → Trade API → Create New Application (labelled "API
            Access Token" / "Consumer Key" on screen — current Neo Trade
            API onboarding issues just this one token, not a separate
            key+secret pair). Passed to NeoAPI(access_token=...) — the
            SDK also supports a consumer_key/consumer_secret constructor
            for older app registrations, but isn't needed here.
        KOTAK_MOBILE_NUMBER   — registered mobile, e.g. "+919999999999"
        KOTAK_UCC             — your Kotak Neo client code
        KOTAK_MPIN            — trading MPIN
        KOTAK_TOTP_SECRET     — base32 TOTP secret from the same Trade API
            page's separate "register for TOTP" step (NOT the 6-digit
            code — the long base32 string), used to generate a fresh code
            with pyotp on every login

    Note: if Kotak enforces an expiry on the access token itself (the Trade
    API page may show a validity/"Generate Access Token" control), a login
    failure that specifically complains about the token/consumer key rather
    than TOTP/MPIN means it needs regenerating from the app.
    """
    access_token = _cfg_or_env(cfg, "KOTAK", "KOTAK_ACCESS_TOKEN", "access_token")
    mobile_number = _cfg_or_env(cfg, "KOTAK", "KOTAK_MOBILE_NUMBER", "mobile_number")
    ucc = _cfg_or_env(cfg, "KOTAK", "KOTAK_UCC", "ucc")
    mpin = _cfg_or_env(cfg, "KOTAK", "KOTAK_MPIN", "mpin")
    totp_secret = _cfg_or_env(cfg, "KOTAK", "KOTAK_TOTP_SECRET", "totp_secret")
    environment = cfg["KOTAK"].get("environment", "prod") if cfg.has_section("KOTAK") else "prod"

    missing = [name for name, val in [
        ("KOTAK_ACCESS_TOKEN", access_token),
        ("KOTAK_MOBILE_NUMBER", mobile_number), ("KOTAK_UCC", ucc),
        ("KOTAK_MPIN", mpin), ("KOTAK_TOTP_SECRET", totp_secret),
    ] if not val]
    if missing:
        raise RuntimeError(f"Kotak Neo login: missing {missing} — set as env var(s) or in "
                            f"config.ini's [KOTAK] section.")

    last_err = None
    for attempt in range(1, 4):
        try:
            client = NeoAPI(access_token=access_token, environment=environment)
            totp_code = pyotp.TOTP(totp_secret).now()
            login_resp = client.totp_login(mobile_number=mobile_number, ucc=ucc, totp=totp_code)
            if isinstance(login_resp, dict) and str(login_resp.get("stat", "")).lower() not in ("ok", ""):
                raise RuntimeError(f"totp_login failed: {login_resp}")

            validate_resp = client.totp_validate(mpin=mpin)
            if isinstance(validate_resp, dict) and str(validate_resp.get("stat", "")).lower() not in ("ok", ""):
                raise RuntimeError(f"totp_validate failed: {validate_resp}")

            logger.info(f"Kotak Neo login OK — ucc={ucc} environment={environment}")
            return client
        except Exception as e:
            last_err = e
            logger.error(f"Kotak Neo login attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(10)

    raise RuntimeError(f"All Kotak Neo login attempts failed — check KOTAK_* credentials. "
                        f"Last error: {last_err}")


def get_analytics_client(cfg: ConfigParser) -> upstox_client.ApiClient:
    """
    Read-only client built from Upstox's long-lived (1-year) Analytics
    Access Token — no TOTP login involved, it's generated once by hand in
    the Upstox Developer Console and pasted into .env.

    Covers Market Data and Real-time/Streaming APIs. Used exclusively for
    prior-close / LTP pulls (live_engine.py) — Upstox is never involved in
    order placement, which is entirely on Kotak Neo (see get_kotak_client).
    """
    token = os.environ.get("UPSTOX_ANALYTICS_TOKEN") or cfg["UPSTOX"].get("analytics_token")
    if not token:
        raise RuntimeError("Set UPSTOX_ANALYTICS_TOKEN env var or 'analytics_token' in config.ini "
                            "to use the Analytics Access Token for market data.")
    configuration = upstox_client.Configuration(sandbox=False)
    configuration.access_token = token
    return upstox_client.ApiClient(configuration)
