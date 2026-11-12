import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import keyring.errors

from actions import trader_coach, trading_monitor
from memory import config_manager, trading_journal


class _MemoryVault:
    errors = keyring.errors
    priority = 1

    def __init__(self):
        self.values = {}

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def get_password(self, service, username):
        return self.values.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.values:
            raise self.errors.PasswordDeleteError("missing")
        del self.values[(service, username)]


class TraderSetupTests(unittest.TestCase):
    def test_credentials_go_to_vault_and_profile_stays_in_config(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_file = root / "config" / "api_keys.json"
            vault = _MemoryVault()
            with (
                patch.object(config_manager, "CONFIG_FILE", config_file),
                patch.object(config_manager, "CONFIG_DIR", config_file.parent),
                patch.object(config_manager, "_keyring", return_value=vault),
            ):
                config_manager.save_trader_setup(
                    "gemini-secret",
                    "windows",
                    {
                        "strategy": "London breakout",
                        "risk_per_trade_pct": 1.0,
                        "daily_loss_limit_pct": 3.0,
                        "max_trades_per_day": 5,
                    },
                    "123456",
                    "Broker-Demo",
                    "investor-secret",
                )
                self.assertTrue(
                    config_manager.is_first_setup_introduction_pending()
                )
                saved = json.loads(config_file.read_text(encoding="utf-8"))
                self.assertNotIn("gemini_api_key", saved)
                self.assertNotIn("mt5_investor_password", saved)
                self.assertEqual(config_manager.get_gemini_key(), "gemini-secret")
                self.assertEqual(
                    config_manager.get_mt5_credentials(),
                    {
                        "login": 123456,
                        "server": "Broker-Demo",
                        "password": "investor-secret",
                    },
                )
                config_manager.mark_first_setup_introduction_sent()
                self.assertFalse(
                    config_manager.is_first_setup_introduction_pending()
                )
                config_manager.save_trader_setup(
                    "gemini-secret",
                    "windows",
                    {
                        "strategy": "London breakout",
                        "risk_per_trade_pct": 1.0,
                        "daily_loss_limit_pct": 3.0,
                        "max_trades_per_day": 5,
                    },
                    "123456",
                    "Broker-Demo",
                    "investor-secret",
                )
                self.assertFalse(
                    config_manager.is_first_setup_introduction_pending()
                )

    def test_existing_setup_does_not_get_a_repeated_introduction(self):
        with TemporaryDirectory() as temp:
            config_file = Path(temp) / "config" / "api_keys.json"
            config_file.parent.mkdir()
            config_file.write_text(
                json.dumps({"trader_profile_setup_complete": True}),
                encoding="utf-8",
            )
            with (
                patch.object(config_manager, "CONFIG_FILE", config_file),
                patch.object(config_manager, "CONFIG_DIR", config_file.parent),
            ):
                self.assertFalse(
                    config_manager.is_first_setup_introduction_pending()
                )

    def test_partial_mt5_credentials_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "account number, server"):
            config_manager.save_trader_setup(
                "gemini-secret", "windows", {"strategy": "scalping"},
                "123", "", "investor-secret",
            )

    def test_multiple_accounts_and_updated_plan_are_persisted_safely(self):
        with TemporaryDirectory() as temp:
            config_file = Path(temp) / "config" / "api_keys.json"
            vault = _MemoryVault()
            with (
                patch.object(config_manager, "CONFIG_FILE", config_file),
                patch.object(config_manager, "CONFIG_DIR", config_file.parent),
                patch.object(config_manager, "_keyring", return_value=vault),
            ):
                config_manager.save_trader_setup(
                    "gemini-secret", "windows",
                    {"strategy": "breakout", "daily_loss_limit_pct": 3},
                )
                config_manager.save_mt5_accounts([
                    {
                        "name": "Funded",
                        "login": "123",
                        "server": "Broker-A",
                        "password": "investor-a",
                    },
                    {
                        "name": "Personal",
                        "login": "456",
                        "server": "Broker-B",
                        "password": "investor-b",
                    },
                ], "456@Broker-B")
                config_manager.save_trader_profile({
                    "strategy": "London breakout",
                    "daily_loss_limit_pct": 2.0,
                })

                saved = json.loads(config_file.read_text(encoding="utf-8"))
                self.assertEqual(
                    [account["id"] for account in config_manager.get_mt5_accounts()],
                    ["123@Broker-A", "456@Broker-B"],
                )
                self.assertEqual(config_manager.get_mt5_account_id(), "456@Broker-B")
                self.assertEqual(
                    config_manager.get_mt5_credentials_for_account("123@Broker-A"),
                    {"login": 123, "server": "Broker-A", "password": "investor-a"},
                )
                self.assertEqual(
                    config_manager.get_trader_profile()["daily_loss_limit_pct"], 2.0
                )
                self.assertNotIn("password", json.dumps(saved).lower())
                self.assertEqual(
                    vault.get_password("TradBot-I", "mt5:456:Broker-B"),
                    "investor-b",
                )

    def test_unknown_account_is_not_replaced_with_primary(self):
        with TemporaryDirectory() as temp:
            config_file = Path(temp) / "config" / "api_keys.json"
            vault = _MemoryVault()
            with (
                patch.object(config_manager, "CONFIG_FILE", config_file),
                patch.object(config_manager, "CONFIG_DIR", config_file.parent),
                patch.object(config_manager, "_keyring", return_value=vault),
            ):
                config_manager.save_trader_setup(
                    "gemini-secret", "windows", {"strategy": "breakout"},
                    "123", "Broker-A", "investor-a",
                )
                with self.assertRaisesRegex(ValueError, "Unknown MT5 account"):
                    config_manager.get_mt5_credentials_for_account("missing")

    def test_live_price_symbols_are_normalized_and_persisted(self):
        with TemporaryDirectory() as temp:
            config_file = Path(temp) / "config" / "api_keys.json"
            with (
                patch.object(config_manager, "CONFIG_FILE", config_file),
                patch.object(config_manager, "CONFIG_DIR", config_file.parent),
            ):
                self.assertEqual(
                    config_manager.get_watch_symbols(),
                    ["XAUUSD", "BTCUSD", "GBPUSD"],
                )
                config_manager.save_watch_symbols(
                    [" eurusd ", "EURUSD", "GBPJPY.a", "xauusd"]
                )
                self.assertEqual(
                    config_manager.get_watch_symbols(),
                    ["EURUSD", "GBPJPY.A", "XAUUSD"],
                )
                saved = json.loads(config_file.read_text(encoding="utf-8"))
                self.assertEqual(
                    saved["watch_symbols"], ["EURUSD", "GBPJPY.A", "XAUUSD"]
                )

    def test_live_price_symbols_reject_invalid_or_empty_lists(self):
        with self.assertRaisesRegex(ValueError, "Invalid market symbol"):
            config_manager.save_watch_symbols(["EUR/USD"])
        with self.assertRaisesRegex(ValueError, "at least one"):
            config_manager.save_watch_symbols([])


