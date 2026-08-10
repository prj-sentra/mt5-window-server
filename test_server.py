from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
import json
import tempfile
from pathlib import Path
from collections import namedtuple
from unittest import mock


sys.modules.setdefault("MetaTrader5", types.SimpleNamespace(
    DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1, DEAL_TYPE_BALANCE=2, DEAL_ENTRY_IN=0, DEAL_ENTRY_INOUT=2,
))
os.environ.setdefault("MT5_TERMINAL", r"C:\\Program Files\\MetaTrader 5\\terminal64.exe")
os.environ.setdefault("MT5_LOGIN", "1")
os.environ.setdefault("MT5_PASSWORD", "password")
os.environ.setdefault("MT5_SERVER", "Broker-Server")
os.environ.setdefault("MT5_INITIAL_FROM", "2020-01-01T00:00:00+00:00")
os.environ.setdefault("BRIDGE_HOST", "127.0.0.1")
os.environ.setdefault("BRIDGE_PORT", "18813")
os.environ.setdefault("BRIDGE_TOKEN", "test-token")
os.environ.setdefault("MT5_ENTRY_BALANCE_PROOF_FILE", "test-proof.json")

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
def entry_balance_proof(**overrides: object) -> server.EntryBalanceProof:
    values: dict[str, object] = {
        "format_version": 1,
        "evidence_sha256": server.APPROVED_EVIDENCE_SHA256,
        "statement_report_sha256": server.APPROVED_REPORT_SHA256,
        "server": "Broker-Server",
        "account_login": 1,
        "currency": "USD",
        "baseline_balance": server.Decimal("100.00"),
        "baseline_deal_ticket": 1,
        "approval_timestamp": server.datetime(2026, 1, 1, tzinfo=server.UTC),
        "ledger_semantics_version": 1,
        "currency_scale": 2,
        "rounding_mode": "ROUND_HALF_UP",
        "deal_type_buy": 0,
        "deal_type_sell": 1,
        "deal_type_balance": 2,
        "deal_entry_in": 0,
        "deal_entry_inout": 2,
    }
    values.update(overrides)
    return server.EntryBalanceProof(**values)




