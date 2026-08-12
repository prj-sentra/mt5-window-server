from __future__ import annotations

import base64
import hashlib
import hmac
import functools
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5



def _strip_optional_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_dotenv(path: Path | None = None) -> None:
    if path is None:
        cwd_env = Path.cwd() / ".env"
        script_env = Path(__file__).resolve().with_name(".env")
        candidates = [cwd_env]
        if script_env != cwd_env:
            candidates.append(script_env)
    else:
        candidates = [path]

    for candidate in candidates:
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                raise RuntimeError(f"invalid .env line in {candidate}: {raw_line!r}")
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), _strip_optional_quotes(value.strip()))
        return


_load_dotenv()

logging.basicConfig(
    level=os.getenv("MT5_BRIDGE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("mt5-bridge-server")

MT5_LOCK = threading.RLock()
CURSOR_VERSION = 5
SYNC_RESPONSE_TARGET_BYTES = 768 * 1024
SYNC_RESPONSE_MAX_BYTES = 1024 * 1024
TICK_RESPONSE_MAX_BYTES = 900_000
TICK_MAX_CHUNK_MSC = 300_000
TICK_MAX_PAGE_SIZE = 1_000
TICK_MAX_SNAPSHOT_TICKS = 20_000
TICK_SNAPSHOT_TTL_SECONDS = 60
TICK_CACHE_MAX_ENTRIES = 8
TICK_CACHE_MAX_BYTES = 6_000_000
TICK_CURSOR_NAMESPACE = "ticks-v1"
TICK_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
TICK_CACHE_BYTES = 0
TICK_CACHE_LOCK = threading.RLock()
SUPPORTED_CALCULATION_MODES = (
    ("SYMBOL_CALC_MODE_FOREX", 0, "FOREX"),
    ("SYMBOL_CALC_MODE_FUTURES", 2, "FUTURES"),
    ("SYMBOL_CALC_MODE_CFD", 1, "CFD"),
    ("SYMBOL_CALC_MODE_CFDINDEX", 3, "CFDINDEX"),
    ("SYMBOL_CALC_MODE_CFDLEVERAGE", 4, "CFDLEVERAGE"),
    ("SYMBOL_CALC_MODE_EXCH_STOCKS", 32, "EXCH_STOCKS"),
    ("SYMBOL_CALC_MODE_EXCH_FUTURES", 33, "EXCH_FUTURES"),
    ("SYMBOL_CALC_MODE_EXCH_FUTURES_FORTS", 34, "EXCH_FUTURES_FORTS"),
    ("SYMBOL_CALC_MODE_EXCH_BONDS", 35, "EXCH_BONDS"),
    ("SYMBOL_CALC_MODE_EXCH_STOCKS_MOEX", 36, "EXCH_STOCKS_MOEX"),
    ("SYMBOL_CALC_MODE_EXCH_BONDS_MOEX", 37, "EXCH_BONDS_MOEX"),
    ("SYMBOL_CALC_MODE_SERV_COLLATERAL", 64, "SERV_COLLATERAL"),
    ("SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE", 10, "FOREX_NO_LEVERAGE"),
)
SUPPORTED_CALCULATION_MODE_NAMES = tuple(mode for _, _, mode in SUPPORTED_CALCULATION_MODES)


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    mt5_terminal: str
    mt5_initial_from: datetime
    bridge_host: str
    bridge_port: int
    bridge_token: str



def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "_asdict"):
        return {key: _serialize(item) for key, item in value._asdict().items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


def _canonical_number(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError("MT5 account balance is invalid") from exc
    if not number.is_finite():
        raise RuntimeError("MT5 account balance is invalid")
    if number.is_zero():
        return "0"
    return format(number.normalize(), "f")


def _canonical_positive_number(value: Any, field: str) -> str:
    number = _canonical_number(value)
    if Decimal(number) <= 0:
        raise ValueError(f"'{field}' must be positive and finite")
    return number


def _tick_field(tick: Any, field: str) -> Any:
    try:
        return tick[field]
    except (IndexError, KeyError, TypeError):
        return getattr(tick, field)


def _tick_snapshot_digest(ticks: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"\x00\x00\x00\x11ticks-v1-snapshot")
    digest.update(len(ticks).to_bytes(8, "big"))
    for tick in ticks:
        digest.update(tick["sequence"].to_bytes(8, "big"))
        digest.update(tick["timeMsc"].to_bytes(8, "big"))
        for field in ("bid", "ask"):
            encoded = tick[field].encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _digest_parts(parts: list[str | int]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, int):
            digest.update(part.to_bytes(8, "big"))
        else:
            encoded = part.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _symbol_valuation(symbol: str, account: Any) -> dict[str, Any]:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError("tick_valuation_unsupported")
    mode_by_value = {
        getattr(mt5, constant, default): mode
        for constant, default, mode in SUPPORTED_CALCULATION_MODES
    }
    calculation_mode = mode_by_value.get(getattr(info, "trade_calc_mode", None))
    if calculation_mode is None:
        raise RuntimeError("tick_valuation_unsupported")
    account_currency = str(getattr(account, "currency", "") or "")
    profit_currency = str(getattr(info, "currency_profit", "") or "")
    if not account_currency or not profit_currency:
        raise RuntimeError("tick_valuation_unsupported")
    tick_size = _canonical_positive_number(getattr(info, "trade_tick_size", None), "tickSize")
    tick_value_profit = _canonical_positive_number(getattr(info, "trade_tick_value_profit", None), "tickValueProfit")
    tick_value_loss = _canonical_positive_number(getattr(info, "trade_tick_value_loss", None), "tickValueLoss")
    point = Decimal(tick_size)
    current_tick = mt5.symbol_info_tick(symbol)
    reference_value = getattr(current_tick, "last", 0) or getattr(current_tick, "bid", 0) or getattr(current_tick, "ask", 0)
    reference = Decimal(str(reference_value))
    if reference <= 0:
        raise RuntimeError("tick_valuation_unsupported")
    for order_type, direction in (
        (getattr(mt5, "ORDER_TYPE_BUY", 0), Decimal(1)),
        (getattr(mt5, "ORDER_TYPE_SELL", 1), Decimal(-1)),
    ):
        for signed_step in (Decimal(1), Decimal(-1)):
            close = reference + direction * signed_step * point
            observed = mt5.order_calc_profit(order_type, symbol, 1.0, float(reference), float(close))
            if observed is None:
                raise RuntimeError("tick_valuation_unsupported")
            expected_tick_value = Decimal(tick_value_profit if signed_step > 0 else tick_value_loss)
            expected = signed_step * expected_tick_value
            tolerance = max(Decimal("0.00000001"), abs(expected) * Decimal("0.000001"))
            if abs(Decimal(str(observed)) - expected) > tolerance:
                raise RuntimeError("tick_valuation_unsupported")
    values = {
        "version": 1, "calculationMode": calculation_mode,
        "accountCurrency": account_currency, "profitCurrency": profit_currency,
        "tickSize": tick_size, "tickValueProfit": tick_value_profit, "tickValueLoss": tick_value_loss,
    }
    values["sha256"] = _digest_parts([
        "ticks-v1-valuation", 1, calculation_mode, account_currency, profit_currency,
        tick_size, tick_value_profit, tick_value_loss,
    ])
    return values


def _encode_tick_cursor(snapshot: dict[str, Any], next_sequence: int, token: str) -> str:
    payload = json.dumps({
        "kind": TICK_CURSOR_NAMESPACE, "v": CURSOR_VERSION,
        "snapshotId": snapshot["id"], "snapshotSha256": snapshot["sha256"],
        "server": snapshot["server"], "accountLogin": snapshot["accountLogin"],
        "symbol": snapshot["symbol"], "rawRange": snapshot["rawRange"],
        "snapshotToMsc": snapshot["snapshotToMsc"], "pageSize": snapshot["pageSize"],
        "expiresAtMsc": snapshot["expiresAtMsc"], "nextSequence": next_sequence,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(token.encode("utf-8"), payload, "sha256").digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_tick_cursor(raw: Any, token: str) -> tuple[dict[str, Any], int]:
    if not isinstance(raw, str) or not raw or len(raw) > 2_048:
        raise ValueError("invalid_or_expired_tick_cursor")
    try:
        signed = base64.urlsafe_b64decode((raw + "=" * (-len(raw) % 4)).encode("ascii"))
        payload, supplied = signed[:-32], signed[-32:]
        if not hmac.compare_digest(supplied, hmac.new(token.encode("utf-8"), payload, "sha256").digest()):
            raise ValueError
        decoded = json.loads(payload)
        if decoded.get("kind") != TICK_CURSOR_NAMESPACE or decoded.get("v") != CURSOR_VERSION:
            raise ValueError
        with TICK_CACHE_LOCK:
            snapshot = TICK_CACHE.get(decoded["snapshotId"])
            if (
                snapshot is None or snapshot["expiresAtMsc"] <= int(time.time() * 1_000)
                or decoded["snapshotSha256"] != snapshot["sha256"]
                or any(decoded[key] != snapshot[key] for key in (
                    "server", "accountLogin", "symbol", "rawRange", "snapshotToMsc", "pageSize", "expiresAtMsc"
                ))
                or not isinstance(decoded["nextSequence"], int)
                or decoded["nextSequence"] < 0
            ):
                raise ValueError
            TICK_CACHE.move_to_end(snapshot["id"])
        return snapshot, decoded["nextSequence"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_or_expired_tick_cursor") from exc


def _expire_tick_cache() -> None:
    global TICK_CACHE_BYTES
    now = int(time.time() * 1_000)
    for key in [key for key, value in TICK_CACHE.items() if value["expiresAtMsc"] <= now]:
        TICK_CACHE_BYTES -= TICK_CACHE.pop(key)["bytes"]


def _admit_tick_snapshot(snapshot: dict[str, Any]) -> None:
    global TICK_CACHE_BYTES
    with TICK_CACHE_LOCK:
        _expire_tick_cache()
        while TICK_CACHE and (
            len(TICK_CACHE) >= TICK_CACHE_MAX_ENTRIES
            or TICK_CACHE_BYTES + snapshot["bytes"] > TICK_CACHE_MAX_BYTES
        ):
            _, evicted = TICK_CACHE.popitem(last=False)
            TICK_CACHE_BYTES -= evicted["bytes"]
        if snapshot["bytes"] > TICK_CACHE_MAX_BYTES:
            raise RuntimeError("tick_snapshot_capacity")
        TICK_CACHE[snapshot["id"]] = snapshot
        TICK_CACHE_BYTES += snapshot["bytes"]

def _currency_digits(account: Any) -> int:
    value = getattr(account, "currency_digits", None)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 8:
        raise RuntimeError("MT5 account currency digits are invalid")
    return value

def _parse_iso8601(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _history_datetime(raw: Any, field: str, *, allow_zero: bool = False) -> datetime:
    minimum = 0 if allow_zero else 1
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"'{field}' must be a {qualifier} integer")
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=raw)
    except OverflowError as exc:
        raise ValueError(f"'{field}' is outside the supported range") from exc


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"missing required environment variable: {name}")
    return value




@functools.lru_cache(maxsize=1)
def _get_config() -> BridgeConfig:
    return BridgeConfig(
        mt5_terminal=_require_env("MT5_TERMINAL"),
        mt5_initial_from=_parse_iso8601(_require_env("MT5_INITIAL_FROM")),
        bridge_host=_require_env("BRIDGE_HOST"),
        bridge_port=int(_require_env("BRIDGE_PORT")),
        bridge_token=_require_env("BRIDGE_TOKEN"),
    )


def _initialize_mt5(config: BridgeConfig | None = None) -> None:
    current = config or _get_config()
    if mt5.initialize(path=current.mt5_terminal):
        return
    raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")


def _as_int_string(value: Any) -> str:
    number = int(value or 0)
    if number < 0:
        raise ValueError("MT5 identifiers must be non-negative")
    return str(number)


def _deal_key(deal: Any) -> tuple[int, int]:
    return int(deal.time_msc), int(deal.ticket)


def _order_key(order: Any) -> tuple[int, int]:
    return max(int(order.time_done_msc), int(order.time_setup_msc)), int(order.ticket)


def _encode_cursor(
    *,
    token: str,
    mode: str,
    server: str,
    account_login: int,
    snapshot_to_msc: int,
    deal_key: tuple[int, int] | None,
    order_key: tuple[int, int] | None,
    changed_since_msc: int | None,
    open_position_ids: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "v": CURSOR_VERSION,
            "m": mode,
            "s": server,
            "a": account_login,
            "t": snapshot_to_msc,
            "d": deal_key,
            "o": order_key,
            "c": changed_since_msc,
            "p": open_position_ids,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(token.encode("utf-8"), payload, "sha256").digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_cursor(
    raw: Any,
    *,
    token: str,
    mode: str,
    server: str,
    account_login: int,
    snapshot_to_msc: int,
    changed_since_msc: int | None,
    open_position_ids: tuple[str, ...],
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    if raw in {None, ""}:
        return None, None
    if not isinstance(raw, str):
        raise ValueError("'pageCursor' must be an opaque string")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        signed = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload, supplied = signed[:-32], signed[-32:]
        expected = hmac.new(token.encode("utf-8"), payload, "sha256").digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError
        decoded = json.loads(payload.decode("utf-8"))
        if (
            decoded.get("v") != CURSOR_VERSION
            or decoded.get("m") != mode
            or decoded.get("s") != server
            or decoded.get("a") != account_login
            or decoded.get("t") != snapshot_to_msc
            or decoded.get("c") != changed_since_msc
            or decoded.get("p") != list(open_position_ids)
        ):
            raise ValueError
        keys: list[tuple[int, int] | None] = []
        for key in (decoded.get("d"), decoded.get("o")):
            if key is None:
                keys.append(None)
                continue
            if (
                not isinstance(key, list)
                or len(key) != 2
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in key)
            ):
                raise ValueError
            keys.append((key[0], key[1]))
        return keys[0], keys[1]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid or expired cursor") from exc


def _positive_id_strings(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError("'openPositionIds' must be an array of positive decimal strings")
    values: list[str] = []
    for value in raw:
        if (
            not isinstance(value, str)
            or not value
            or not value.isdecimal()
            or value.startswith("0")
        ):
            raise ValueError("'openPositionIds' must contain unique positive decimal strings")
        values.append(value)
    if len(values) != len(set(values)):
        raise ValueError("'openPositionIds' must contain unique positive decimal strings")
    return tuple(sorted(values, key=int))


def _dedupe_sorted(facts: list[Any], key: Any) -> list[Any]:
    result: list[Any] = []
    tickets: set[int] = set()
    for fact in sorted(facts, key=key):
        ticket = int(fact.ticket)
        if ticket not in tickets:
            tickets.add(ticket)
            result.append(fact)
    return result


def _compact_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _serialize_deal(deal: Any) -> dict[str, Any]:
    return {
        "ticket": _as_int_string(deal.ticket),
        "order": _as_int_string(deal.order),
        "positionId": _as_int_string(deal.position_id),
        "time": int(deal.time),
        "timeMsc": int(deal.time_msc),
        "type": int(deal.type),
        "entry": int(deal.entry),
        "magic": _as_int_string(deal.magic),
        "reason": int(deal.reason),
        "volume": float(deal.volume),
        "price": float(deal.price),
        "commission": float(deal.commission),
        "swap": float(deal.swap),
        "profit": float(deal.profit),
        "fee": float(deal.fee),
        "symbol": str(deal.symbol or ""),
        "comment": str(deal.comment or ""),
        "externalId": str(deal.external_id or ""),
    }


def _serialize_order(order: Any) -> dict[str, Any]:
    return {
        "ticket": _as_int_string(order.ticket),
        "positionId": _as_int_string(order.position_id),
        "timeSetup": int(order.time_setup),
        "timeSetupMsc": int(order.time_setup_msc),
        "timeDone": int(order.time_done),
        "timeDoneMsc": int(order.time_done_msc),
        "type": int(order.type),
        "state": int(order.state),
        "reason": int(order.reason),
        "volumeInitial": float(order.volume_initial),
        "volumeCurrent": float(order.volume_current),
        "priceOpen": float(order.price_open),
        "sl": float(order.sl),
        "tp": float(order.tp),
        "priceCurrent": float(order.price_current),
        "priceStopLimit": float(order.price_stoplimit),
        "symbol": str(order.symbol or ""),
        "comment": str(order.comment or ""),
        "externalId": str(order.external_id or ""),
    }




def _login_for_sync(server: str, account_login: int, password: str) -> None:
    config = _get_config()
    if not mt5.initialize(path=config.mt5_terminal):
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
    if not mt5.login(login=account_login, password=password, server=server):
        raise RuntimeError(f"mt5.login() failed: {mt5.last_error()}")
    account = mt5.account_info()
    if account is None or int(account.login) != account_login:
        raise RuntimeError("MT5 logged into an unexpected account")


def _ensure_connected() -> None:
    terminal_info = mt5.terminal_info()
    if terminal_info is not None:
        return
    _initialize_mt5()


class Mt5BridgeHandler(BaseHTTPRequestHandler):
    server_version = "Mt5Bridge/5.0"

    def do_GET(self) -> None:  # noqa: N802
        try:
            if not self._require_auth():
                return
            if self.path == "/health":
                self._handle_health()
                return
            if self.path == "/capabilities":
                self._handle_capabilities()
                return
            if self.path == "/api/account":
                self._handle_account()
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except Exception as exc:  # pragma: no cover - exercised by manual runtime
            LOGGER.exception("GET %s failed", self.path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self._require_auth():
                return
            if self.path == "/sync":
                self._handle_sync()
                return
            if self.path == "/ticks":
                self._handle_ticks()
                return
            if self.path == "/api/history/deals":
                self._handle_history_deals()
                return
            if self.path == "/api/history/orders":
                self._handle_history_orders()
                return
            if self.path == "/api/profit/calc":
                self._handle_profit_calc()
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            LOGGER.warning("POST %s rejected: %s", self.path, exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except RuntimeError as exc:
            error = str(exc)
            status = HTTPStatus.UNPROCESSABLE_ENTITY if error in {"tick_source_limit", "tick_valuation_unsupported"} else HTTPStatus.SERVICE_UNAVAILABLE if error == "tick_snapshot_capacity" else HTTPStatus.INTERNAL_SERVER_ERROR
            LOGGER.warning("POST %s failed: %s", self.path, error)
            self._send_json(status, {"error": error})
        except Exception as exc:  # pragma: no cover - exercised by manual runtime
            LOGGER.exception("POST %s failed", self.path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _require_auth(self) -> bool:
        expected = _get_config().bridge_token
        actual = self.headers.get("Authorization")
        if actual == f"Bearer {expected}":
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _handle_health(self) -> None:
        _ensure_connected()
        config = _get_config()
        terminal_info = mt5.terminal_info()
        account_info = mt5.account_info()
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "server_time_utc": datetime.now(UTC).isoformat(),
                "initial_from": config.mt5_initial_from.isoformat(),
                "terminal": _serialize(terminal_info),
                "account": _serialize(account_info),
            },
        )

    def _handle_account(self) -> None:
        _ensure_connected()
        account_info = mt5.account_info()
        if account_info is None:
            raise RuntimeError(f"mt5.account_info() failed: {mt5.last_error()}")
        self._send_json(HTTPStatus.OK, _serialize(account_info))

    def _handle_capabilities(self) -> None:
        self._send_json(HTTPStatus.OK, {
            "contractVersion": CURSOR_VERSION,
            "sync": {"bootstrap": True, "incremental": True, "fixedSnapshot": True},
            "ticks": {
                "available": True, "cursorNamespace": TICK_CURSOR_NAMESPACE,
                "maxRequestBytes": 8_192, "maxCursorChars": 2_048,
                "pageSize": {"min": 1, "max": TICK_MAX_PAGE_SIZE},
                "maxResponseBytes": TICK_RESPONSE_MAX_BYTES,
                "maxChunkSpanMsc": TICK_MAX_CHUNK_MSC,
                "maxChunkTicks": TICK_MAX_SNAPSHOT_TICKS,
                "maxSnapshotBytes": 750_000,
                "snapshotTtlSeconds": TICK_SNAPSHOT_TTL_SECONDS,
                "cacheMaxEntries": TICK_CACHE_MAX_ENTRIES,
                "cacheMaxBytes": TICK_CACHE_MAX_BYTES,
                "valuationVersion": 1,
                "supportedCalculationModes": list(SUPPORTED_CALCULATION_MODE_NAMES),
            },
        })

    def _handle_ticks(self) -> None:
        payload = self._read_json_body()
        config = _get_config()
        if payload.get("contractVersion") != CURSOR_VERSION:
            raise ValueError("'contractVersion' must be 5")
        cursor = payload.get("pageCursor")
        if cursor:
            snapshot, offset = _decode_tick_cursor(cursor, config.bridge_token)
        else:
            server = payload.get("server")
            account_login = payload.get("accountLogin")
            password = payload.get("password")
            symbol = payload.get("symbol")
            raw_range = payload.get("rawRange")
            snapshot_to_msc = payload.get("snapshotToMsc")
            page_size = payload.get("pageSize", TICK_MAX_PAGE_SIZE)
            if not isinstance(server, str) or not server.strip() or server != server.strip():
                raise ValueError("'server' must be a non-empty exact string")
            if not isinstance(account_login, int) or isinstance(account_login, bool) or account_login <= 0:
                raise ValueError("'accountLogin' must be a positive integer")
            if not isinstance(password, str) or not password:
                raise ValueError("'password' must be a non-empty string")
            if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip():
                raise ValueError("'symbol' must be a non-empty exact string")
            if not isinstance(raw_range, dict):
                raise ValueError("'rawRange' must be an object")
            from_msc, to_msc = raw_range.get("fromMsc"), raw_range.get("toMsc")
            _history_datetime(from_msc, "rawRange.fromMsc", allow_zero=True)
            raw_to = _history_datetime(to_msc, "rawRange.toMsc", allow_zero=True)
            _history_datetime(snapshot_to_msc, "snapshotToMsc")
            if from_msc > to_msc or to_msc > snapshot_to_msc or to_msc - from_msc > TICK_MAX_CHUNK_MSC:
                raise ValueError("'rawRange' is outside the supported snapshot")
            if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= TICK_MAX_PAGE_SIZE:
                raise ValueError("'pageSize' is outside the supported range")
            with MT5_LOCK:
                _login_for_sync(server, account_login, password)
                source = mt5.copy_ticks_range(
                    symbol, _history_datetime(from_msc, "rawRange.fromMsc", allow_zero=True),
                    raw_to, mt5.COPY_TICKS_ALL,
                )
                if source is None:
                    raise RuntimeError(f"mt5.copy_ticks_range() failed: {mt5.last_error()}")
                account = mt5.account_info()
                if account is None:
                    raise RuntimeError(f"mt5.account_info() failed: {mt5.last_error()}")
                valuation = _symbol_valuation(symbol, account)
                rows = []
                for source_index, tick in enumerate(source):
                    # MetaTrader5 returns a NumPy structured array. Individual
                    # rows are numpy.void values and require field indexing.
                    time_msc = int(_tick_field(tick, "time_msc"))
                    if from_msc <= time_msc <= to_msc:
                        try:
                            bid = _canonical_positive_number(_tick_field(tick, "bid"), "bid")
                            ask = _canonical_positive_number(_tick_field(tick, "ask"), "ask")
                        except ValueError:
                            continue
                        rows.append((time_msc, source_index, bid, ask))
            rows.sort(key=lambda row: (row[0], row[1]))
            if len(rows) > TICK_MAX_SNAPSHOT_TICKS:
                raise RuntimeError("tick_source_limit")
            ticks = [
                {"sequence": sequence, "timeMsc": row[0], "bid": row[2], "ask": row[3]}
                for sequence, row in enumerate(rows)
            ]
            snapshot = {
                "id": uuid.uuid4().hex, "server": server, "accountLogin": account_login,
                "symbol": symbol, "rawRange": {"fromMsc": from_msc, "toMsc": to_msc},
                "snapshotToMsc": snapshot_to_msc, "pageSize": page_size, "ticks": ticks,
                "valuation": valuation,
                "sha256": _tick_snapshot_digest(ticks),
                "expiresAtMsc": int(time.time() * 1_000) + TICK_SNAPSHOT_TTL_SECONDS * 1_000,
            }
            snapshot["bytes"] = len(_compact_json(ticks))
            if snapshot["bytes"] > 750_000:
                raise RuntimeError("tick_source_limit")
            _admit_tick_snapshot(snapshot)
            offset = 0
        page_ticks = snapshot["ticks"][offset:offset + snapshot["pageSize"]]
        next_offset = offset + len(page_ticks)
        complete = next_offset == len(snapshot["ticks"])
        response = {
            "contractVersion": CURSOR_VERSION, "cursorNamespace": TICK_CURSOR_NAMESPACE,
            "server": snapshot["server"], "accountLogin": snapshot["accountLogin"],
            "symbol": snapshot["symbol"], "rawRange": snapshot["rawRange"],
            "snapshotToMsc": snapshot["snapshotToMsc"],
            "pageSize": snapshot["pageSize"],
            "snapshot": {
                "id": snapshot["id"], "sha256": snapshot["sha256"],
                "tickCount": len(snapshot["ticks"]), "expiresAtMsc": snapshot["expiresAtMsc"],
            },
            "valuation": snapshot["valuation"],
            "ticks": page_ticks,
            "complete": complete,
            "bytes": 0,
        }
        if not complete:
            response["nextCursor"] = _encode_tick_cursor(snapshot, next_offset, config.bridge_token)
        while True:
            size = len(_compact_json(response))
            if response["bytes"] == size:
                break
            response["bytes"] = size
        if size > TICK_RESPONSE_MAX_BYTES:
            raise RuntimeError("tick response exceeds configured limit")
        self._send_json(HTTPStatus.OK, response)

    def _handle_history_deals(self) -> None:
        _ensure_connected()
        config = _get_config()
        payload = self._read_json_body()
        date_from = _parse_iso8601(payload["from"]) if payload.get("from") else config.mt5_initial_from
        date_to = _parse_iso8601(payload["to"]) if payload.get("to") else datetime.now(UTC)
        if date_to < date_from:
            raise ValueError("'to' must be greater than or equal to 'from'")
        group = payload.get("group")

        if group:
            deals = mt5.history_deals_get(date_from, date_to, group=group)
        else:
            deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            raise RuntimeError(f"mt5.history_deals_get() failed: {mt5.last_error()}")

        self._send_json(
            HTTPStatus.OK,
            {
                "count": len(deals),
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
                "group": group,
                "deals": _serialize(deals),
            },
        )

    def _handle_history_orders(self) -> None:
        _ensure_connected()
        config = _get_config()
        payload = self._read_json_body()
        date_from = _parse_iso8601(payload["from"]) if payload.get("from") else config.mt5_initial_from
        date_to = _parse_iso8601(payload["to"]) if payload.get("to") else datetime.now(UTC)
        if date_to < date_from:
            raise ValueError("'to' must be greater than or equal to 'from'")
        group = payload.get("group")

        if group:
            orders = mt5.history_orders_get(date_from, date_to, group=group)
        else:
            orders = mt5.history_orders_get(date_from, date_to)
        if orders is None:
            raise RuntimeError(f"mt5.history_orders_get() failed: {mt5.last_error()}")

        self._send_json(
            HTTPStatus.OK,
            {
                "count": len(orders),
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
                "group": group,
                "orders": _serialize(orders),
            },
        )

    def _handle_sync(self) -> None:
        config = _get_config()
        payload = self._read_json_body()
        if payload.get("contractVersion") != CURSOR_VERSION:
            raise ValueError("'contractVersion' must be 5")
        server = payload.get("server")
        account_login = payload.get("accountLogin")
        password = payload.get("password")
        if not isinstance(server, str) or not server.strip() or server != server.strip():
            raise ValueError("'server' must be a non-empty exact string")
        if not isinstance(account_login, int) or isinstance(account_login, bool) or account_login <= 0:
            raise ValueError("'accountLogin' must be a positive integer")
        if not isinstance(password, str) or not password:
            raise ValueError("'password' must be a non-empty string")
        mode = payload.get("mode")
        if mode not in {"bootstrap", "incremental"}:
            raise ValueError("'mode' must be 'bootstrap' or 'incremental'")
        snapshot_to_msc = payload.get("snapshotToMsc")
        snapshot_to = _history_datetime(snapshot_to_msc, "snapshotToMsc")
        changed_since_msc: int | None = None
        open_position_ids: tuple[str, ...] = ()
        if mode == "bootstrap":
            if "changedSinceMsc" in payload or "openPositionIds" in payload:
                raise ValueError("bootstrap requests must not include incremental-only fields")
        else:
            changed_since_msc = payload.get("changedSinceMsc")
            _history_datetime(changed_since_msc, "changedSinceMsc", allow_zero=True)
            if changed_since_msc > snapshot_to_msc:
                raise ValueError("'changedSinceMsc' must not exceed 'snapshotToMsc'")
            open_position_ids = _positive_id_strings(payload.get("openPositionIds"))
        previous_deal_key, previous_order_key = _decode_cursor(
            payload.get("pageCursor"),
            token=config.bridge_token,
            mode=mode,
            server=server,
            account_login=account_login,
            snapshot_to_msc=snapshot_to_msc,
            changed_since_msc=changed_since_msc,
            open_position_ids=open_position_ids,
        )
        with MT5_LOCK:
            _login_for_sync(server, account_login, password)
            history_from = (
                datetime(1970, 1, 1, tzinfo=UTC)
                if mode == "bootstrap"
                else _history_datetime(changed_since_msc, "changedSinceMsc", allow_zero=True)
            )
            all_deals_result = mt5.history_deals_get(history_from, snapshot_to)
            all_orders_result = mt5.history_orders_get(history_from, snapshot_to)
            if all_deals_result is None:
                raise RuntimeError(f"mt5.history_deals_get() failed: {mt5.last_error()}")
            if all_orders_result is None:
                raise RuntimeError(f"mt5.history_orders_get() failed: {mt5.last_error()}")
            all_deals = list(all_deals_result)
            all_orders = list(all_orders_result)
            for position_id in open_position_ids:
                position_deals = mt5.history_deals_get(position=int(position_id))
                position_orders = mt5.history_orders_get(position=int(position_id))
                if position_deals is None:
                    raise RuntimeError(f"mt5.history_deals_get(position) failed: {mt5.last_error()}")
                if position_orders is None:
                    raise RuntimeError(f"mt5.history_orders_get(position) failed: {mt5.last_error()}")
                all_deals.extend(deal for deal in position_deals if int(deal.time_msc) <= snapshot_to_msc)
                all_orders.extend(order for order in position_orders if _order_key(order)[0] <= snapshot_to_msc)
            account = mt5.account_info()
            if account is None:
                raise RuntimeError(f"mt5.account_info() failed: {mt5.last_error()}")

            deals = _dedupe_sorted(all_deals, _deal_key)
            orders = _dedupe_sorted(all_orders, _order_key)

        response_base = {
            "contractVersion": CURSOR_VERSION,
            "server": server,
            "accountLogin": account_login,
            "mode": mode,
            "snapshotToMsc": snapshot_to_msc,
            "account": {
                "currency": str(account.currency or ""),
                "currentBalance": _canonical_number(account.balance),
                "currencyDigits": _currency_digits(account),
            },
        }
        selected_deals: list[dict[str, Any]] = []
        selected_orders: list[dict[str, Any]] = []
        remaining: list[tuple[str, Any, tuple[int, int]]] = []
        remaining.extend(("deal", deal, _deal_key(deal)) for deal in deals if previous_deal_key is None or _deal_key(deal) > previous_deal_key)
        remaining.extend(("order", order, _order_key(order)) for order in orders if previous_order_key is None or _order_key(order) > previous_order_key)
        remaining.sort(key=lambda item: (item[2], item[0]))
        last_deal_key, last_order_key = previous_deal_key, previous_order_key

        def response_for_page(has_more: bool) -> dict[str, Any]:
            page: dict[str, Any] = {"hasMore": has_more, "bytes": 0}
            if has_more:
                page["nextCursor"] = _encode_cursor(
                    token=config.bridge_token, mode=mode, server=server, account_login=account_login,
                    snapshot_to_msc=snapshot_to_msc, deal_key=last_deal_key, order_key=last_order_key,
                    changed_since_msc=changed_since_msc, open_position_ids=open_position_ids,
                )
            response = {**response_base, "page": page, "deals": selected_deals, "orders": selected_orders}
            while True:
                size = len(_compact_json(response))
                if response["page"]["bytes"] == size:
                    return response
                response["page"]["bytes"] = size

        consumed = 0
        for stream, fact, key in remaining:
            target = selected_deals if stream == "deal" else selected_orders
            target.append(_serialize_deal(fact) if stream == "deal" else _serialize_order(fact))
            old_deal_key, old_order_key = last_deal_key, last_order_key
            if stream == "deal":
                last_deal_key = key
            else:
                last_order_key = key
            if len(_compact_json(response_for_page(consumed + 1 < len(remaining)))) > SYNC_RESPONSE_TARGET_BYTES:
                target.pop()
                last_deal_key, last_order_key = old_deal_key, old_order_key
                if consumed == 0:
                    target.append(_serialize_deal(fact) if stream == "deal" else _serialize_order(fact))
                    last_deal_key = key if stream == "deal" else last_deal_key
                    last_order_key = key if stream == "order" else last_order_key
                    consumed = 1
                break
            consumed += 1
        has_more = consumed < len(remaining)
        response = response_for_page(has_more)
        if len(_compact_json(response)) >= SYNC_RESPONSE_MAX_BYTES:
            raise RuntimeError("bridge v5 response exceeds 1 MiB")
        self._send_json(HTTPStatus.OK, response)
    def _handle_profit_calc(self) -> None:
        _ensure_connected()
        payload = self._read_json_body()
        symbol = str(payload.get("symbol") or "").strip()
        side = str(payload.get("side") or "").strip().lower()
        volume = float(payload.get("volume"))
        open_price = float(payload.get("open_price"))
        close_price = float(payload.get("close_price"))
        if not symbol:
            raise ValueError("'symbol' is required")
        if side not in {"long", "short"}:
            raise ValueError("'side' must be 'long' or 'short'")
        if volume <= 0 or open_price <= 0 or close_price <= 0:
            raise ValueError("'volume', 'open_price', and 'close_price' must be positive")

        order_type = getattr(mt5, "ORDER_TYPE_BUY", 0) if side == "long" else getattr(mt5, "ORDER_TYPE_SELL", 1)
        profit = mt5.order_calc_profit(order_type, symbol, volume, open_price, close_price)
        if profit is None:
            raise RuntimeError(f"mt5.order_calc_profit() failed: {mt5.last_error()}")

        self._send_json(
            HTTPStatus.OK,
            {
                "symbol": symbol,
                "side": side,
                "volume": volume,
                "open_price": open_price,
                "close_price": close_price,
                "profit": float(profit),
            },
        )

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = _compact_json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    config = _get_config()
    _initialize_mt5(config)
    server = ThreadingHTTPServer((config.bridge_host, config.bridge_port), Mt5BridgeHandler)
    LOGGER.info(
        "listening on %s:%s from=%s",
        config.bridge_host,
        config.bridge_port,
        config.mt5_initial_from.isoformat(),
    )
    try:
        server.serve_forever()
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()


