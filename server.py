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
CURSOR_VERSION = 4


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


def _facts_digest(facts: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        facts,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_cursor(deals_digest: str, orders_digest: str, token: str) -> str:
    payload = json.dumps(
        {"v": CURSOR_VERSION, "d": deals_digest, "o": orders_digest},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(token.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_cursor(raw: Any, token: str) -> tuple[str, str]:
    if raw in {None, ""}:
        return "", ""
    if not isinstance(raw, str):
        raise ValueError("'cursor' must be an opaque string")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        signed = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload, supplied = signed[:-32], signed[-32:]
        expected = hmac.new(token.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError
        decoded = json.loads(payload.decode("utf-8"))
        version = decoded.get("v")
        if (
            version not in {2, 3, CURSOR_VERSION}
            or not isinstance(decoded.get("d"), str)
            or not isinstance(decoded.get("o"), str)
            or any(
                digest and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest))
                for digest in (decoded["d"], decoded["o"])
            )
        ):
            raise ValueError
        if version != CURSOR_VERSION:
            return "", ""
        return decoded["d"], decoded["o"]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid or expired cursor") from exc


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
    server_version = "Mt5Bridge/4.0"

    def do_GET(self) -> None:  # noqa: N802
        try:
            if not self._require_auth():
                return
            if self.path == "/health":
                self._handle_health()
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
        server = payload.get("server")
        account_login = payload.get("accountLogin")
        password = payload.get("password")
        if not isinstance(server, str) or not server.strip() or server != server.strip():
            raise ValueError("'server' must be a non-empty exact string")
        if not isinstance(account_login, int) or isinstance(account_login, bool) or account_login <= 0:
            raise ValueError("'accountLogin' must be a positive integer")
        if not isinstance(password, str) or not password:
            raise ValueError("'password' must be a non-empty string")

        previous_deals_digest, previous_orders_digest = _decode_cursor(
            payload.get("cursor"),
            config.bridge_token,
        )
        with MT5_LOCK:
            _login_for_sync(server, account_login, password)
            history_from = _history_datetime(payload.get("historyFromMsc"), "historyFromMsc", allow_zero=True)
            history_to = _history_datetime(payload.get("historyToMsc"), "historyToMsc")
            if history_to < history_from:
                raise ValueError("'historyToMsc' must not precede 'historyFromMsc'")
            all_deals = mt5.history_deals_get(history_from, history_to)
            all_orders = mt5.history_orders_get(history_from, history_to)
            account = mt5.account_info()
            if all_deals is None:
                raise RuntimeError(f"mt5.history_deals_get() failed: {mt5.last_error()}")
            if all_orders is None:
                raise RuntimeError(f"mt5.history_orders_get() failed: {mt5.last_error()}")
            if account is None:
                raise RuntimeError(f"mt5.account_info() failed: {mt5.last_error()}")

            deals = sorted(all_deals, key=_deal_key)
            orders = sorted(all_orders, key=_order_key)
            serialized_deals = [_serialize_deal(deal) for deal in deals]
            serialized_orders = [_serialize_order(order) for order in orders]
            deals_digest = _facts_digest(serialized_deals)
            orders_digest = _facts_digest(serialized_orders)
            changed_deals = [] if deals_digest == previous_deals_digest else serialized_deals
            changed_orders = [] if orders_digest == previous_orders_digest else serialized_orders

        response = {
            "contractVersion": 4,
            "server": server,
            "accountLogin": account_login,
            "cursor": _encode_cursor(deals_digest, orders_digest, config.bridge_token),
            "historyRange": {
                "fromMsc": payload["historyFromMsc"],
                "toMsc": payload["historyToMsc"],
            },
            "account": {
                "currency": str(account.currency or ""),
                "currentBalance": _canonical_number(account.balance),
                "currencyDigits": _currency_digits(account),
            },
            "deals": changed_deals,
            "orders": changed_orders,
        }
        if len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 1024 * 1024:
            raise RuntimeError("bridge v4 response exceeds 1 MiB")
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
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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


