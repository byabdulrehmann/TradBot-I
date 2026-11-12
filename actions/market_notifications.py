"""Optional daily market-update delivery through explicitly configured channels."""
from __future__ import annotations

from email.message import EmailMessage
from html import escape
import os
import smtplib

import requests


def configured_channels() -> list[str]:
    """Return delivery channels with all required local settings present."""
    sid = os.getenv("JARVIS_TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("JARVIS_TWILIO_AUTH_TOKEN", "").strip()
    channels = []
    if sid and token and os.getenv("JARVIS_DAILY_WHATSAPP_TO") and os.getenv("JARVIS_TWILIO_WHATSAPP_FROM"):
        channels.append("whatsapp")
    if sid and token and os.getenv("JARVIS_DAILY_CALL_TO") and os.getenv("JARVIS_TWILIO_CALL_FROM"):
        channels.append("call")
    if (
        os.getenv("JARVIS_DAILY_EMAIL_TO")
        and os.getenv("JARVIS_SMTP_HOST")
        and os.getenv("JARVIS_SMTP_USERNAME")
        and os.getenv("JARVIS_SMTP_PASSWORD")
    ):
        channels.append("email")
    return channels


def _twilio_post(resource: str, data: dict[str, str]) -> None:
    sid = os.environ["JARVIS_TWILIO_ACCOUNT_SID"].strip()
    token = os.environ["JARVIS_TWILIO_AUTH_TOKEN"].strip()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/{resource}.json"
    response = requests.post(url, data=data, auth=(sid, token), timeout=15)
    response.raise_for_status()


def deliver_daily_update(report: str) -> dict[str, str]:
    """Send only through channels explicitly configured in the local environment."""
    outcomes: dict[str, str] = {}
    body = report.strip()[:1500]

    if "whatsapp" in configured_channels():
        try:
            _twilio_post("Messages", {
                "From": os.environ["JARVIS_TWILIO_WHATSAPP_FROM"].strip(),
                "To": os.environ["JARVIS_DAILY_WHATSAPP_TO"].strip(),
                "Body": body,
            })
            outcomes["whatsapp"] = "sent"
        except Exception as exc:
            outcomes["whatsapp"] = f"failed: {exc}"

    if "call" in configured_channels():
        try:
            spoken = escape(report.strip()[:1000])
            _twilio_post("Calls", {
                "From": os.environ["JARVIS_TWILIO_CALL_FROM"].strip(),
                "To": os.environ["JARVIS_DAILY_CALL_TO"].strip(),
                "Twiml": f'<Response><Say>{spoken}</Say></Response>',
            })
            outcomes["call"] = "started"
        except Exception as exc:
            outcomes["call"] = f"failed: {exc}"

    if "email" in configured_channels():
        try:
            message = EmailMessage()
            message["Subject"] = "JARVIS Daily Market Update"
            message["From"] = os.getenv("JARVIS_EMAIL_FROM", os.environ["JARVIS_SMTP_USERNAME"])
            message["To"] = os.environ["JARVIS_DAILY_EMAIL_TO"].strip()
            message.set_content(report.strip())
            host = os.environ["JARVIS_SMTP_HOST"].strip()
            port = int(os.getenv("JARVIS_SMTP_PORT", "587"))
            with smtplib.SMTP_SSL(host, port, timeout=15) if port == 465 else smtplib.SMTP(host, port, timeout=15) as server:
                if port != 465:
                    server.starttls()
                server.login(
                    os.environ["JARVIS_SMTP_USERNAME"].strip(),
                    os.environ["JARVIS_SMTP_PASSWORD"],
                )
                server.send_message(message)
            outcomes["email"] = "sent"
        except Exception as exc:
            outcomes["email"] = f"failed: {exc}"

    return outcomes


def deliver_market_alert(update: dict[str, str]) -> dict[str, str]:
    """Deliver one high-impact alert via configured WhatsApp; PSTN call is separately opt-in."""
    outcomes: dict[str, str] = {}
    title = str(update.get("title", "")).strip()
    source = str(update.get("source", "")).strip()
    terms = str(update.get("impact_terms", "")).strip()
    if not title:
        return outcomes

    if "whatsapp" in configured_channels():
        try:
            body = f"JARVIS HIGH-IMPACT GOLD/USD UPDATE\n{title}"
            if source:
                body += f"\nSource: {source}"
            if terms:
                body += f"\nWhy flagged: {terms}"
            body += "\nPlease verify the event with a primary news or economic-calendar source."
            _twilio_post("Messages", {
                "From": os.environ["JARVIS_TWILIO_WHATSAPP_FROM"].strip(),
                "To": os.environ["JARVIS_DAILY_WHATSAPP_TO"].strip(),
                "Body": body[:1200],
            })
            outcomes["whatsapp_message"] = "sent"
        except Exception as exc:
            outcomes["whatsapp_message"] = f"failed: {exc}"

    if (
        os.getenv("JARVIS_MARKET_ALERT_CALLS", "").strip().lower() in {"1", "true", "yes", "on"}
        and "call" in configured_channels()
    ):
        try:
            spoken = escape(f"High impact gold and dollar market update. {title}. {source}")
            _twilio_post("Calls", {
                "From": os.environ["JARVIS_TWILIO_CALL_FROM"].strip(),
                "To": os.environ["JARVIS_DAILY_CALL_TO"].strip(),
                "Twiml": f'<Response><Say>{spoken}</Say></Response>',
            })
            outcomes["phone_call"] = "started"
        except Exception as exc:
            outcomes["phone_call"] = f"failed: {exc}"

    return outcomes