class TradeJournalTests(unittest.TestCase):
    def test_history_sync_start_is_incremental_after_initial_import(self):
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "journal.sqlite3"
            initial = trading_journal.history_sync_start("123@Broker", db_path=db_path)
            self.assertEqual(initial, datetime(2000, 1, 1, tzinfo=timezone.utc))
            trading_journal.mark_history_synced("123@Broker", db_path=db_path)
            next_start = trading_journal.history_sync_start("123@Broker", db_path=db_path)
            self.assertLess(next_start, datetime.now(timezone.utc))
            self.assertGreater(next_start, initial)

    def test_history_upsert_and_reflections_are_persistent(self):
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "journal.sqlite3"
            now = datetime.now(timezone.utc)
            closed = (now - timedelta(minutes=3)).isoformat()
            opened_after_loss = (now - timedelta(minutes=2)).isoformat()
            trading_journal.save_mt5_trades(
                "123@Broker",
                [
                    {
                        "ticket": "1", "symbol": "EURUSD", "side": "buy",
                        "opened_at": (now - timedelta(minutes=5)).isoformat(),
                        "closed_at": closed, "volume": 0.1, "net": -10.0,
                    },
                    {
                        "ticket": "2", "symbol": "EURUSD", "side": "sell",
                        "opened_at": opened_after_loss,
                        "closed_at": now.isoformat(), "volume": 0.2, "net": 5.0,
                    },
                ],
                db_path=db_path,
            )
            trading_journal.save_mt5_trades(
                "123@Broker",
                [{
                    "ticket": "1", "symbol": "EURUSD", "side": "buy",
                    "opened_at": (now - timedelta(minutes=5)).isoformat(),
                    "closed_at": closed, "volume": 0.1, "net": -10.0,
                }],
                db_path=db_path,
            )
            trading_journal.save_reflection(
                "123@Broker", "post_trade", "EURUSD",
                emotional_state="rushed", plan_adherence="no",
                lesson="wait for confirmation", ticket="1", db_path=db_path,
            )

            summary = trading_journal.history_summary(
                account_id="123@Broker", db_path=db_path
            )
            self.assertEqual(summary["trade_count"], 2)
            self.assertEqual(summary["net"], -5.0)
            self.assertEqual(summary["rapid_reentries_after_loss"], 1)
            self.assertEqual(summary["reflections"][0]["emotional_state"], "rushed")

    def test_reflection_requires_valid_category_and_symbol(self):
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "category"):
                trading_journal.save_reflection(
                    "123", "diagnosis", "EURUSD",
                    db_path=Path(temp) / "journal.sqlite3",
                )

    def test_daily_checkin_merges_answers_and_completes_when_all_are_saved(self):
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "journal.sqlite3"
            first = trading_journal.save_daily_checkin(
                "2026-10-22",
                {"trading_mood": "steady", "market_bias": "neutral"},
                db_path=db_path,
            )
            self.assertFalse(first["complete"])
            self.assertIn("intended_direction", first["missing"])

            all_answers = {
                field: f"self-report: {field}"
                for field in trading_journal.DAILY_CHECKIN_FIELDS
            }
            result = trading_journal.save_daily_checkin(
                "2026-10-22", all_answers, db_path=db_path
            )
            self.assertTrue(result["complete"])
            self.assertEqual(result["answers"]["trading_mood"], "self-report: trading_mood")
            self.assertEqual(
                trading_journal.get_daily_checkin("2026-10-22", db_path=db_path)["answers"],
                all_answers,
            )
            self.assertEqual(
                trading_journal.get_recent_daily_checkins(db_path=db_path)[0]["checkin_date"],
                "2026-10-22",
            )

    def test_daily_checkin_rejects_invalid_dates_and_ignores_unknown_fields(self):
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "journal.sqlite3"
            with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                trading_journal.save_daily_checkin("today", {}, db_path=db_path)
            result = trading_journal.save_daily_checkin(
                "2026-10-22",
                {"trading_mood": "steady", "made_up_field": "not saved"},
                db_path=db_path,
            )
            self.assertEqual(result["answers"], {"trading_mood": "steady"})

    def test_trading_coach_records_daily_checkin(self):
        answers = {"trading_mood": "calm", "confidence": "steady"}
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "journal.sqlite3"
            with (
                patch.object(trading_journal, "JOURNAL_PATH", db_path),
                patch.object(config_manager, "get_mt5_account_id", return_value=""),
            ):
                response = trader_coach.trading_coach({
                    "action": "record_daily_checkin",
                    "checkin_date": "2026-10-22",
                    "answers": answers,
                })
                saved = trading_journal.get_daily_checkin("2026-10-22")

        self.assertIn("2 daily check-in answers", response)
        self.assertEqual(saved["answers"], answers)


