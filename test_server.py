from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
import json
from collections import namedtuple
from unittest import mock


sys.modules.setdefault("MetaTrader5", types.SimpleNamespace(
    DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1, DEAL_TYPE_BALANCE=2, DEAL_ENTRY_IN=0, DEAL_ENTRY_INOUT=2,
))
os.environ.setdefault("MT5_TERMINAL", r"C:\\Program Files\\MetaTrader 5\\terminal64.exe")
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
Account = namedtuple("Account", "login balance currency")




class BridgeV4Tests(unittest.TestCase):
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

        legacy_payload = json.dumps(
            {"v": 2, "d": deals_digest, "o": orders_digest},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        legacy_signature = server.hmac.new(b"secret", legacy_payload, server.hashlib.sha256).digest()
        legacy_cursor = server.base64.urlsafe_b64encode(legacy_payload + legacy_signature).decode("ascii").rstrip("=")
        self.assertEqual(server._decode_cursor(legacy_cursor, "secret"), ("", ""))

    def test_config_has_no_account_specific_credentials(self) -> None:
        server._get_config.cache_clear()
        try:
            config = server._get_config()
            self.assertFalse(hasattr(config, "mt5_login"))
            self.assertFalse(hasattr(config, "mt5_password"))
            self.assertFalse(hasattr(config, "mt5_server"))
            self.assertFalse(hasattr(config, "entry_balance_proof"))
        finally:
            server._get_config.cache_clear()

    def test_main_initializes_terminal_without_loading_an_account(self) -> None:
        config = mock.Mock(
            bridge_host="127.0.0.1",
            bridge_port=18813,
            mt5_initial_from=server.datetime(2020, 1, 1, tzinfo=server.UTC),
        )
        http_server = mock.Mock()
        with (
            mock.patch.object(server, "_get_config", return_value=config),
            mock.patch.object(server, "_initialize_mt5") as initialize,
            mock.patch.object(server, "ThreadingHTTPServer", return_value=http_server),
            mock.patch.object(server.mt5, "shutdown", create=True),
        ):
            http_server.serve_forever.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                server.main()
        initialize.assert_called_once_with(config)

    def test_account_balance_is_serialized_as_a_canonical_decimal(self) -> None:
        self.assertEqual(server._canonical_number(0.0), "0")
        self.assertEqual(server._canonical_number(111.25), "111.25")
        with self.assertRaisesRegex(RuntimeError, "balance is invalid"):
            server._canonical_number(float("nan"))

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

    def test_history_datetime_validates_each_boundary(self) -> None:
        expected = server.datetime(2025, 12, 11, 0, 10, 45, 123000, tzinfo=server.UTC)
        self.assertEqual(server._history_datetime(1_765_411_845_123, "historyToMsc"), expected)
        self.assertEqual(server._history_datetime(0, "historyFromMsc", allow_zero=True), server.datetime(1970, 1, 1, tzinfo=server.UTC))
        for invalid in (None, True, -1, 1.5, "1765411845123"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "historyFromMsc"):
                    server._history_datetime(invalid, "historyFromMsc", allow_zero=True)
    def test_sync_replays_each_digest_stream_independently(self) -> None:
        deals = [
            Deal(1, 1, 50, 1, 1000, 0, 0, 0, 0, 1.0, 100.0, -1.0, 0.0, 12.0, 0.0, "X", "", ""),
        ]
        orders = [
            Order(2, 50, 1, 1000, 2, 2000, 0, 0, 0, 1.0, 0.0, 100.0, 0.0, 0.0, 100.0, 0.0, "X", "", ""),
        ]
        serialized_deals = [server._serialize_deal(deal) for deal in deals]
        serialized_orders = [server._serialize_order(order) for order in orders]
        deals_digest = server._facts_digest(serialized_deals)
        orders_digest = server._facts_digest(serialized_orders)

        for name, deals_changed, orders_changed in (
            ("deals only", True, False),
            ("orders only", False, True),
            ("both", True, True),
            ("neither", False, False),
        ):
            with self.subTest(name=name):
                cursor = server._encode_cursor(
                    "0" * 64 if deals_changed else deals_digest,
                    "1" * 64 if orders_changed else orders_digest,
                    "test-token",
                )
                handler = object.__new__(server.Mt5BridgeHandler)
                handler._read_json_body = lambda: {
                    "server": "Broker-Server",
                    "accountLogin": 1,
                    "password": "password",
                    "cursor": cursor,
                    "historyFromMsc": 0,
                    "historyToMsc": 1_765_411_845_123,
                }
                response: list[tuple[object, dict[str, object]]] = []
                handler._send_json = lambda status, body: response.append((status, body))

                with (
                    mock.patch.object(server, "_login_for_sync") as login,
                    mock.patch.object(
                        server,
                        "_get_config",
                        return_value=types.SimpleNamespace(bridge_token="test-token"),
                    ),
                    mock.patch.object(server.mt5, "history_deals_get", return_value=deals, create=True),
                    mock.patch.object(server.mt5, "history_orders_get", return_value=orders, create=True),
                    mock.patch.object(server.mt5, "account_info", return_value=Account(1, 111.0, "USD"), create=True),
                ):
                    handler._handle_sync()
                    expected_history_from = server.datetime(1970, 1, 1, tzinfo=server.UTC)
                    expected_history_to = server.datetime(2025, 12, 11, 0, 10, 45, 123000, tzinfo=server.UTC)
                    server.mt5.history_deals_get.assert_called_once_with(expected_history_from, expected_history_to)
                    server.mt5.history_orders_get.assert_called_once_with(expected_history_from, expected_history_to)

                login.assert_called_once_with("Broker-Server", 1, "password")
                status, body = response[0]
                self.assertEqual(status, server.HTTPStatus.OK)
                self.assertEqual(body["contractVersion"], 4)
                self.assertEqual(body["server"], "Broker-Server")
                self.assertEqual(body["accountLogin"], 1)
                self.assertEqual(body["historyRange"], {
                    "fromMsc": 0,
                    "toMsc": 1_765_411_845_123,
                })
                self.assertEqual(body["account"], {
                    "currency": "USD",
                    "currentBalance": "111",
                })
                self.assertEqual(
                    body["cursor"],
                    server._encode_cursor(deals_digest, orders_digest, "test-token"),
                )
                self.assertEqual(body["deals"], serialized_deals if deals_changed else [])
                self.assertEqual(body["orders"], serialized_orders if orders_changed else [])


if __name__ == "__main__":
    unittest.main()
