"""
test_order.py — one-off smoke test for order placement.

Places a single MARKET delivery (CNC) order for 1 share of YESBANK-EQ
(currently ~Rs 20-25) on NSE cash. Purpose is only to confirm the Kotak
Neo login + place_order path works end-to-end, without running the full
bot (bot.py) or risking a crash mid-session.

Run: python test_order.py
"""

import logging

from bot import load_config, setup_logging, _load_env
from auth import get_kotak_client

TEST_SYMBOL = "YESBANK-EQ"
EXCHANGE_SEGMENT = "nse_cm"

logger = logging.getLogger(__name__)


def main():
    _load_env()
    cfg = load_config()
    setup_logging(cfg["PATHS"].get("log_file", "logs/test_order.log"))
    client = get_kotak_client(cfg)

    resp = client.place_order(
        exchange_segment=EXCHANGE_SEGMENT,
        product="CNC",
        price="0",
        order_type="MKT",
        quantity="1",
        validity="DAY",
        trading_symbol=TEST_SYMBOL,
        transaction_type="B",
        amo="NO",
        disclosed_quantity="0",
        trigger_price="0",
    )

    print("Order response:", resp)

    if isinstance(resp, dict) and str(resp.get("stat", "")).lower() in ("ok", ""):
        order_id = resp.get("nOrdNo")
        print(f"Order placed OK. order_id={order_id}. Check the Kotak Neo order book to confirm the fill.")
    else:
        print("Order placement FAILED or response could not be parsed — check response above.")


if __name__ == "__main__":
    main()
