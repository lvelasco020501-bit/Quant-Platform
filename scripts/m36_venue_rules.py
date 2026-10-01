"""Capture venue rules for the pool markets M16 never captured.

M36 phase 2 runs the certified engine, and the engine needs a market's venue rules: price tick,
quantity step, minimum quantity and minimum notional. Those decide whether an order is rejected,
and order rejection is one of the Risk V2 layers phase 2 claims to exercise. Substituting
permissive placeholders would make that claim false, so the real rules are fetched instead.

M16 captured rules for its six markets and they are **not** re-fetched here: that dataset is
validated and three milestones of results rest on it, so re-acquiring it would create a second
lineage for exactly the markets whose numbers must stay comparable. These rules are written to
M36's own directory and the loader prefers M16's where both exist.

Two honest limitations, both recorded in the output:

* The rules are today's, not the rules in force when each bar printed. Binance has changed tick
  sizes and minimum notionals over the years. This is the same limitation M16's own six carry,
  which is why the standard is consistent rather than better.
* A market whose status is not TRADING still publishes rules, and they are captured with the
  status attached. A halted market's current rules are a reasonable stand-in for its historical
  ones and a poor stand-in for nothing, but the reader is told which is which.

Usage:
    uv run python scripts/m36_venue_rules.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from quantplatform.research.m16 import ASSETS
from quantplatform.research.m32 import pool_symbols

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
OUT: Final[Path] = ROOT / "data/raw/m36/rules"
REPORT: Final[Path] = ROOT / "data/raw/m36/venue_rules_report.json"
EXCHANGE_INFO: Final[str] = "https://api.binance.com/api/v3/exchangeInfo"


def _fetch() -> dict[str, Any]:
    """Return Binance's spot exchange information."""
    request = urllib.request.Request(
        EXCHANGE_INFO, headers={"User-Agent": "quant-platform-m36/1.0"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        body: dict[str, Any] = json.loads(response.read())
    return body


def _filter(symbol: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """Return one of a symbol's filters by type."""
    for entry in symbol.get("filters", []):
        if entry.get("filterType") == kind:
            return entry
    return None


def _rules(symbol: dict[str, Any], raw: str) -> dict[str, Any]:
    """Return the rules for one market in the canonical shape M16 wrote."""
    price = _filter(symbol, "PRICE_FILTER") or {}
    lot = _filter(symbol, "LOT_SIZE") or {}
    notional = _filter(symbol, "NOTIONAL") or _filter(symbol, "MIN_NOTIONAL") or {}
    base = str(symbol["baseAsset"])
    quote = str(symbol["quoteAsset"])
    return {
        "symbol": f"{base}/{quote}",
        "base_asset": base,
        "quote_asset": quote,
        "market_type": "spot",
        "price_tick": price.get("tickSize"),
        "quantity_step": lot.get("stepSize"),
        "min_quantity": lot.get("minQty"),
        "max_quantity": lot.get("maxQty"),
        "min_notional": notional.get("minNotional"),
        "max_notional": notional.get("maxNotional"),
        "source": f"binance_spot:{raw}",
        # Recorded rather than filtered on: a halted market's current rules are a reasonable
        # stand-in for its historical ones and a poor stand-in for nothing.
        "venue_status": symbol.get("status"),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    """Write rules for every pool market M16 did not already capture."""
    already = {asset.raw for asset in ASSETS}
    wanted = [raw for raw in pool_symbols() if raw not in already]
    info = _fetch()
    listed = {entry["symbol"]: entry for entry in info["symbols"]}

    OUT.mkdir(parents=True, exist_ok=True)
    written: dict[str, Any] = {}
    missing: list[str] = []
    for raw in wanted:
        symbol = listed.get(raw)
        if symbol is None:
            missing.append(raw)
            sys.stdout.write(f"  {raw:11} NOT LISTED -- no venue rules exist\n")
            continue
        rules = _rules(symbol, raw)
        incomplete = [
            key
            for key in ("price_tick", "quantity_step", "min_quantity", "min_notional")
            if rules[key] is None
        ]
        if incomplete:
            missing.append(raw)
            sys.stdout.write(f"  {raw:11} INCOMPLETE -- missing {incomplete}\n")
            continue
        (OUT / f"{raw}.json").write_text(
            json.dumps(rules, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        written[raw] = rules
        flag = "" if rules["venue_status"] == "TRADING" else f"  [{rules['venue_status']}]"
        sys.stdout.write(
            f"  {raw:11} tick {rules['price_tick']:>12} step {rules['quantity_step']:>12} "
            f"minNotional {rules['min_notional']:>8}{flag}\n"
        )
        sys.stdout.flush()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "milestone": "m36",
                "purpose": "venue rules the certified engine needs for the corrected universe",
                "source": EXCHANGE_INFO,
                "not_refetched": sorted(already),
                "captured": sorted(written),
                "unavailable": sorted(missing),
                "limitation": (
                    "these are today's rules, not the rules in force when each bar printed; "
                    "the same limitation M16's own six carry"
                ),
                "non_trading": sorted(
                    raw for raw, r in written.items() if r["venue_status"] != "TRADING"
                ),
                "generated_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(
        f"\n{len(written)} captured, {len(missing)} unavailable, "
        f"{len(already)} left untouched -> {REPORT.relative_to(ROOT)}\n"
    )
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
