"""Read-only MetaTrader 5 history and TradingView helpers."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from statistics import fmean, median
import threading
import time
import re
import json
from urllib.parse import quote
import webbrowser

import requests

_MT5_LOCK = threading.RLock()
WATCH_SYMBOLS = ("XAUUSD", "BTCUSD", "GBPUSD")
PUBLIC_WATCH_SYMBOLS = frozenset(WATCH_SYMBOLS)
_PUBLIC_TICKERS = {"BTCUSD": "BTC-USD", "GBPUSD": "GBPUSD=X"}
_PUBLIC_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}
_PUBLIC_QUOTE_LOCK = threading.Lock()

def _load_mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError(
            "MetaTrader5 is not installed. Run: pip install MetaTrader5"
        ) from exc
    return mt5


def _deal_value(deal, name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(deal, name, default) or default)
    except (TypeError, ValueError):
        return default


def _monthly_history_unlocked(mt5, months: int = 24) -> dict:
    """Return closed-deal totals grouped by UTC calendar month.

    MT5 must be installed and its desktop terminal must be logged into the
    desired account. This function never places, edits, or closes an order.
    """
    months = max(1, min(int(months), 120))
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=months * 32)
    deals = mt5.history_deals_get(start, now)
    if deals is None:
        code, message = mt5.last_error()
        raise RuntimeError(f"MT5 history request failed ({code}): {message}")

    entry_out = getattr(mt5, "DEAL_ENTRY_OUT", 1)
    grouped: dict[str, dict] = defaultdict(
        lambda: {"trades": 0, "made": 0.0, "lost": 0.0, "net": 0.0,
                 "positions": set()}
    )
    for deal in deals:
        if getattr(deal, "entry", None) not in (entry_out, "out", "OUT"):
            continue
        timestamp = _deal_value(deal, "time", 0.0)
        if not timestamp:
            continue
        month = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m")
        value = (
            _deal_value(deal, "profit")
            + _deal_value(deal, "swap")
            + _deal_value(deal, "commission")
            + _deal_value(deal, "fee")
        )
        row = grouped[month]
        position_id = getattr(deal, "position_id", None)
        if position_id:
            row["positions"].add(position_id)
        else:
            row["trades"] += 1
        if value >= 0:
            row["made"] += value
        else:
            row["lost"] += abs(value)
        row["net"] += value

    rows = []
    for month, row in sorted(grouped.items()):
        rows.append({
            "month": month,
            "trades": len(row["positions"]) or row["trades"],
            "made": round(row["made"], 2),
            "lost": round(row["lost"], 2),
            "net": round(row["net"], 2),
        })

    account = mt5.account_info()
    return {
        "ok": True,
        "currency": getattr(account, "currency", "") if account else "",
        "rows": rows,
        "source": "MetaTrader 5 closed deals",
    }


def _with_mt5(reader: Callable[[object], object], account_id: str = ""):
    """Serialize terminal access and always release the MT5 connection."""
    with _MT5_LOCK:
        from memory.config_manager import (
            get_mt5_credentials,
            get_mt5_credentials_for_account,
        )

        credentials = (
            get_mt5_credentials_for_account(account_id)
            if account_id else get_mt5_credentials()
        )
        if not credentials:
            raise RuntimeError(
                "MT5 is not configured. Add the broker-issued investor/read-only "
                "credentials in setup; JARVIS will not attach to a trading session."
            )
        mt5 = _load_mt5()
        if not mt5.initialize(
            login=credentials["login"],
            password=credentials["password"],
            server=credentials["server"],
        ):
            code, message = mt5.last_error()
            if code == -10005:
                raise RuntimeError(
                    "MetaTrader 5 is not running or is not ready. "
                    "Open the MT5 desktop terminal."
                )
            raise RuntimeError(f"MT5 investor connection failed ({code}): {message}")
        try:
            account = mt5.account_info()
            if account is None:
                code, message = mt5.last_error()
                raise RuntimeError(
                    f"MT5 account verification failed ({code}): {message}"
                )
            if int(getattr(account, "login", 0) or 0) != credentials["login"]:
                raise RuntimeError(
                    "MT5 connected to a different account than the configured investor login."
                )
            actual_server = str(getattr(account, "server", "") or "")
            if actual_server and actual_server != credentials["server"]:
                raise RuntimeError(
                    "MT5 connected to a different broker server than the configured investor account."
                )
            if bool(getattr(account, "trade_allowed", True)):
                raise RuntimeError(
                    "MT5 reports trading permission for this login. Use the broker-issued "
                    "investor/read-only password; JARVIS will not read a trading login."
                )
            return reader(mt5)
        finally:
            mt5.shutdown()


def monthly_history(months: int = 24, account_id: str = "") -> dict:
    if account_id:
        report = _with_mt5(
            lambda mt5: _monthly_history_unlocked(mt5, months), account_id
        )
        report["account_id"] = account_id
        return report
    from memory.config_manager import get_mt5_accounts

    accounts = get_mt5_accounts()
    if not accounts:
        return _with_mt5(lambda mt5: _monthly_history_unlocked(mt5, months))
    if len(accounts) == 1:
        account = accounts[0]
        identifier = str(account.get("id") or f"{account['login']}@{account['server']}")
        report = _with_mt5(
            lambda mt5: _monthly_history_unlocked(mt5, months), identifier
        )
        report["account_id"] = identifier
        report["account_name"] = account.get("name", identifier)
        return report
    reports = []
    errors = []
    for account in accounts:
        identifier = str(account.get("id") or f"{account['login']}@{account['server']}")
        try:
            report = _with_mt5(
                lambda mt5: _monthly_history_unlocked(mt5, months), identifier
            )
            report["account_id"] = identifier
            report["account_name"] = account.get("name", identifier)
            reports.append(report)
        except Exception as exc:
            errors.append(f"{account.get('name', identifier)}: {exc}")
    if not reports:
        raise RuntimeError("; ".join(errors) or "No MT5 accounts are configured.")
    return {
        "ok": True,
        "source": "MetaTrader 5 closed deals across configured accounts",
        "accounts": reports,
        "errors": errors,
    }


def _resolve_symbol(mt5, requested: str) -> tuple[str, object]:
    requested = requested.strip().upper()
    info = mt5.symbol_info(requested)
    if info is not None and mt5.symbol_select(requested, True):
        return requested, info

    symbols = mt5.symbols_get() or ()
    normalized = "".join(char for char in requested if char.isalnum())
    matches = [
        str(getattr(item, "name", ""))
        for item in symbols
        if "".join(char for char in str(getattr(item, "name", "")).upper()
                   if char.isalnum()).startswith(normalized)
    ]
    for candidate in sorted(matches, key=lambda name: (len(name), name)):
        info = mt5.symbol_info(candidate)
        if info is not None and mt5.symbol_select(candidate, True):
            return candidate, info
    raise RuntimeError(f"MT5 does not offer the symbol {requested} or a broker suffix match.")


def _market_snapshot(mt5, symbol: str) -> dict:
    symbol = (symbol or "").strip().upper()
    if not symbol or len(symbol) > 32 or not all(
        char.isalnum() or char in "._-#" for char in symbol
    ):
        raise ValueError("Enter a valid broker symbol, such as EURUSD.")

    requested_symbol = symbol
    symbol, info = _resolve_symbol(mt5, symbol)
    tick = mt5.symbol_info_tick(symbol)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 1, 80)
    if tick is None or rates is None or len(rates) < 20:
        code, message = mt5.last_error()
        raise RuntimeError(f"No live quote/history for {symbol} ({code}: {message}).")

    closes = [float(rate["close"]) for rate in rates]
    ranges = [
        max(
            float(rate["high"]) - float(rate["low"]),
            abs(float(rate["high"]) - closes[index - 1]) if index else 0.0,
            abs(float(rate["low"]) - closes[index - 1]) if index else 0.0,
        )
        for index, rate in enumerate(rates)
    ]
    fast_mean = fmean(closes[-20:])
    slow_mean = fmean(closes[-50:]) if len(closes) >= 50 else fmean(closes)
    price = float(getattr(tick, "bid", 0.0) or closes[-1])
    if fast_mean > slow_mean * 1.0002:
        trend = "bullish"
    elif fast_mean < slow_mean * 0.9998:
        trend = "bearish"
    else:
        trend = "sideways"

    point = float(getattr(info, "point", 0.0) or 0.0)
    digits = int(getattr(info, "digits", 0) or 0)
    pip_size = point * 10 if digits in (3, 5) else point
    spread_points = (
        (float(getattr(tick, "ask", 0.0)) - float(getattr(tick, "bid", 0.0))) / point
        if point > 0 else 0.0
    )
    spread_history = [float(rate["spread"]) for rate in rates[-60:] if "spread" in rate.dtype.names]
    normal_spread = median(spread_history) if spread_history else 0.0
    recent_atr = fmean(ranges[-14:])
    baseline_atr = fmean(ranges[-60:-14]) if len(ranges) >= 60 else fmean(ranges[:-14])
    volatility_ratio = recent_atr / baseline_atr if baseline_atr > 0 else 1.0
    hourly_reference = closes[-12] if len(closes) >= 12 else closes[0]

    return {
        "symbol": symbol,
        "watch_symbol": requested_symbol,
        "source": "MetaTrader 5",
        "digits": digits,
        "price": price,
        "ask": float(getattr(tick, "ask", 0.0)),
        "trend": trend,
        "change_1h_percent": round((price / hourly_reference - 1) * 100, 3)
        if hourly_reference else 0.0,
        "fast_average": round(fast_mean, digits),
        "slow_average": round(slow_mean, digits),
        "spread_pips": round(spread_points * point / pip_size, 2) if pip_size else None,
        "spread_vs_normal": round(spread_points / normal_spread, 2) if normal_spread else None,
        "volatility_ratio": round(volatility_ratio, 2),
        "volatility_abnormal": volatility_ratio >= 2.5,
        "spread_abnormal": bool(normal_spread and spread_points >= normal_spread * 3),
        "quote_age_seconds": max(0, int(time.time() - float(getattr(tick, "time", time.time())))),
    }


def market_snapshot(symbol: str) -> dict:
    """Read a live MT5 quote and five-minute trend/volatility context."""
    try:
        return _with_mt5(lambda mt5: _market_snapshot(mt5, symbol))
    except Exception as mt5_error:
        try:
            return _public_market_snapshot(symbol)
        except Exception as public_error:
            raise RuntimeError(
                f"MT5 unavailable ({mt5_error}); public quote fallback unavailable ({public_error})."
            ) from public_error


def _public_market_snapshot(symbol: str) -> dict:
    """Best-effort public price fallback; never used to claim broker/account risk."""
    requested = (symbol or "").strip().upper()
    if requested not in PUBLIC_WATCH_SYMBOLS:
        raise ValueError(f"No public quote mapping is configured for {requested}.")
    now = time.time()
    with _PUBLIC_QUOTE_LOCK:
        cached = _PUBLIC_QUOTE_CACHE.get(requested)
        if cached and now - cached[0] < 15:
            result = dict(cached[1])
            result["quote_age_seconds"] += int(now - cached[0])
            return result

    if requested == "XAUUSD":
        response = requests.get(
            "https://api.gold-api.com/price/XAU",
            headers={"User-Agent": "TradBot-I market watch"},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        updated = datetime.fromisoformat(
            str(payload["updatedAt"]).replace("Z", "+00:00")
        ).timestamp()
        price = float(payload["price"])
        result = {
            "symbol": "XAUUSD",
            "watch_symbol": requested,
            "source": "Gold-API public spot",
            "digits": 2,
            "price": price,
            "ask": None,
            "trend": "unavailable",
            "change_1h_percent": 0.0,
            "fast_average": price,
            "slow_average": price,
            "spread_pips": None,
            "spread_vs_normal": None,
            "volatility_ratio": 1.0,
            "volatility_abnormal": False,
            "spread_abnormal": False,
            "quote_age_seconds": max(0, int(now - updated)),
        }
    else:
        ticker = _PUBLIC_TICKERS[requested]
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"range": "1d", "interval": "5m"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        response.raise_for_status()
        chart = response.json().get("chart", {})
        if chart.get("error") or not chart.get("result"):
            raise RuntimeError(f"Public quote source returned no data for {ticker}.")
        data = chart["result"][0]
        meta = data.get("meta", {})
        series = data.get("indicators", {}).get("quote", [{}])[0]
        closes = [float(value) for value in series.get("close", []) if value is not None]
        highs = [float(value) for value in series.get("high", []) if value is not None]
        lows = [float(value) for value in series.get("low", []) if value is not None]
        if not closes:
            raise RuntimeError(f"Public quote source returned no prices for {ticker}.")
        price = float(meta.get("regularMarketPrice", closes[-1]))
        fast = fmean(closes[-12:])
        slow = fmean(closes[-50:]) if len(closes) >= 50 else fmean(closes)
        trend = "bullish" if fast > slow * 1.0002 else "bearish" if fast < slow * 0.9998 else "sideways"
        ranges = [high - low for high, low in zip(highs, lows)]
        recent = fmean(ranges[-12:]) if ranges else 0.0
        baseline = fmean(ranges[-60:-12]) if len(ranges) >= 60 else fmean(ranges[:-12]) if len(ranges) > 12 else 0.0
        timestamps = data.get("timestamp") or []
        updated = int(timestamps[-1]) if timestamps else int(meta.get("regularMarketTime", now))
        hint = int(meta.get("priceHint", 2) or 2)
        result = {
            "symbol": requested,
            "watch_symbol": requested,
            "source": "Yahoo Finance public quote",
            "digits": max(0, min(8, hint)),
            "price": price,
            "ask": None,
            "trend": trend,
            "change_1h_percent": round((price / closes[-12] - 1) * 100, 3) if len(closes) >= 12 and closes[-12] else 0.0,
            "fast_average": round(fast, hint),
            "slow_average": round(slow, hint),
            "spread_pips": None,
            "spread_vs_normal": None,
            "volatility_ratio": round(recent / baseline, 2) if baseline > 0 else 1.0,
            "volatility_abnormal": bool(baseline > 0 and recent / baseline >= 2.5),
            "spread_abnormal": False,
            "quote_age_seconds": max(0, int(now - updated)),
        }

    with _PUBLIC_QUOTE_LOCK:
        _PUBLIC_QUOTE_CACHE[requested] = (now, dict(result))
    return result


def _account_snapshot(mt5) -> dict:
    account = mt5.account_info()
    positions = mt5.positions_get()
    if account is None or positions is None:
        code, message = mt5.last_error()
        raise RuntimeError(f"MT5 account data unavailable ({code}: {message}).")

    grouped: dict[str, dict] = defaultdict(
        lambda: {"long_lots": 0.0, "short_lots": 0.0, "floating_pnl": 0.0,
                 "positions": 0, "approx_notional": 0.0, "notional_currency": ""}
    )
    position_rows = []
    buy_type = getattr(mt5, "POSITION_TYPE_BUY", 0)
    for position in positions:
        symbol = str(getattr(position, "symbol", ""))
        volume = float(getattr(position, "volume", 0.0) or 0.0)
        is_long = getattr(position, "type", None) == buy_type
        row = grouped[symbol]
        row["long_lots" if is_long else "short_lots"] += volume
        row["floating_pnl"] += float(getattr(position, "profit", 0.0) or 0.0)
        row["positions"] += 1
        info = mt5.symbol_info(symbol)
        if info is not None:
            row["approx_notional"] += (
                volume
                * float(getattr(info, "trade_contract_size", 0.0) or 0.0)
                * float(getattr(position, "price_current", 0.0) or 0.0)
            )
            row["notional_currency"] = str(getattr(info, "currency_profit", "") or "")
        position_rows.append({
            "ticket": str(getattr(position, "ticket", "") or ""),
            "symbol": symbol,
            "direction": "long" if is_long else "short",
            "lots": volume,
            "entry": float(getattr(position, "price_open", 0.0) or 0.0),
            "current": float(getattr(position, "price_current", 0.0) or 0.0),
            "floating_pnl": float(getattr(position, "profit", 0.0) or 0.0),
            "stop_loss": float(getattr(position, "sl", 0.0) or 0.0),
            "take_profit": float(getattr(position, "tp", 0.0) or 0.0),
        })

    exposure = []
    for symbol, row in sorted(grouped.items()):
        long_lots = row["long_lots"]
        short_lots = row["short_lots"]
        exposure.append({
            "symbol": symbol,
            "positions": row["positions"],
            "long_lots": round(long_lots, 2),
            "short_lots": round(short_lots, 2),
            "net_lots": round(long_lots - short_lots, 2),
            "floating_pnl": round(row["floating_pnl"], 2),
            "approx_notional": round(row["approx_notional"], 2),
            "notional_currency": row["notional_currency"],
        })

    return {
        "currency": str(getattr(account, "currency", "") or ""),
        "login": str(getattr(account, "login", "") or ""),
        "server": str(getattr(account, "server", "") or ""),
        "balance": float(getattr(account, "balance", 0.0) or 0.0),
        "equity": float(getattr(account, "equity", 0.0) or 0.0),
        "floating_pnl": float(getattr(account, "profit", 0.0) or 0.0),
        "margin": float(getattr(account, "margin", 0.0) or 0.0),
        "free_margin": float(getattr(account, "margin_free", 0.0) or 0.0),
        "margin_level_percent": float(getattr(account, "margin_level", 0.0) or 0.0),
        "position_count": len(position_rows),
        "exposure": exposure,
        "positions": position_rows,
    }


def account_snapshot(account_id: str = "") -> dict:
    """Read account margin, equity, open positions, and per-symbol exposure."""
    return _with_mt5(_account_snapshot, account_id)


def _group_closed_trades(mt5, deals) -> list[dict]:
    entry_in = getattr(mt5, "DEAL_ENTRY_IN", 0)
    close_entries = {
        getattr(mt5, "DEAL_ENTRY_OUT", 1),
        getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
        getattr(mt5, "DEAL_ENTRY_INOUT", 2),
    }
    buy_type = getattr(mt5, "DEAL_TYPE_BUY", 0)
    grouped: dict[int, dict] = {}
    for deal in deals:
        position_id = getattr(deal, "position_id", None) or getattr(deal, "ticket", None)
        if position_id is None:
            continue
        timestamp = int(_deal_value(deal, "time", 0))
        row = grouped.setdefault(position_id, {
            "ticket": str(position_id), "symbol": str(getattr(deal, "symbol", "")),
            "side": "buy" if getattr(deal, "type", None) == buy_type else "sell",
            "opened_at": 0, "closed_at": 0, "net": 0.0, "volume": 0.0,
        })
        entry = getattr(deal, "entry", None)
        if entry == entry_in:
            row["opened_at"] = min(row["opened_at"] or timestamp, timestamp)
            row["volume"] += _deal_value(deal, "volume")
        if entry in close_entries:
            row["closed_at"] = max(row["closed_at"], timestamp)
            row["net"] += sum(
                _deal_value(deal, key) for key in ("profit", "swap", "commission", "fee")
            )

    rows = []
    for row in grouped.values():
        if not row["closed_at"]:
            continue
        rows.append({
            "ticket": row["ticket"],
            "symbol": row["symbol"],
            "side": row["side"],
            "opened_at": (
                datetime.fromtimestamp(row["opened_at"], timezone.utc).isoformat()
                if row["opened_at"] else ""
            ),
            "closed_at": datetime.fromtimestamp(row["closed_at"], timezone.utc).isoformat(),
            "volume": round(row["volume"], 4),
            "net": round(row["net"], 2),
            "_closed_ts": row["closed_at"],
        })
    return sorted(rows, key=lambda row: row["_closed_ts"])


def _behavior_snapshot(mt5, hours: int = 24) -> dict:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=max(1, min(int(hours), 168)))
    deals = mt5.history_deals_get(start, now)
    if deals is None:
        code, message = mt5.last_error()
        raise RuntimeError(f"MT5 deal history unavailable ({code}: {message}).")

    closed_trades = _group_closed_trades(mt5, deals)
    closed = [
        {
            "opened_at": int(datetime.fromisoformat(row["opened_at"]).timestamp())
            if row["opened_at"] else 0,
            "closed_at": row["_closed_ts"],
            "net": row["net"],
            "volume": row["volume"],
            "ticket": row["ticket"],
        }
        for row in closed_trades
    ]
    one_hour_ago = now.timestamp() - 3600
    recent_hour = [row for row in closed if row["closed_at"] >= one_hour_ago]
    loss_streak = 0
    for row in reversed(closed):
        if row["net"] >= 0:
            break
        loss_streak += 1

    rapid_reentry = False
    size_increase_after_loss = False
    by_open_time = sorted(
        (row for row in closed if row["opened_at"]),
        key=lambda row: row["opened_at"],
    )
    for previous, current in zip(by_open_time, by_open_time[1:]):
        gap = current["opened_at"] - previous["closed_at"]
        if previous["net"] < 0 and 0 <= gap <= 180:
            rapid_reentry = True
            if previous["volume"] > 0 and current["volume"] > previous["volume"] * 1.5:
                size_increase_after_loss = True

    flags = []
    if len(recent_hour) >= 8:
        flags.append("high_trade_frequency")
    if loss_streak >= 3:
        flags.append("loss_streak")
    if rapid_reentry:
        flags.append("rapid_reentry_after_loss")
    if size_increase_after_loss:
        flags.append("size_increased_after_loss")
    return {
        "period_hours": max(1, min(int(hours), 168)),
        "closed_positions": len(closed),
        "closed_last_hour": len(recent_hour),
        "consecutive_losses": loss_streak,
        "flags": flags,
        "closed_trades": closed_trades,
    }


def behavior_snapshot(hours: int = 24, account_id: str = "") -> dict:
    """Describe trade-frequency and loss-response patterns, not emotions."""
    return _with_mt5(lambda mt5: _behavior_snapshot(mt5, hours), account_id)


def _monitor_snapshot(mt5, account_id: str = "") -> dict:
    account = _account_snapshot(mt5)
    behavior = _behavior_snapshot(mt5)
    account_id = account_id or account["login"]
    if account["login"] and "@" not in account_id and account["server"]:
        account_id = f"{account_id}@{account['server']}"
    now = time.monotonic()
    if account["login"] and now - _JOURNAL_LAST_SYNC.get(account_id, 0.0) >= _JOURNAL_SYNC_INTERVAL:
        with _JOURNAL_SYNC_LOCK:
            now = time.monotonic()
            if now - _JOURNAL_LAST_SYNC.get(account_id, 0.0) >= _JOURNAL_SYNC_INTERVAL:
                _JOURNAL_LAST_SYNC[account_id] = now
                try:
                    from memory.trading_journal import (
                        history_sync_start, mark_history_synced, save_mt5_trades,
                    )

                    start = history_sync_start(account_id)
                    deals = mt5.history_deals_get(start, datetime.now(timezone.utc))
                    if deals is None:
                        code, message = mt5.last_error()
                        raise RuntimeError(f"MT5 history sync failed ({code}): {message}")

                    rows = _group_closed_trades(mt5, deals)
                    for row in rows:
                        row.pop("_closed_ts", None)
                    save_mt5_trades(account_id, rows)
                    mark_history_synced(account_id)
                    _JOURNAL_SYNC_ERROR[account_id] = ""
                except Exception as exc:
                    _JOURNAL_SYNC_ERROR[account_id] = str(exc)
                    print(f"[TradeJournal] History sync failed: {exc}")
    journal = {}
    if account_id:
        try:
            from memory.trading_journal import daily_summary

            journal = daily_summary(account_id)
            _JOURNAL_READ_ERROR[account_id] = ""
        except Exception as exc:
            _JOURNAL_READ_ERROR[account_id] = str(exc)
            print(f"[TradeJournal] Daily summary failed: {exc}")
    markets = []
    from memory.config_manager import get_watch_symbols

    symbols = get_watch_symbols()
    symbols.extend(item["symbol"] for item in account["exposure"][:10])
    for symbol in dict.fromkeys(symbols):
        try:
            markets.append(_market_snapshot(mt5, symbol))
        except Exception as exc:
            markets.append({"symbol": symbol, "watch_symbol": symbol, "error": str(exc)})
    return {
        "account": account, "behavior": behavior, "markets": markets,
        "journal": journal,
        "journal_error": (
            _JOURNAL_SYNC_ERROR.get(account_id, "")
            or _JOURNAL_READ_ERROR.get(account_id, "")
        ),
    }


_WATCH_LOCK = threading.Lock()
_WATCH_LAST_ALERT: dict[str, float] = {}
_WATCH_SEEN_NEWS: set[str] = set()
_JOURNAL_SYNC_LOCK = threading.Lock()
_JOURNAL_LAST_SYNC: dict[str, float] = {}
_JOURNAL_SYNC_INTERVAL = 900
_JOURNAL_SYNC_ERROR: dict[str, str] = {}
_JOURNAL_READ_ERROR: dict[str, str] = {}
_TRADE_EVENT_LOCK = threading.Lock()
_TRADE_EVENT_STATE: dict[str, tuple[set[str], set[str]]] = {}
_HIGH_IMPACT_TERMS = (
    "central bank", "interest rate", "rate decision", "rate cut", "rate hike",
    "rate statement", "fed minutes", "powell", "inflation", "cpi", "pce",
    "payroll", "nonfarm", "unemployment", "jobs report", "fomc", "ecb",
    "bank of england", "bank of japan", "gdp", "recession", "treasury yield",
    "bond yield", "dxy", "dollar index", "gold", "xauusd", "bullion",
    "opec", "oil", "israel", "iran", "ukraine", "china", "debt ceiling",
    "sanctions", "tariff", "trade war", "war", "ceasefire", "oil supply",
    "emergency intervention", "market crash", "market halt",
)


def _watch_alert(key: str, message: str, cooldown: int = 900) -> str | None:
    now = time.monotonic()
    with _WATCH_LOCK:
        if now - _WATCH_LAST_ALERT.get(key, 0.0) < cooldown:
            return None
        _WATCH_LAST_ALERT[key] = now
    return message


def _risk_alerts(snapshot: dict) -> list[str]:
    alerts = []
    account = snapshot["account"]
    account_id = str(account.get("login", ""))
    if account.get("server"):
        account_id = f"{account_id}@{account['server']}"
    prefix = f"[{account_id}] " if account_id else ""
    margin_level = account["margin_level_percent"]
    if account["margin"] > 0 and 0 < margin_level < 200:
        alert = _watch_alert(
            f"{account_id}:margin_level",
            f"{prefix}Account margin level is {margin_level:.0f}%. Check the broker's margin and stop-out requirements.",
        )
        if alert:
            alerts.append(alert)

    for market in snapshot["markets"]:
        symbol = market["symbol"]
        if market.get("volatility_abnormal"):
            alert = _watch_alert(
                f"{account_id}:volatility:{symbol}",
                f"{prefix}{symbol} five-minute volatility is {market['volatility_ratio']:.1f} times its recent baseline.",
            )
            if alert:
                alerts.append(alert)
        if market.get("spread_abnormal"):
            spread = market.get("spread_pips")
            spread_text = f"{spread:.1f} pips" if spread is not None else "elevated"
            alert = _watch_alert(
                f"{account_id}:spread:{symbol}",
                f"{prefix}{symbol} spread is unusually wide ({spread_text}, about {market['spread_vs_normal']:.1f} times normal).",
            )
            if alert:
                alerts.append(alert)

    journal = snapshot.get("journal", {})
    if journal:
        from memory.config_manager import get_trader_profile

        profile = get_trader_profile()
        try:
            max_trades = int(profile.get("max_trades_per_day", 0) or 0)
        except (TypeError, ValueError):
            max_trades = 0
        if max_trades and journal["closed_trades"] >= max_trades:
            alert = _watch_alert(
                f"{account_id}:personal_trade_limit:{max_trades}",
                f"{prefix}The trade journal records {journal['closed_trades']} closed trades today, "
                f"at or above your stated daily maximum of {max_trades}.",
                cooldown=1800,
            )
            if alert:
                alerts.append(alert)
        try:
            daily_loss_limit = float(profile.get("daily_loss_limit_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            daily_loss_limit = 0.0
        balance = float(account.get("balance", 0.0) or 0.0)
        loss_pct = (
            journal["realized_loss"] / balance * 100
            if balance > 0 else 0.0
        )
        if daily_loss_limit and loss_pct >= daily_loss_limit:
            alert = _watch_alert(
                f"{account_id}:personal_daily_loss:{daily_loss_limit}",
                f"{prefix}Today's recorded realized losses are about {loss_pct:.2f}% of current "
                f"balance, at or above your stated daily limit of {daily_loss_limit:.2f}%. "
                "This comparison uses current balance as an approximation.",
                cooldown=1800,
            )
            if alert:
                alerts.append(alert)

    behavior = snapshot["behavior"]
    behavior_messages = {
        "high_trade_frequency": (
            f"Trading activity is unusually frequent: {behavior['closed_last_hour']} positions closed in the last hour."
        ),
        "loss_streak": (
            f"A streak of {behavior['consecutive_losses']} consecutive losing positions was detected. "
            "This is a pattern alert, not a conclusion about intent."
        ),
        "rapid_reentry_after_loss": (
            "A position was reopened within three minutes of a losing close; consider reviewing the entry against your plan."
        ),
        "size_increased_after_loss": (
            "Position size increased substantially after a losing trade; review exposure before the next decision."
        ),
    }
    for flag, message in behavior_messages.items():
        if flag in behavior["flags"]:
            alert = _watch_alert(
                f"{account_id}:behavior:{flag}", prefix + message, cooldown=1800
            )
            if alert:
                alerts.append(alert)
    return alerts


def _news_results(query: str = "", max_results: int = 8) -> list[dict]:
    from actions.web_search import _ddg_news, _run_bounded

    query = (query or "high impact gold XAUUSD US dollar Fed CPI PCE payroll Treasury yield "
             "economic events geopolitics market news today").strip()
    results = _run_bounded(
        lambda: _ddg_news(query, max_results=max(1, min(int(max_results), 10))),
        timeout=5.0,
        label="market news",
    )
    return results or []


def high_impact_news_updates() -> list[dict[str, str]]:
    """Return deduplicated, high-impact gold/USD news candidates for alert delivery."""
    queries = (
        "breaking gold XAUUSD major price moving news Fed geopolitics",
        "breaking US dollar DXY major Fed CPI PCE NFP Treasury market moving news",
        "breaking major geopolitical event global markets gold dollar impact",
    )
    results = []
    for query in queries:
        results.extend(_news_results(query, max_results=5))
    updates = []
    for article in results:
        title = str(article.get("title", "")).strip()
        if not title:
            continue
        text = f"{title} {article.get('snippet', '')}".lower()
        impact_terms = [term for term in _HIGH_IMPACT_TERMS if term in text]
        if not impact_terms:
            continue
        gold_usd_terms = (
            "gold", "xau", "bullion", "dollar", "usd", "dxy", "federal reserve",
            "fed ", "fomc", "treasury", "cpi", "pce", "payroll", "nonfarm",
            "interest rate", "rate decision", "yield",
        )
        has_market_link = any(term in text for term in gold_usd_terms)
        has_major_event = any(term in text for term in (
            "emergency", "unexpected", "surprise", "halt", "crash", "invasion",
            "war", "ceasefire", "sanction", "intervention", "default", "cut by",
            "hike by", "rate decision", "rate cut", "rate hike", "fomc", "cpi",
            "nonfarm", "payroll", "assassination", "coup",
        ))
        if not has_market_link or not has_major_event:
            continue
        fingerprint = title.lower()
        with _WATCH_LOCK:
            if fingerprint in _WATCH_SEEN_NEWS:
                continue
            _WATCH_SEEN_NEWS.add(fingerprint)
            if len(_WATCH_SEEN_NEWS) > 500:
                _WATCH_SEEN_NEWS.clear()
                _WATCH_SEEN_NEWS.add(fingerprint)
        source = str(article.get("source", "")).strip()
        source_text = f" ({source})" if source else ""
        updates.append({
            "key": fingerprint,
            "title": title,
            "source": source_text.strip(" ()"),
            "impact_terms": ", ".join(impact_terms[:4]),
        })
    return updates


def _trade_events(snapshot: dict) -> list[dict]:
    account = snapshot["account"]
    account_id = str(account.get("login", ""))
    if account.get("server"):
        account_id = f"{account_id}@{account['server']}"
    positions = snapshot["account"].get("positions", [])
    open_now = {
        str(position.get("ticket", "")): position
        for position in positions if position.get("ticket")
    }
    closed_now = {
        str(trade.get("ticket", "")): trade
        for trade in snapshot["behavior"].get("closed_trades", [])
        if trade.get("ticket")
    }
    with _TRADE_EVENT_LOCK:
        if account_id not in _TRADE_EVENT_STATE:
            _TRADE_EVENT_STATE[account_id] = (set(open_now), set(closed_now))
            return []
        open_tickets, closed_tickets = _TRADE_EVENT_STATE[account_id]
        newly_opened = [
            position for ticket, position in open_now.items()
            if ticket not in open_tickets
        ]
        newly_closed = [
            trade for ticket, trade in closed_now.items()
            if ticket not in closed_tickets
        ]
        open_tickets.clear()
        open_tickets.update(open_now)
        closed_tickets.update(closed_now)
        if len(closed_tickets) > 5000:
            closed_tickets.clear()
            closed_tickets.update(closed_now)

    events = [
        {
            "event": "opened", "ticket": str(row.get("ticket", "")),
            "symbol": str(row.get("symbol", "")),
            "side": str(row.get("direction", "")),
            "volume": float(row.get("lots", 0.0)),
            "account_id": account_id,
        }
        for row in newly_opened
    ]
    events.extend({
        "event": "closed", "ticket": row["ticket"], "symbol": row["symbol"],
        "side": row["side"], "volume": row["volume"], "net": row["net"],
        "account_id": account_id,
    } for row in newly_closed)
    return events


def monitoring_check() -> dict:
    """Read current exposure and emit deduplicated risk, behavior, and news alerts."""
    from memory.config_manager import (
        get_mt5_account_id, get_mt5_accounts, get_watch_symbols,
    )

    snapshots = []
    errors = []
    accounts = get_mt5_accounts()
    active_account_id = get_mt5_account_id()
    accounts.sort(
        key=lambda account: (
            str(account.get("id") or f"{account['login']}@{account['server']}")
            != active_account_id
        )
    )
    for account in accounts:
        account_id = str(account.get("id") or f"{account['login']}@{account['server']}")
        try:
            snapshots.append(
                _with_mt5(
                    lambda mt5, identifier=account_id: _monitor_snapshot(mt5, identifier),
                    account_id,
                )
            )
        except Exception as exc:
            errors.append(f"{account.get('name', account_id)}: {exc}")

    snapshot = snapshots[0] if snapshots else None
    error = "; ".join(errors)
    alerts = [alert for item in snapshots for alert in _risk_alerts(item)]
    trade_events = [event for item in snapshots for event in _trade_events(item)]
    markets = list(snapshot.get("markets", [])) if snapshot else []
    existing = {item.get("watch_symbol", item.get("symbol")) for item in markets}
    watch_symbols = get_watch_symbols()
    for symbol in watch_symbols:
        if symbol in existing and not next(
            (item.get("error") for item in markets
             if item.get("watch_symbol", item.get("symbol")) == symbol),
            None,
        ):
            continue
        try:
            fallback = _public_market_snapshot(symbol)
            markets = [
                item for item in markets
                if item.get("watch_symbol", item.get("symbol")) != symbol
            ]
            markets.append(fallback)
        except Exception as exc:
            if symbol not in existing:
                markets.append({"symbol": symbol, "watch_symbol": symbol, "error": str(exc)})
    return {
        "snapshot": snapshot, "snapshots": snapshots, "markets": markets, "alerts": alerts,
        "trade_events": trade_events,
        "journal_error": "; ".join(
            item.get("journal_error", "") for item in snapshots
            if item.get("journal_error")
        ),
        "error": error,
    }


def daily_market_report() -> str:
    """Build a compact, read-only daily report for locally configured delivery."""
    result = monitoring_check()
    lines = [f"JARVIS Daily Market Update · {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}"]
    snapshot = result.get("snapshot")
    snapshots = result.get("snapshots", [])
    if result.get("error"):
        lines.append(f"Account data unavailable: {result['error']}")
    if snapshots:
        for account_snapshot in snapshots:
            account = account_snapshot["account"]
            account_id = str(account.get("login", ""))
            if account.get("server"):
                account_id = f"{account_id}@{account['server']}"
            lines.append(f"Account {account_id}:")
            lines.append(_format_account(account))
            lines.extend(
                _format_market(market)
                for market in account_snapshot.get("markets", [])
                if not market.get("error")
            )
            lines.append(_format_behavior(account_snapshot["behavior"]))
    elif snapshot:
        lines.append(_format_account(snapshot["account"]))
        lines.extend(
            _format_market(market)
            for market in result.get("markets", snapshot["markets"])
            if not market.get("error")
        )
        lines.append(_format_behavior(snapshot["behavior"]))
    else:
        lines.append(f"MT5 data unavailable: {result.get('error') or 'unknown error'}")
        lines.extend(
            _format_market(market)
            for market in result.get("markets", [])
            if not market.get("error")
        )

    headlines = []
    seen_headlines = set()
    for query in (
        "gold XAUUSD bullion market news today",
        "US dollar USD Federal Reserve economic events today",
    ):
        for article in _news_results(query, max_results=3):
            title = str(article.get("title", "")).strip()
            if title and title.casefold() not in seen_headlines:
                seen_headlines.add(title.casefold())
                source = str(article.get("source", "")).strip()
                headlines.append(f"{title}" + (f" [{source}]" if source else ""))
    lines.append("Headlines (search triage, not an official calendar):")
    lines.extend(f"- {title}" for title in headlines[:6])
    if not headlines:
        lines.append("- No current headlines returned by the configured search source.")
    if result.get("alerts"):
        lines.append("Risk and behavior alerts:")
        lines.extend(f"- {alert}" for alert in result["alerts"][:8])
    lines.append("Read-only market context; not a trade instruction or guaranteed feed.")
    return "\n".join(lines)


def analyze_tradingview_frames(
    previous: bytes, previous_mime: str, current: bytes, current_mime: str, title: str
) -> dict:
    """Compare two user-authorized TradingView window frames for visible changes."""
    from google.genai import types
    from core import gemini

    prompt = (
        "Compare FRAME A (15 seconds ago) with FRAME B (now). This is a read-only "
        "TradingView window observer. Return JSON only with keys: visible_change "
        "(boolean), market_context (brief directly visible chart facts), "
        "behavior_observation (string or null), confidence (0 to 1). Only report "
        "behavior when a concrete on-screen trading-control change is unmistakable; "
        "a chart moving is not user behavior. Never infer emotions, claim an order "
        "was executed unless the UI clearly proves it, recommend a trade, or predict "
        f"prices. Window title: {title!r}."
    )
    response = gemini.call(
        [
            types.Part.from_bytes(data=previous, mime_type=previous_mime),
            types.Part.from_bytes(data=current, mime_type=current_mime),
            prompt,
        ],
        tier=gemini.SMART,
        timeout_ms=12_000,
    )
    if response is None:
        raise RuntimeError("Gemini screen analysis is unavailable.")
    text = str(getattr(response, "text", "") or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        result = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Gemini returned an unreadable screen observation.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Gemini returned an invalid screen observation.")
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    behavior = result.get("behavior_observation")
    return {
        "visible_change": bool(result.get("visible_change")),
        "market_context": str(result.get("market_context", ""))[:500],
        "behavior_observation": str(behavior)[:300] if behavior else None,
        "confidence": confidence,
    }


def _format_market(snapshot: dict) -> str:
    spread = snapshot["spread_pips"]
    spread_text = f"{spread:.2f} pips" if spread is not None else "unavailable"
    source = snapshot.get("source", "MetaTrader 5")
    price_label = "bid" if source == "MetaTrader 5" else "reference price"
    ask = snapshot.get("ask")
    ask_text = f"{ask}" if ask is not None else "unavailable"
    return (
        f"{snapshot['symbol']} is {snapshot['trend']} on the five-minute context ({source}). "
        f"{price_label}: {snapshot['price']}; ask: {ask_text}; "
        f"one-hour change: {snapshot['change_1h_percent']:+.3f}%; spread: {spread_text}; "
        f"short/normal volatility ratio: {snapshot['volatility_ratio']:.2f}."
    )


def _format_account(snapshot: dict) -> str:
    currency = snapshot["currency"]
    lines = [
        f"Balance {snapshot['balance']:.2f} {currency}; equity {snapshot['equity']:.2f} {currency}; "
        f"floating P/L {snapshot['floating_pnl']:.2f} {currency}; "
        f"used margin {snapshot['margin']:.2f}; free margin {snapshot['free_margin']:.2f}; "
        f"margin level {snapshot['margin_level_percent']:.1f}%; "
        f"{snapshot['position_count']} open position(s).",
    ]
    for item in snapshot["exposure"]:
        notional = (
            f"; approximate notional {item['approx_notional']:.2f} {item['notional_currency']}"
            if item["notional_currency"] else ""
        )
        lines.append(
            f"{item['symbol']}: long {item['long_lots']:.2f} lots, "
            f"short {item['short_lots']:.2f}, net {item['net_lots']:+.2f}, "
            f"floating P/L {item['floating_pnl']:.2f}{notional}."
        )
    return "\n".join(lines)


def _format_behavior(snapshot: dict) -> str:
    names = {
        "high_trade_frequency": "high close frequency",
        "loss_streak": "consecutive losses",
        "rapid_reentry_after_loss": "rapid re-entry after a loss",
        "size_increased_after_loss": "larger size after a loss",
    }
    flags = [names[flag] for flag in snapshot["flags"]]
    pattern_text = ", ".join(flags) if flags else "no configured behavior patterns detected"
    return (
        f"In the last {snapshot['period_hours']} hours: {snapshot['closed_positions']} positions closed; "
        f"{snapshot['closed_last_hour']} closed in the last hour; "
        f"current consecutive-loss count {snapshot['consecutive_losses']}. "
        f"Patterns: {pattern_text}. These statistics do not establish emotional intent."
    )


def tradingview_url(symbol: str) -> str:
    """Build a TradingView forex chart URL without executing browser code."""
    symbol = (symbol or "EURUSD").strip().upper().replace("/", "")
    if not symbol.isalnum() or len(symbol) not in (6, 7):
        raise ValueError("Use a forex symbol such as EURUSD or GBPJPY.")
    return f"https://www.tradingview.com/chart/?symbol=OANDA%3A{quote(symbol)}"


def trading_action(parameters: dict, player=None) -> str:
    action = str(parameters.get("action", "summary")).strip().lower()
    if action == "open_chart":
        symbol = str(parameters.get("symbol", "EURUSD"))
        url = tradingview_url(symbol)
        webbrowser.open(url)
        return f"Opened the TradingView chart for {symbol.upper()}, sir."

    if action == "news":
        query = str(parameters.get("query", "")).strip()
        articles = _news_results(query)
        if not articles:
            return "No current market headlines were available from the news search."
        lines = []
        for article in articles:
            title = str(article.get("title", "")).strip()
            if not title:
                continue
            source = str(article.get("source", "")).strip()
            body = str(article.get("snippet", "")).strip()
            text = f"{title}" + (f" [{source}]" if source else "")
            if any(term in f"{title} {body}".lower() for term in _HIGH_IMPACT_TERMS):
                text = "[HIGH-IMPACT KEYWORD MATCH] " + text
            lines.append(text)
            if body:
                lines.append(f"  {body[:180]}")
            if article.get("url"):
                lines.append(f"  {article['url']}")
        return "\n".join(lines) or "No titled market headlines were found."

    try:
        from memory.config_manager import get_mt5_account_ids

        requested_account = str(parameters.get("account_id", "")).strip()
        account_ids = (
            [requested_account] if requested_account else get_mt5_account_ids()
        )
        if action == "market":
            return _format_market(market_snapshot(str(parameters.get("symbol", "EURUSD"))))
        if action == "account":
            reports = []
            for account_id in account_ids:
                try:
                    reports.append(_format_account(account_snapshot(account_id)))
                except Exception as exc:
                    reports.append(f"Account {account_id} unavailable: {exc}")
            if not account_ids:
                reports.append(_format_account(account_snapshot()))
            return "\n\n".join(reports)
        if action == "behavior":
            reports = []
            for account_id in account_ids:
                try:
                    reports.append(
                        f"Account {account_id}: "
                        f"{_format_behavior(behavior_snapshot(int(parameters.get('hours', 24)), account_id))}"
                    )
                except Exception as exc:
                    reports.append(f"Account {account_id} unavailable: {exc}")
            if not account_ids:
                reports.append(
                    _format_behavior(behavior_snapshot(int(parameters.get("hours", 24))))
                )
            return "\n\n".join(reports)
        if action == "risk":
            reports = []
            for identifier in account_ids:
                try:
                    snapshot = _with_mt5(
                        lambda mt5, account=identifier: _monitor_snapshot(mt5, account),
                        identifier,
                    )
                except Exception as exc:
                    reports.append(f"Account {identifier} unavailable: {exc}")
                    continue
                lines = [_format_account(snapshot["account"])]
                lines.extend(
                    _format_market(market) for market in snapshot["markets"]
                    if "error" not in market
                )
                lines.append(_format_behavior(snapshot["behavior"]))
                alerts = _risk_alerts(snapshot)
                lines.append(
                    "Risk flags: " + (
                        "; ".join(alerts)
                        if alerts else "none of the configured alert thresholds are currently crossed."
                    )
                )
                reports.append("\n".join(lines))
            if not account_ids:
                snapshot = _with_mt5(_monitor_snapshot)
                reports.append(_format_account(snapshot["account"]))
            return "\n\n".join(reports)
        months = int(parameters.get("months", 12))
        report = monthly_history(months, requested_account)
        reports = report.get("accounts") or [report]
    except Exception as exc:
        return f"Trading data is unavailable: {exc}"
    summaries = []
    for report in reports:
        if not report["rows"]:
            summaries.append(
                f"{report.get('account_id', 'MT5')} has no closed trades in that period."
            )
            continue
        latest = report["rows"][-1]
        summaries.append(
            f"{report.get('account_id', 'MT5')} shows {latest['trades']} trades "
            f"in {latest['month']}: made {latest['made']:.2f}, "
            f"lost {latest['lost']:.2f}, net {latest['net']:.2f} "
            f"{report['currency']}."
        )
    return "\n".join(summaries)


TOOL = {
    "name": "trading_monitor",
    "description": (
        "Read-only market intelligence from the connected MetaTrader 5 terminal. "
        "Actions: market for live price, five-minute trend, spread and volatility; "
        "news for current global financial headlines with high-impact keyword triage; "
        "account for balance, equity, margin and open-position exposure across all "
        "configured accounts; risk for per-account market and alert snapshots; behavior "
        "for objective trade frequency, loss-streak, rapid-reentry and size-change "
        "patterns across configured accounts; summary "
        "for monthly closed-trade history; open_chart for a TradingView chart. "
        "This tool is strictly read-only and never places or modifies trades."
    ),
    "parameters": {
        "type": "OBJECT",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["market", "news", "account", "risk", "behavior", "summary", "open_chart"],
                "description": "Which read-only market intelligence view to return.",
            },
            "months": {
                "type": "INTEGER",
                "description": "How many recent months to inspect for summary (1-120).",
            },
            "symbol": {
                "type": "STRING",
                "description": "Broker symbol for market analysis or TradingView, for example EURUSD.",
            },
            "query": {
                "type": "STRING",
                "description": "Optional topic or instrument to search in market news.",
            },
            "hours": {
                "type": "INTEGER",
                "description": "Trade behavior lookback window in hours, 1-168.",
            },
            "account_id": {
                "type": "STRING",
                "description": (
                    "Optional configured account identifier (login@server); omit "
                    "to review all configured accounts."
                ),
            },
        },
    },
    "handler": trading_action,
}