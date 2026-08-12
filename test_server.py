from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from collections import namedtuple
from unittest import mock

sys.modules.setdefault("MetaTrader5", types.SimpleNamespace())
os.environ.setdefault("MT5_TERMINAL", "terminal64.exe")
os.environ.setdefault("MT5_INITIAL_FROM", "2020-01-01T00:00:00+00:00")
os.environ.setdefault("BRIDGE_HOST", "127.0.0.1")
os.environ.setdefault("BRIDGE_PORT", "18813")
os.environ.setdefault("BRIDGE_TOKEN", "test-token")
server = importlib.import_module("server")

Deal = namedtuple("Deal", "ticket order position_id time time_msc type entry magic reason volume price commission swap profit fee symbol comment external_id")
Order = namedtuple("Order", "ticket position_id time_setup time_setup_msc time_done time_done_msc type state reason volume_initial volume_current price_open sl tp price_current price_stoplimit symbol comment external_id")
Account = namedtuple("Account", "login balance currency currency_digits")


class BridgeV5Tests(unittest.TestCase):
    def request(self, **extra):
        return {"contractVersion": 5, "server": "Broker-Server", "accountLogin": 1,
                "password": "password", "mode": "bootstrap", "snapshotToMsc": 10000, **extra}

    def cursor(self, **extra):
        return server._encode_cursor(token="secret", mode="incremental", server="Broker-Server",
            account_login=1, snapshot_to_msc=10000, deal_key=(2, 3), order_key=(4, 5),
            changed_since_msc=100, open_position_ids=("7", "8"), **extra)


    def sync_pages(self, request, deals, orders, *, position_history=None):
        pages = []
        page_cursor = None

        def history_deals_get(*args, **kwargs):
            if "position" in kwargs:
                return position_history[kwargs["position"]][0]
            return deals

        def history_orders_get(*args, **kwargs):
            if "position" in kwargs:
                return position_history[kwargs["position"]][1]
            return orders

        with (
            mock.patch.object(server, "_login_for_sync"),
            mock.patch.object(
                server, "_get_config",
                return_value=types.SimpleNamespace(bridge_token="test-token"),
            ),
            mock.patch.object(server.mt5, "history_deals_get", side_effect=history_deals_get, create=True),
            mock.patch.object(server.mt5, "history_orders_get", side_effect=history_orders_get, create=True),
            mock.patch.object(server.mt5, "account_info", return_value=Account(1, 12, "USD", 2), create=True),
            mock.patch.object(server.mt5, "symbol_info_tick", return_value=types.SimpleNamespace(last=1, bid=1, ask=1), create=True),
        ):
            while True:
                handler = object.__new__(server.Mt5BridgeHandler)
                handler._read_json_body = lambda: {**request, **({"pageCursor": page_cursor} if page_cursor else {})}
                handler._send_json = lambda status, body: pages.append((status, body))
                handler._handle_sync()
                page = pages[-1][1]
                if not page["page"]["hasMore"]:
                    break
                page_cursor = page["page"]["nextCursor"]
        return [body for _, body in pages]

    def deal(self, ticket, time_msc, position_id=1, comment=""):
        return Deal(ticket, ticket, position_id, time_msc // 1000, time_msc, 0, 0, 0, 0,
                    1, 1, 0, 0, 0, 0, "X", comment, "")

    def order(self, ticket, time_msc, position_id=1, comment=""):
        return Order(ticket, position_id, time_msc // 1000, time_msc, time_msc // 1000,
                     time_msc, 0, 0, 0, 1, 0, 1, 0, 0, 1, 0, "X", comment, "")

    def sync(self, payload, deals, orders, *, position_deals=None, position_orders=None):
        handler = object.__new__(server.Mt5BridgeHandler)
        handler._read_json_body = lambda: payload
        sent = []
        handler._send_json = lambda status, body: sent.append((status, body))

        def history_deals(*args, **kwargs):
            return (position_deals or {}).get(kwargs["position"], []) if "position" in kwargs else deals

        def history_orders(*args, **kwargs):
            return (position_orders or {}).get(kwargs["position"], []) if "position" in kwargs else orders

        with (
            mock.patch.object(server, "_login_for_sync"),
            mock.patch.object(server, "_get_config", return_value=types.SimpleNamespace(bridge_token="test-token")),
            mock.patch.object(server.mt5, "history_deals_get", side_effect=history_deals, create=True),
            mock.patch.object(server.mt5, "history_orders_get", side_effect=history_orders, create=True),
            mock.patch.object(server.mt5, "account_info", return_value=Account(1, 12, "USD", 2), create=True),
        ):
            handler._handle_sync()
        self.assertEqual(sent[0][0], server.HTTPStatus.OK)
        return sent[0][1]

    def test_cursor_is_signed_request_bound_and_opaque(self):
        cursor = self.cursor()
        self.assertEqual(server._decode_cursor(cursor, token="secret", mode="incremental", server="Broker-Server", account_login=1, snapshot_to_msc=10000, changed_since_msc=100, open_position_ids=("7", "8")), ((2, 3), (4, 5)))
        with self.assertRaisesRegex(ValueError, "invalid or expired cursor"):
            server._decode_cursor(cursor, token="secret", mode="bootstrap", server="Broker-Server", account_login=1, snapshot_to_msc=10000, changed_since_msc=None, open_position_ids=())
        with self.assertRaisesRegex(ValueError, "invalid or expired cursor"):
            server._decode_cursor(cursor[:-1] + "A", token="secret", mode="incremental", server="Broker-Server", account_login=1, snapshot_to_msc=10000, changed_since_msc=100, open_position_ids=("7", "8"))

    def test_incremental_open_ids_are_canonical_and_validated(self):
        self.assertEqual(server._positive_id_strings(["10", "2"]), ("2", "10"))
        for value in (None, ["0"], ["01"], ["1", "1"], [1]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    server._positive_id_strings(value)

    def test_stable_sort_and_dedupe_by_ticket(self):
        first = Deal(2, 1, 1, 1, 10, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, "X", "", "")
        duplicate = Deal(2, 1, 1, 1, 20, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, "X", "", "")
        earlier = Deal(1, 1, 1, 1, 5, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, "X", "", "")
        self.assertEqual([item.ticket for item in server._dedupe_sorted([first, duplicate, earlier], server._deal_key)], [1, 2])

    def test_sync_bootstrap_response_has_fixed_page_bytes(self):
        handler = object.__new__(server.Mt5BridgeHandler)
        handler._read_json_body = lambda: self.request()
        sent = []
        handler._send_json = lambda status, body: sent.append((status, body))
        with (mock.patch.object(server, "_login_for_sync"),
              mock.patch.object(server, "_get_config", return_value=types.SimpleNamespace(bridge_token="test-token")),
              mock.patch.object(server.mt5, "history_deals_get", return_value=[], create=True),
              mock.patch.object(server.mt5, "history_orders_get", return_value=[], create=True),
              mock.patch.object(server.mt5, "account_info", return_value=Account(1, 12, "USD", 2), create=True)):
            handler._handle_sync()
        _, body = sent[0]
        self.assertEqual(body["contractVersion"], 5)
        self.assertEqual(body["mode"], "bootstrap")
        self.assertEqual(body["snapshotToMsc"], 10000)
        self.assertFalse(body["page"]["hasMore"])
        self.assertNotIn("nextCursor", body["page"])
        self.assertEqual(body["page"]["bytes"], len(server._compact_json(body)))
        self.assertLess(body["page"]["bytes"], 1024 * 1024)

    def test_incremental_requires_watermark_and_ids(self):
        handler = object.__new__(server.Mt5BridgeHandler)
        handler._read_json_body = lambda: self.request(mode="incremental")
        with self.assertRaisesRegex(ValueError, "changedSinceMsc"):
            handler._handle_sync()
        handler._read_json_body = lambda: self.request(changedSinceMsc=1, openPositionIds=["1"], mode="bootstrap")
        with self.assertRaisesRegex(ValueError, "incremental-only"):
            handler._handle_sync()

    def test_bootstrap_pages_complete_large_payload_without_gaps_or_duplicates(self):
        comment = "x" * 4096
        deals = [self.deal(ticket, 1000 + ticket, comment=comment) for ticket in range(1, 450)]
        pages = self.sync_pages(self.request(), deals, [])
        tickets = [fact["ticket"] for page in pages for fact in page["deals"]]
        self.assertGreater(len(pages), 1)
        self.assertEqual(tickets, [str(ticket) for ticket in range(1, 450)])
        self.assertEqual(len(tickets), len(set(tickets)))
        for index, page in enumerate(pages):
            self.assertEqual(page["page"]["bytes"], len(server._compact_json(page)))
            self.assertLess(page["page"]["bytes"], 1024 * 1024)
            self.assertLessEqual(page["page"]["bytes"], server.SYNC_RESPONSE_TARGET_BYTES)
            self.assertEqual(page["page"]["hasMore"], index < len(pages) - 1)
            self.assertEqual("nextCursor" in page["page"], index < len(pages) - 1)

    def test_cursor_continuation_preserves_equal_time_ticket_boundaries(self):
        comment = "x" * 8192
        deals = [self.deal(ticket, 5000, comment=comment) for ticket in range(1, 160)]
        orders = [self.order(ticket, 5000, comment=comment) for ticket in range(160, 320)]
        pages = self.sync_pages(self.request(), deals, orders)
        facts = [
            (fact["ticket"], "deal") for page in pages for fact in page["deals"]
        ] + [
            (fact["ticket"], "order") for page in pages for fact in page["orders"]
        ]
        self.assertEqual({ticket for ticket, _ in facts}, {str(ticket) for ticket in range(1, 320)})
        self.assertEqual(len(facts), 319)
        self.assertGreater(len(pages), 1)

    def test_incremental_unions_changed_window_open_positions_and_dedupes_tickets(self):
        changed_deal = self.deal(10, 9000, position_id=7)
        changed_order = self.order(20, 9000, position_id=7)
        historic_deal = self.deal(1, 1000, position_id=7)
        historic_order = self.order(2, 1000, position_id=7)
        pages = self.sync_pages(
            self.request(mode="incremental", changedSinceMsc=8000, openPositionIds=["7"]),
            [changed_deal],
            [changed_order],
            position_history={7: ([historic_deal, changed_deal], [historic_order, changed_order])},
        )
        self.assertEqual([fact["ticket"] for page in pages for fact in page["deals"]], ["1", "10"])
        self.assertEqual([fact["ticket"] for page in pages for fact in page["orders"]], ["2", "20"])

    def test_bootstrap_pagination_is_complete_at_equal_time_ticket_boundaries(self):
        # Each comment is deliberately large enough to force several target-bounded pages.
        comment = "x" * 220_000
        deals = [self.deal(ticket, 1000, comment=comment) for ticket in range(1, 7)]
        orders = [self.order(ticket, 1000, comment=comment) for ticket in range(10, 16)]
        payload = self.request()
        deal_tickets, order_tickets, page_count = [], [], 0
        while True:
            body = self.sync(payload, deals, orders)
            page_count += 1
            self.assertLess(body["page"]["bytes"], 1024 * 1024)
            self.assertEqual(body["page"]["bytes"], len(server._compact_json(body)))
            deal_tickets.extend(fact["ticket"] for fact in body["deals"])
            order_tickets.extend(fact["ticket"] for fact in body["orders"])
            if not body["page"]["hasMore"]:
                self.assertNotIn("nextCursor", body["page"])
                break
            self.assertTrue(body["page"]["nextCursor"])
            payload = self.request(pageCursor=body["page"]["nextCursor"])
        self.assertGreater(page_count, 1)
        self.assertEqual(deal_tickets, [str(ticket) for ticket in range(1, 7)])
        self.assertEqual(order_tickets, [str(ticket) for ticket in range(10, 16)])
        self.assertEqual(len(deal_tickets), len(set(deal_tickets)))
        self.assertEqual(len(order_tickets), len(set(order_tickets)))

    def test_capabilities_advertise_independent_bounded_tick_contract(self):
        handler = object.__new__(server.Mt5BridgeHandler)
        sent = []
        handler._send_json = lambda status, body: sent.append((status, body))
        handler._handle_capabilities()
        status, body = sent[0]
        self.assertEqual(status, server.HTTPStatus.OK)
        self.assertEqual(body["contractVersion"], 5)
        self.assertEqual(body["ticks"]["cursorNamespace"], "ticks-v1")
        self.assertEqual(body["ticks"]["maxChunkSpanMsc"], 300000)
        self.assertLess(body["ticks"]["maxResponseBytes"], 1024 * 1024)
        self.assertEqual(body["ticks"]["supportedCalculationModes"], list(server.SUPPORTED_CALCULATION_MODE_NAMES))
        self.assertIn("CFDLEVERAGE", body["ticks"]["supportedCalculationModes"])

    def test_tick_pages_are_immutable_request_bound_and_cursor_isolated(self):
        Tick = namedtuple("Tick", "time_msc bid ask")
        ticks = [Tick(1000 + index, 100 + index, 101 + index) for index in range(3)]
        payload = {
            "contractVersion": 5, "server": "Broker-Server", "accountLogin": 1,
            "password": "password", "symbol": "XAUUSD",
            "rawRange": {"fromMsc": 1000, "toMsc": 2000},
            "snapshotToMsc": 3000, "pageSize": 2,
        }
        server.TICK_CACHE.clear()
        server.TICK_CACHE_BYTES = 0
        handler = object.__new__(server.Mt5BridgeHandler)
        sent = []
        handler._read_json_body = lambda: payload
        handler._send_json = lambda status, body: sent.append((status, body))
        with (
            mock.patch.object(server, "_get_config", return_value=types.SimpleNamespace(bridge_token="test-token")),
            mock.patch.object(server, "_login_for_sync"),
            mock.patch.object(server.mt5, "COPY_TICKS_ALL", 3, create=True),
            mock.patch.object(server.mt5, "SYMBOL_CALC_MODE_FOREX", 0, create=True),
            mock.patch.object(server.mt5, "copy_ticks_range", return_value=ticks, create=True) as copy_ticks,
            mock.patch.object(server.mt5, "account_info", return_value=Account(1, 12, "USD", 2), create=True),
            mock.patch.object(server.mt5, "symbol_info_tick", return_value=types.SimpleNamespace(last=1, bid=1, ask=1), create=True),
            mock.patch.object(server.mt5, "symbol_info", return_value=types.SimpleNamespace(
                trade_calc_mode=0, currency_profit="USD", trade_tick_size=0.01,
                trade_tick_value_profit=1, trade_tick_value_loss=1,
            ), create=True),
            mock.patch.object(
                server.mt5, "order_calc_profit",
                side_effect=lambda order_type, _symbol, _lots, opened, closed:
                    (closed - opened) / 0.01 * (1 if order_type == 0 else -1),
                create=True,
            ),
        ):
            handler._handle_ticks()
            first = sent[-1][1]
            self.assertFalse(first["complete"])
            self.assertEqual([tick["sequence"] for tick in first["ticks"]], [0, 1])
            payload = {"contractVersion": 5, "pageCursor": first["nextCursor"]}
            handler._handle_ticks()
            second = sent[-1][1]
        self.assertTrue(second["complete"])
        self.assertEqual([tick["sequence"] for tick in second["ticks"]], [2])
        copy_ticks.assert_called_once()
        self.assertEqual(first["snapshot"]["id"], second["snapshot"]["id"])
        with self.assertRaisesRegex(ValueError, "invalid_or_expired_tick_cursor"):
            server._decode_tick_cursor(
                server._encode_cursor(
                    token="test-token", mode="bootstrap", server="Broker-Server",
                    account_login=1, snapshot_to_msc=3000, deal_key=None, order_key=None,
                    changed_since_msc=None, open_position_ids=(),
                ),
                "test-token",
            )

    def test_tick_snapshot_digest_is_stable_and_price_sensitive(self):
        ticks = [{"sequence": 0, "timeMsc": 1000, "bid": "100", "ask": "101"}]
        self.assertEqual(server._tick_snapshot_digest(ticks), server._tick_snapshot_digest(list(ticks)))
        self.assertNotEqual(
            server._tick_snapshot_digest(ticks),
            server._tick_snapshot_digest([{**ticks[0], "ask": "102"}]),
        )

if __name__ == "__main__":
    unittest.main()
