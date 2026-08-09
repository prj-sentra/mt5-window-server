from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from collections import namedtuple


sys.modules.setdefault("MetaTrader5", types.SimpleNamespace())
os.environ.setdefault("MT5_TERMINAL", r"C:\\Program Files\\MetaTrader 5\\terminal64.exe")
os.environ.setdefault("MT5_LOGIN", "1")
os.environ.setdefault("MT5_PASSWORD", "password")
os.environ.setdefault("MT5_SERVER", "Broker-Server")
os.environ.setdefault("MT5_INITIAL_FROM", "2020-01-01T00:00:00+00:00")
os.environ.setdefault("BRIDGE_HOST", "127.0.0.1")
os.environ.setdefault("BRIDGE_PORT", "18813")
os.environ.setdefault("BRIDGE_TOKEN", "test-token")

server = importlib.import_module("server")

Deal = namedtuple(
    "Deal",
    "ticket order position_id time time_msc type entry magic reason volume price commission swap profit fee symbol comment external_id",
)
Order = namedtuple(
    "Order",
    "ticket position_id time_setup time_setup_msc time_done time_done_msc type state reason volume_initial volume_current price_open sl tp price_current price_stoplimit symbol comment external_id",
)


class BridgeV2Tests(unittest.TestCase):
    def test_cursor_is_signed_and_round_trips(self) -> None:
        deals_digest = "a" * 64
        orders_digest = "b" * 64
        cursor = server._encode_cursor(deals_digest, orders_digest, "secret")
        self.assertEqual(
            server._decode_cursor(cursor, "secret"),
            (deals_digest, orders_digest),
        )
        with self.assertRaisesRegex(ValueError, "invalid or expired cursor"):
            server._decode_cursor(cursor, "different-secret")

    def test_position_balance_is_balance_before_first_entry(self) -> None:
        deals = [
            Deal(1, 1, 50, 1, 1000, 0, 0, 0, 0, 1.0, 100.0, -1.0, 0.0, 0.0, 0.0, "X", "", ""),
            Deal(2, 2, 50, 2, 2000, 1, 1, 0, 0, 1.0, 110.0, -1.0, 0.0, 12.0, 0.0, "X", "", ""),
        ]
        self.assertEqual(
            server._build_position_balances(deals, 110.0),
            [{"positionId": "50", "preEntryBalance": 100.0}],
        )

    def test_credit_does_not_corrupt_reconstructed_balance(self) -> None:
        deals = [
            Deal(1, 1, 50, 1, 1000, 0, 0, 0, 0, 1.0, 100.0, -1.0, 0.0, 0.0, 0.0, "X", "", ""),
            Deal(2, 2, 50, 2, 2000, 1, 1, 0, 0, 1.0, 110.0, -1.0, 0.0, 12.0, 0.0, "X", "", ""),
            Deal(3, 0, 0, 3, 3000, 3, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 50.0, 0.0, "", "credit", ""),
        ]
        self.assertEqual(
            server._build_position_balances(deals, 110.0),
            [{"positionId": "50", "preEntryBalance": 100.0}],
        )

    def test_fact_digest_detects_historical_mutation(self) -> None:
        original = [{"ticket": "1", "type": 0, "profit": 10.0}]
        revised = [{"ticket": "1", "type": 2, "profit": 0.0}]
        self.assertNotEqual(server._facts_digest(original), server._facts_digest(revised))
        self.assertEqual(server._facts_digest(original), server._facts_digest(list(original)))

    def test_serializers_match_api_contract(self) -> None:
        deal = Deal(1, 2, 3, 4, 5, 6, 0, 7, 8, 0.1, 9.0, -1.0, 0.0, 2.0, 0.0, "X", "c", "e")
        order = Order(1, 3, 4, 5, 6, 7, 8, 9, 10, 0.2, 0.0, 11.0, 10.0, 12.0, 11.0, 0.0, "X", "c", "e")
        self.assertEqual(server._serialize_deal(deal)["positionId"], "3")
        self.assertEqual(server._serialize_deal(deal)["externalId"], "e")
        self.assertEqual(server._serialize_order(order)["priceStopLimit"], 0.0)
        self.assertEqual(server._order_key(order), (7, 1))


if __name__ == "__main__":
    unittest.main()
