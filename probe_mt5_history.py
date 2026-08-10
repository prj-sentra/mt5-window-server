from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"Missing environment file: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise RuntimeError(f"Invalid .env line: {raw_line!r}")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fields(value: Any) -> list[str]:
    return list(getattr(value, "_fields", ()))


def decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def deal_delta(deal: Any) -> Decimal:
    return sum(
        (decimal(getattr(deal, field, 0)) for field in ("profit", "commission", "swap", "fee")),
        Decimal("0"),
    )


def summarize_deal(deal: Any) -> dict[str, Any]:
    return {
        "ticket": str(deal.ticket),
        "order": str(deal.order),
        "positionId": str(deal.position_id),
        "time": datetime.fromtimestamp(deal.time_msc / 1000, tz=UTC).isoformat(),
        "type": int(deal.type),
        "entry": int(deal.entry),
        "symbol": deal.symbol,
        "profit": str(decimal(deal.profit)),
        "commission": str(decimal(deal.commission)),
        "swap": str(decimal(deal.swap)),
        "fee": str(decimal(deal.fee)),
        "hasBalanceField": hasattr(deal, "balance"),
        "balanceField": str(getattr(deal, "balance", "<not provided>")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only probe for MetaTrader5 deal/order/position history capabilities."
    )
    parser.add_argument("--env", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--days", type=int, default=90, help="History window when MT5_INITIAL_FROM is absent")
    parser.add_argument("--position", type=int, help="Inspect one MT5 position id")
    args = parser.parse_args()

    load_dotenv(args.env)
    terminal = required("MT5_TERMINAL")
    login = int(required("MT5_LOGIN"))
    password = required("MT5_PASSWORD")
    server = required("MT5_SERVER")
    raw_start = os.getenv("MT5_INITIAL_FROM")
    start = datetime.fromisoformat(raw_start).astimezone(UTC) if raw_start else datetime.now(UTC) - timedelta(days=args.days)
    end = datetime.now(UTC)

    if not mt5.initialize(path=terminal):
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        if not mt5.login(login=login, password=password, server=server):
            raise RuntimeError(f"mt5.login failed: {mt5.last_error()}")

        account = mt5.account_info()
        if account is None:
            raise RuntimeError(f"mt5.account_info failed: {mt5.last_error()}")
        raw_deals = mt5.history_deals_get(start, end)
        if raw_deals is None:
            raise RuntimeError(f"mt5.history_deals_get failed: {mt5.last_error()}")
        raw_orders = mt5.history_orders_get(start, end)
        if raw_orders is None:
            raise RuntimeError(f"mt5.history_orders_get failed: {mt5.last_error()}")
        raw_positions = mt5.positions_get()
        if raw_positions is None:
            raise RuntimeError(f"mt5.positions_get failed: {mt5.last_error()}")
        deals = list(raw_deals)
        orders = list(raw_orders)
        positions = list(raw_positions)

        deals.sort(key=lambda item: (int(item.time_msc), int(item.ticket)))
        opening_deals = [
            deal for deal in deals
            if int(deal.position_id) > 0 and int(deal.entry) == int(mt5.DEAL_ENTRY_IN)
        ]
        balance_deals = [deal for deal in deals if int(deal.type) == int(mt5.DEAL_TYPE_BALANCE)]

        result: dict[str, Any] = {
            "readOnly": True,
            "window": {"from": start.isoformat(), "to": end.isoformat()},
            "account": {
                "login": str(account.login),
                "server": account.server,
                "currency": account.currency,
                "currentBalance": str(decimal(account.balance)),
            },
            "counts": {
                "deals": len(deals),
                "orders": len(orders),
                "openPositions": len(positions),
                "openingDeals": len(opening_deals),
                "balanceDeals": len(balance_deals),
            },
            "availableFields": {
                "deal": fields(deals[0]) if deals else [],
                "order": fields(orders[0]) if orders else [],
                "position": fields(positions[0]) if positions else [],
            },
            "constants": {
                "DEAL_ENTRY_IN": int(mt5.DEAL_ENTRY_IN),
                "DEAL_ENTRY_OUT": int(mt5.DEAL_ENTRY_OUT),
                "DEAL_ENTRY_INOUT": int(mt5.DEAL_ENTRY_INOUT),
                "DEAL_TYPE_BUY": int(mt5.DEAL_TYPE_BUY),
                "DEAL_TYPE_SELL": int(mt5.DEAL_TYPE_SELL),
                "DEAL_TYPE_BALANCE": int(mt5.DEAL_TYPE_BALANCE),
                "DEAL_TYPE_CREDIT": int(mt5.DEAL_TYPE_CREDIT),
            },
            "sampleOpeningDeal": summarize_deal(opening_deals[0]) if opening_deals else None,
            "sampleBalanceDeal": summarize_deal(balance_deals[0]) if balance_deals else None,
        }

        if args.position:
            position_deals = list(mt5.history_deals_get(position=args.position) or ())
            position_orders = list(mt5.history_orders_get(position=args.position) or ())
            result["selectedPosition"] = {
                "positionId": str(args.position),
                "dealCount": len(position_deals),
                "orderCount": len(position_orders),
                "deals": [summarize_deal(deal) for deal in position_deals],
            }

        if balance_deals:
            first_balance = balance_deals[0]
            running = deal_delta(first_balance)
            reconstructed: list[dict[str, str]] = []
            for deal in deals[deals.index(first_balance) + 1:]:
                before = running
                running += deal_delta(deal)
                if int(deal.position_id) > 0 and int(deal.entry) == int(mt5.DEAL_ENTRY_IN):
                    reconstructed.append({
                        "positionId": str(deal.position_id),
                        "entryDealTicket": str(deal.ticket),
                        "entryTime": datetime.fromtimestamp(deal.time_msc / 1000, tz=UTC).isoformat(),
                        "balanceBeforeEntry": str(before),
                        "balanceAfterEntryDeal": str(running),
                    })
            result["reconstructionProbe"] = {
                "startsAtBalanceDeal": str(first_balance.ticket),
                "calculatedFinalBalance": str(running),
                "currentBalance": str(decimal(account.balance)),
                "matchesCurrentBalance": running == decimal(account.balance),
                "sampleEntries": reconstructed[:10],
            }

        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