class MT5ReadOnlyTests(unittest.TestCase):
    def test_monitor_checks_configured_live_price_symbols(self):
        symbols = ["EURUSD", "GBPJPY"]
        with (
            patch("memory.config_manager.get_mt5_accounts", return_value=[]),
            patch("memory.config_manager.get_mt5_account_id", return_value=""),
            patch("memory.config_manager.get_watch_symbols", return_value=symbols),
            patch.object(
                trading_monitor,
                "_public_market_snapshot",
                side_effect=lambda symbol: {
                    "symbol": symbol,
                    "watch_symbol": symbol,
                    "source": "test",
                },
            ),
        ):
            result = trading_monitor.monitoring_check()

        self.assertEqual(
            [market["watch_symbol"] for market in result["markets"]],
            symbols,
        )

    def test_monitoring_uses_selected_account_for_primary_market_data(self):
        accounts = [
            {"id": "123@Broker-A", "name": "Funded", "login": "123", "server": "Broker-A"},
            {"id": "456@Broker-B", "name": "Personal", "login": "456", "server": "Broker-B"},
        ]
        symbols = trading_monitor.WATCH_SYMBOLS

        def read_account(_reader, account_id=""):
            login, server = account_id.split("@", 1)
            return {
                "account": {"login": login, "server": server},
                "markets": [
                    {"symbol": symbol, "watch_symbol": symbol, "source": account_id}
                    for symbol in symbols
                ],
                "journal_error": "",
            }

        with (
            patch("memory.config_manager.get_mt5_accounts", return_value=accounts),
            patch("memory.config_manager.get_mt5_account_id", return_value="456@Broker-B"),
            patch("memory.config_manager.get_watch_symbols", return_value=list(symbols)),
            patch.object(trading_monitor, "_with_mt5", side_effect=read_account),
            patch.object(trading_monitor, "_risk_alerts", return_value=[]),
            patch.object(trading_monitor, "_trade_events", return_value=[]),
        ):
            result = trading_monitor.monitoring_check()

        self.assertEqual(result["snapshot"]["account"]["login"], "456")
        self.assertTrue(all(
            market["source"] == "456@Broker-B" for market in result["markets"]
        ))
        self.assertEqual(len(result["snapshots"]), 2)

    def test_monthly_history_reports_each_configured_account(self):
        accounts = [
            {"id": "123@Broker-A", "name": "Funded", "login": "123", "server": "Broker-A"},
            {"id": "456@Broker-B", "name": "Personal", "login": "456", "server": "Broker-B"},
        ]

        def read_account(_reader, account_id=""):
            return {
                "ok": True,
                "currency": "USD",
                "rows": [{"month": "2026-10", "trades": 2, "made": 5, "lost": 1, "net": 4}],
            }

        with (
            patch("memory.config_manager.get_mt5_accounts", return_value=accounts),
            patch.object(trading_monitor, "_with_mt5", side_effect=read_account),
        ):
            result = trading_monitor.monthly_history(months=1)

        self.assertEqual(
            [account["account_id"] for account in result["accounts"]],
            ["123@Broker-A", "456@Broker-B"],
        )
        self.assertEqual(
            [account["account_name"] for account in result["accounts"]],
            ["Funded", "Personal"],
        )

    def test_connection_uses_configured_investor_credentials(self):
        mt5 = SimpleNamespace(
            initialize=lambda **kwargs: kwargs == {
                "login": 123, "password": "investor", "server": "Broker-Demo"
            },
            account_info=lambda: SimpleNamespace(login=123, trade_allowed=False),
            shutdown=lambda: None,
        )
        with (
            patch("memory.config_manager.get_mt5_credentials", return_value={
                "login": 123, "password": "investor", "server": "Broker-Demo"
            }),
            patch.object(trading_monitor, "_load_mt5", return_value=mt5),
        ):
            self.assertEqual(trading_monitor._with_mt5(lambda client: "read-only"), "read-only")

    def test_connection_rejects_a_login_with_trading_permission(self):
        mt5 = SimpleNamespace(
            initialize=lambda **kwargs: True,
            account_info=lambda: SimpleNamespace(login=123, trade_allowed=True),
            shutdown=lambda: None,
        )
        with (
            patch("memory.config_manager.get_mt5_credentials", return_value={
                "login": 123, "password": "not-used", "server": "Broker-Demo"
            }),
            patch.object(trading_monitor, "_load_mt5", return_value=mt5),
            self.assertRaisesRegex(RuntimeError, "trading permission"),
        ):
            trading_monitor._with_mt5(lambda client: "must not run")

    def test_closed_trade_aggregation_includes_costs_and_position_id(self):
        mt5 = SimpleNamespace(
            DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=3,
            DEAL_ENTRY_INOUT=2, DEAL_TYPE_BUY=0,
        )
        opened = int(datetime.now(timezone.utc).timestamp()) - 120
        closed = opened + 60
        rows = trading_monitor._group_closed_trades(mt5, [
            SimpleNamespace(
                position_id=77, ticket=101, symbol="EURUSD", type=0,
                entry=0, time=opened, volume=0.2, profit=0, swap=0,
                commission=0, fee=0,
            ),
            SimpleNamespace(
                position_id=77, ticket=102, symbol="EURUSD", type=1,
                entry=1, time=closed, volume=0.2, profit=12, swap=-1,
                commission=-0.5, fee=0,
            ),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticket"], "77")
        self.assertEqual(rows[0]["net"], 10.5)
        self.assertEqual(rows[0]["side"], "buy")

    def test_trade_events_skip_existing_positions_then_detect_changes(self):
        trading_monitor._TRADE_EVENT_STATE.clear()
        try:
            account = {"login": "123", "server": "Broker", "positions": []}
            behavior = {"closed_trades": []}
            self.assertEqual(
                trading_monitor._trade_events({"account": account, "behavior": behavior}),
                [],
            )
            account["positions"] = [{
                "ticket": "88", "symbol": "GBPUSD",
                "direction": "long", "lots": 0.1,
            }]
            opened = trading_monitor._trade_events(
                {"account": account, "behavior": behavior}
            )
            self.assertEqual(opened[0]["event"], "opened")
            behavior["closed_trades"] = [{
                "ticket": "77", "symbol": "EURUSD", "side": "buy",
                "volume": 0.2, "net": -4.0,
            }]
            closed = trading_monitor._trade_events(
                {"account": account, "behavior": behavior}
            )
            self.assertEqual(closed[0]["event"], "closed")
            self.assertEqual(closed[0]["ticket"], "77")
            self.assertEqual(closed[0]["account_id"], "123@Broker")
        finally:
            trading_monitor._TRADE_EVENT_STATE.clear()

    def test_trade_events_track_each_account_without_cross_account_reset(self):
        trading_monitor._TRADE_EVENT_STATE.clear()
        try:
            first = {
                "account": {"login": "123", "server": "Broker-A", "positions": []},
                "behavior": {"closed_trades": []},
            }
            second = {
                "account": {"login": "456", "server": "Broker-B", "positions": []},
                "behavior": {"closed_trades": []},
            }
            self.assertEqual(trading_monitor._trade_events(first), [])
            self.assertEqual(trading_monitor._trade_events(second), [])
            first["account"]["positions"] = [{
                "ticket": "1", "symbol": "EURUSD", "direction": "long", "lots": 0.1,
            }]
            self.assertEqual(trading_monitor._trade_events(first)[0]["account_id"], "123@Broker-A")
            second["account"]["positions"] = [{
                "ticket": "2", "symbol": "GBPUSD", "direction": "short", "lots": 0.2,
            }]
            self.assertEqual(trading_monitor._trade_events(second)[0]["account_id"], "456@Broker-B")
        finally:
            trading_monitor._TRADE_EVENT_STATE.clear()

    def test_personal_trade_limits_are_reported_from_profile(self):
        snapshot = {
            "account": {"balance": 100.0, "margin": 0.0, "margin_level_percent": 0.0},
            "journal": {"closed_trades": 5, "realized_loss": 4.0},
            "markets": [],
            "behavior": {
                "flags": [], "closed_last_hour": 0, "consecutive_losses": 0,
            },
        }
        with patch(
            "memory.config_manager.get_trader_profile",
            return_value={"max_trades_per_day": 5, "daily_loss_limit_pct": 3.0},
        ):
            alerts = trading_monitor._risk_alerts(snapshot)
        self.assertTrue(any("daily maximum of 5" in alert for alert in alerts))
        self.assertTrue(any("daily limit of 3.00%" in alert for alert in alerts))


if __name__ == "__main__":
    unittest.main()
