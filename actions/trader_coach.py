"""Local trade-history summaries and user-authored trading reflections."""
from __future__ import annotations

import json


def trading_coach(parameters: dict) -> str:
    from memory.config_manager import (
        get_mt5_account_id, get_mt5_account_ids, get_trader_profile,
    )
    from memory.trading_journal import (
        get_recent_daily_checkins,
        history_summary,
        save_daily_checkin,
        save_reflection,
    )

    action = str(parameters.get("action", "review")).strip().lower()
    if action == "record_daily_checkin":
        result = save_daily_checkin(
            checkin_date=str(parameters.get("checkin_date", "")),
            answers=parameters.get("answers", {}),
        )
        if result["complete"]:
            return (
                f"Saved all daily self-reported check-in answers for "
                f"{result['checkin_date']}."
            )
        return (
            f"Saved {len(result['answers'])} daily check-in answers for "
            f"{result['checkin_date']}; still missing: "
            f"{', '.join(result['missing'])}."
        )
    account_id = get_mt5_account_id(str(parameters.get("account_id", "")))
    if action == "record_reflection":
        reflection_id = save_reflection(
            account_id=account_id,
            category=str(parameters.get("category", "")),
            symbol=str(parameters.get("symbol", "")),
            setup=str(parameters.get("setup", "")),
            risk_pct=parameters.get("risk_pct"),
            emotional_state=str(parameters.get("emotional_state", "")),
            plan_adherence=str(parameters.get("plan_adherence", "")),
            lesson=str(parameters.get("lesson", "")),
            ticket=str(parameters.get("ticket", "")),
            db_path=None,
        )
        return f"Saved the user's trade reflection locally (record {reflection_id})."

    days = max(1, min(int(parameters.get("days", 365)), 3650))
    requested_account = str(parameters.get("account_id", "")).strip()
    account_ids = (
        [get_mt5_account_id(requested_account)]
        if requested_account else get_mt5_account_ids()
    )
    if not account_ids:
        account_ids = [account_id] if account_id else [""]
    summaries = [
        {
            "account_id": current_account,
            **history_summary(account_id=current_account, days=days),
        }
        for current_account in account_ids
    ]
    summary = next(
        (item for item in summaries if item["account_id"] == account_id),
        summaries[0],
    )
    profile = get_trader_profile()
    result = {
        "period_days": summary["period_days"],
        "closed_trades_recorded": summary["trade_count"],
        "wins": summary["wins"],
        "losses": summary["losses"],
        "net_result": summary["net"],
        "profit_factor": summary["profit_factor"],
        "current_loss_streak": summary["consecutive_losses"],
        "rapid_reentries_after_loss": summary["rapid_reentries_after_loss"],
        "trader_plan": profile,
        "recent_reflections": summary["reflections"][:8],
        "recent_daily_checkins": get_recent_daily_checkins(limit=8),
        "account_summaries": [
            {
                "account_id": item["account_id"],
                "closed_trades_recorded": item["trade_count"],
                "wins": item["wins"],
                "losses": item["losses"],
                "net_result": item["net"],
                "profit_factor": item["profit_factor"],
                "current_loss_streak": item["consecutive_losses"],
                "rapid_reentries_after_loss": item["rapid_reentries_after_loss"],
                "recent_reflections": item["reflections"][:8],
            }
            for item in summaries
        ],
        "interpretation_limit": (
            "These are recorded account outcomes and self-reported reflections, "
            "not a diagnosis or a prediction."
        ),
    }
    return json.dumps(result, ensure_ascii=False)


TOOL = {
    "name": "trading_coach",
    "description": (
        "Read the local MT5 trade journal and the trader's setup guardrails, or "
        "save a daily trader check-in or pre/post-trade reflection using only what "
        "the trader explicitly "
        "said. Use action=review when asked to study past behavior or align with "
        "the plan. Use action=record_reflection after the trader has described a "
        "setup, state, plan adherence, or lesson. Use action=record_daily_checkin "
        "after each answer in the scheduled daily questionnaire, passing the "
        "check-in date and all answers heard so far. Never infer emotions, diagnose, "
        "predict performance, or give a trade instruction. This tool cannot place "
        "or modify orders."
    ),
    "parameters": {
        "type": "OBJECT",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["review", "record_reflection", "record_daily_checkin"],
                "description": "Read the accumulated journal or save a reflection.",
            },
            "days": {
                "type": "INTEGER",
                "description": "History lookback in days for a review (1-3650).",
            },
            "account_id": {
                "type": "STRING",
                "description": (
                    "Optional configured MT5 account identifier (login@server); "
                    "omit to use the primary account."
                ),
            },
            "category": {
                "type": "STRING",
                "enum": ["pre_trade", "trade_open", "post_trade"],
                "description": "Reflection timing; required for record_reflection.",
            },
            "symbol": {
                "type": "STRING",
                "description": "Instrument named by the trader.",
            },
            "ticket": {
                "type": "STRING",
                "description": "Optional matching MT5 position ticket.",
            },
            "setup": {
                "type": "STRING",
                "description": "Setup rationale in the trader's own terms.",
            },
            "risk_pct": {
                "type": "NUMBER",
                "description": "Risk percentage explicitly stated by the trader, if any.",
            },
            "emotional_state": {
                "type": "STRING",
                "description": "Emotional state explicitly self-reported by the trader.",
            },
            "plan_adherence": {
                "type": "STRING",
                "description": "Whether the trader says the trade followed their plan.",
            },
            "lesson": {
                "type": "STRING",
                "description": "Lesson or intended adjustment explicitly stated by the trader.",
            },
            "checkin_date": {
                "type": "STRING",
                "description": "Local check-in date in YYYY-MM-DD format.",
            },
            "answers": {
                "type": "OBJECT",
                "description": (
                    "Trader's exact self-reported answers so far in today's check-in. "
                    "Include answers already saved when sending the next update."
                ),
                "properties": {
                    "trading_mood": {"type": "STRING"},
                    "market_bias": {"type": "STRING"},
                    "intended_direction": {"type": "STRING"},
                    "outside_mood": {"type": "STRING"},
                    "confidence": {"type": "STRING"},
                    "readiness": {"type": "STRING"},
                    "stand_aside_conditions": {"type": "STRING"},
                    "risk_commitment": {"type": "STRING"},
                },
            },
        },
    },
    "handler": trading_coach,
}
