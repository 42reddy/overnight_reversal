"""
net_ipv4.py  —  Force outbound HTTP calls over IPv4
─────────────────────────────────────────────────────
Upstox's static-IP whitelist is IPv4-only. On a dual-stack network, urllib3
(what `requests` and upstox_client's generated REST client both sit on)
will happily pick an IPv6 route to api.upstox.com if one exists, which
doesn't match the whitelisted address and gets every call rejected with
UDAPI1154 even though the whitelist itself is configured correctly.

Import this once, before any Upstox API calls, to pin urllib3's connection
resolution to IPv4 addresses only for the rest of the process.
"""

import socket

import urllib3.util.connection as urllib3_conn

_original_allowed_gai_family = urllib3_conn.allowed_gai_family


def _allowed_gai_family_ipv4():
    return socket.AF_INET


def force_ipv4():
    urllib3_conn.allowed_gai_family = _allowed_gai_family_ipv4


force_ipv4()