class BridgeV3Tests(unittest.TestCase):
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

    def test_private_proof_schema_binds_approved_digests(self) -> None:
        payload = {
            "formatVersion": 1, "evidenceSha256": server.APPROVED_EVIDENCE_SHA256,
            "statementReportSha256": server.APPROVED_REPORT_SHA256,
            "server": "Broker-Server", "accountLogin": 1, "currency": "USD",
            "baselineBalance": "395.19", "baselineDealTicket": 224575884,
            "approvalTimestamp": "2026-01-01T00:00:00+00:00",
            "ledgerSemanticsVersion": 1, "currencyScale": 2, "roundingMode": "ROUND_HALF_UP",
            "dealTypeBuy": 0, "dealTypeSell": 1, "dealTypeBalance": 2, "dealEntryIn": 0, "dealEntryInOut": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proof.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(server._load_entry_balance_proof(path).baseline_balance, server.Decimal("395.19"))
            payload["statementReportSha256"] = "a" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid values"):
                server._load_entry_balance_proof(path)
            payload["statementReportSha256"] = server.APPROVED_REPORT_SHA256
            for invalid in ("+1", "-0", "1.0", "1e2", "1.1234567890123456789012345678901", "1" * 36):
                payload["baselineBalance"] = invalid
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "invalid values"):
                    server._load_entry_balance_proof(path)
            payload["baselineBalance"] = "1.12345678901234567890123456789"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(server._load_entry_balance_proof(path).baseline_balance, server.Decimal(payload["baselineBalance"]))
            payload["dealTypeBuy"] = 999
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid values"):
                server._load_entry_balance_proof(path)
    def test_config_freezes_validated_proof_until_restart(self) -> None:
        payload = {
            "formatVersion": 1, "evidenceSha256": server.APPROVED_EVIDENCE_SHA256,
            "statementReportSha256": server.APPROVED_REPORT_SHA256,
            "server": "Broker-Server", "accountLogin": 1, "currency": "USD",
            "baselineBalance": "100", "baselineDealTicket": 1,
            "approvalTimestamp": "2026-01-01T00:00:00+00:00",
            "ledgerSemanticsVersion": 1, "currencyScale": 2, "roundingMode": "ROUND_HALF_UP",
            "dealTypeBuy": 0, "dealTypeSell": 1, "dealTypeBalance": 2, "dealEntryIn": 0, "dealEntryInOut": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proof.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.dict(os.environ, {"MT5_ENTRY_BALANCE_PROOF_FILE": str(path)}):
                server._get_config.cache_clear()
                config = server._get_config()
                payload["evidenceSha256"] = "a" * 64
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIs(server._get_config(), config)
                self.assertEqual(config.entry_balance_proof.evidence_sha256, server.APPROVED_EVIDENCE_SHA256)
                server._get_config.cache_clear()

    def test_main_uses_config_owned_proof_without_reloading_path(self) -> None:
        config = mock.Mock(bridge_host="127.0.0.1", bridge_port=18813, mt5_initial_from=server.datetime(2020, 1, 1, tzinfo=server.UTC))
        http_server = mock.Mock()
        with (
            mock.patch.object(server, "_get_config", return_value=config),
            mock.patch.object(server, "_initialize_mt5") as initialize,
            mock.patch.object(server, "ThreadingHTTPServer", return_value=http_server),
            mock.patch.object(server, "_load_entry_balance_proof") as load_proof,
            mock.patch.object(server.mt5, "shutdown", create=True),
        ):
            http_server.serve_forever.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                server.main()
        initialize.assert_called_once_with(config)
        load_proof.assert_not_called()

    def test_live_ledger_after_approved_checkpoint_remains_proven(self) -> None:
        deals = [
            Deal(1, 0, 0, 1, 1000, 2, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0, "", "", ""),
            Deal(2, 2, 50, 2, 2000, 0, 0, 0, 0, 1.0, 110.0, -1.0, 0.0, 12.0, 0.0, "X", "", ""),
            Deal(3, 3, 50, 3, 3000, 1, 1, 0, 0, 1.0, 120.0, 0.0, 0.0, 8.0, 0.0, "X", "", ""),
        ]
        proven, unsupported = server._build_position_entry_assertions(
            deals, Account(1, 119.0, "USD"), "Broker-Server", 1, entry_balance_proof(),
        )
        self.assertEqual(unsupported, [])
        self.assertEqual(proven[0]["preEntryBalance"], "100")

    def test_position_assertions_use_decimal_balance_before_exact_in_entry(self) -> None:
        deals = [
            Deal(1, 0, 0, 1, 1000, 2, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0, "", "", ""),
            Deal(2, 2, 50, 2, 2000, 0, 0, 0, 0, 1.0, 110.0, -1.0, 0.0, 12.0, 0.0, "X", "", ""),
        ]
        proof = entry_balance_proof()
        proven, unsupported = server._build_position_entry_assertions(deals, Account(1, 111.0, "USD"), "Broker-Server", 1, proof)
        self.assertEqual(unsupported, [])
        self.assertEqual(proven, [{
            "positionId": "50", "entryDealTicket": "2", "entryOrderTicket": "2",
            "entryTimeMsc": 2000, "preEntryBalance": "100", "ledgerSemanticsVersion": 1,
        }])

    def test_exit_only_position_is_unanchored(self) -> None:
        deals = [Deal(2, 2, 50, 2, 2000, 1, 1, 0, 0, 1.0, 110.0, -1.0, 0.0, 12.0, 0.0, "X", "", "")]
        proven, unsupported = server._build_position_entry_assertions(deals, Account(1, 0, "USD"), "Broker-Server", 1, None)
        self.assertEqual(proven, [])
        self.assertEqual(unsupported, [{"kind": "UNANCHORED", "positionId": "50", "reason": "OPENING_DEAL_OUTSIDE_HISTORY", "ledgerSemanticsVersion": 1}])
    def test_non_execution_position_linked_deal_is_unanchored(self) -> None:
        deals = [Deal(9, 9, 50, 1, 1000, 2, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 10.0, 0.0, "", "", "")]
        proven, unsupported = server._build_position_entry_assertions(
            deals, Account(1, 110.0, "USD"), "Broker-Server", 1, entry_balance_proof(),
        )
        self.assertEqual(proven, [])
        self.assertEqual(unsupported, [{"kind": "UNANCHORED", "positionId": "50", "reason": "OPENING_DEAL_OUTSIDE_HISTORY", "ledgerSemanticsVersion": 1}])

    def test_account_mismatch_is_anchored_unsupported(self) -> None:
        deals = [
            Deal(1, 0, 0, 1, 1000, 2, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0, "", "", ""),
            Deal(2, 2, 50, 2, 2000, 0, 0, 0, 0, 1.0, 110.0, 0.0, 0.0, 0.0, 0.0, "X", "", ""),
        ]
        proven, unsupported = server._build_position_entry_assertions(
            deals, Account(1, 100.0, "USD"), "Other-Server", 1, entry_balance_proof(),
        )
        self.assertEqual(proven, [])
        self.assertEqual(unsupported[0]["reason"], "UNSUPPORTED_ACCOUNT_NOT_APPROVED")

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

    def test_history_to_datetime_requires_positive_epoch_milliseconds(self) -> None:
        self.assertEqual(
            server._history_to_datetime(1_765_411_845_123),
            server.datetime(2025, 12, 11, 0, 10, 45, 123000, tzinfo=server.UTC),
        )
        for invalid in (None, True, 0, -1, 1.5, "1765411845123"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "historyToMsc"):
                    server._history_to_datetime(invalid)
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
                    "historyToMsc": 1_765_411_845_123,
                }
                response: list[tuple[object, dict[str, object]]] = []
                handler._send_json = lambda status, body: response.append((status, body))

                with (
                    mock.patch.object(server, "_login_for_sync") as login,
                    mock.patch.object(
                        server,
                        "_get_config",
                        return_value=types.SimpleNamespace(
                            bridge_token="test-token",
                            entry_balance_proof=entry_balance_proof(server="Other", account_login=2, baseline_balance=server.Decimal("0")),
                            mt5_initial_from=server.datetime(2020, 1, 1, tzinfo=server.UTC),
                        ),
                    ),
                    mock.patch.object(server.mt5, "history_deals_get", return_value=deals, create=True),
                    mock.patch.object(server.mt5, "history_orders_get", return_value=orders, create=True),
                    mock.patch.object(server.mt5, "account_info", return_value=Account(1, 111.0, "USD"), create=True),
                ):
                    handler._handle_sync()
                    expected_history_to = server.datetime(2025, 12, 11, 0, 10, 45, 123000, tzinfo=server.UTC)
                    server.mt5.history_deals_get.assert_called_once_with(
                        server.datetime(2020, 1, 1, tzinfo=server.UTC), expected_history_to,
                    )
                    server.mt5.history_orders_get.assert_called_once_with(
                        server.datetime(2020, 1, 1, tzinfo=server.UTC), expected_history_to,
                    )

                login.assert_called_once_with("Broker-Server", 1, "password")
                status, body = response[0]
                self.assertEqual(status, server.HTTPStatus.OK)
                self.assertEqual(body["contractVersion"], 3)
                self.assertEqual(body["server"], "Broker-Server")
                self.assertEqual(body["accountLogin"], 1)
                self.assertEqual(
                    body["cursor"],
                    server._encode_cursor(deals_digest, orders_digest, "test-token"),
                )
                self.assertEqual(body["deals"], serialized_deals if deals_changed else [])
                self.assertEqual(body["orders"], serialized_orders if orders_changed else [])
                self.assertEqual(body["positionEntryBalances"], [])
                self.assertEqual(body["unsupportedPositionEntryBalances"], [{
                    "kind": "ANCHORED", "positionId": "50", "entryDealTicket": "1",
                    "entryOrderTicket": "1", "entryTimeMsc": 1000,
                    "reason": "UNSUPPORTED_ACCOUNT_NOT_APPROVED", "ledgerSemanticsVersion": 1,
                }])


if __name__ == "__main__":
    unittest.main()
