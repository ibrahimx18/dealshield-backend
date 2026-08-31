"""
SafePay Notification System
Handles webhook triggers to n8n and Telegram notifications.
n8n picks up events via webhook and processes them.
"""
import os
import json
import logging
import requests
from typing import Optional

logger = logging.getLogger("safepay.notifications")

# ── Configuration ──
# n8n webhook base URL for SafePay events
N8N_WEBHOOK_BASE = os.getenv("N8N_WEBHOOK_BASE", "http://localhost:5678/webhook")
# No insecure fallback — app.main.validate_required_secrets() enforces this is set at startup.
SAFEPAY_WEBHOOK_SECRET = os.getenv("SAFEPAY_WEBHOOK_SECRET", "")

# Telegram bot token (for direct sends if n8n is down)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GERALT_TELEGRAM_CHAT_ID = os.getenv("GERALT_TELEGRAM_CHAT_ID", "1224185098")


def trigger_webhook(event_type: str, data: dict) -> bool:
    """
    Fire a webhook to n8n for a SafePay event.
    n8n will handle the actual notification routing (Telegram, email, etc.)
    """
    payload = {
        "event": event_type,
        "secret": SAFEPAY_WEBHOOK_SECRET,
        "data": data,
    }
    webhook_url = f"{N8N_WEBHOOK_BASE}/safepay-{event_type}"

    try:
        resp = requests.post(webhook_url, json=payload, timeout=5)
        if resp.status_code in (200, 201):
            logger.info(f"Webhook triggered: {event_type}")
            return True
        else:
            logger.warning(f"Webhook {event_type} returned {resp.status_code}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Webhook {event_type} failed: {e}")

    # Fallback: try direct Telegram message if n8n is down
    if TELEGRAM_BOT_TOKEN:
        _send_telegram_fallback(event_type, data)
    return False


def _send_telegram_fallback(event_type: str, data: dict):
    """Send a basic Telegram message directly if n8n is unavailable."""
    try:
        msg = _format_telegram_message(event_type, data)
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": GERALT_TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }, timeout=5)
    except Exception as e:
        logger.error(f"Telegram fallback failed: {e}")


def _format_telegram_message(event_type: str, data: dict) -> str:
    """Format a basic notification message for Telegram."""
    emoji_map = {
        "escrow_created": "🛡️",
        "escrow_shipped": "📦",
        "escrow_delivered": "✅",
        "escrow_disputed": "⚠️",
        "escrow_cancelled": "❌",
        "listing_created": "📝",
        "price_alert": "📈",
        "low_wallet": "💰",
        "new_user": "👤",
        "kyc_submitted": "🪪",
    }
    emoji = emoji_map.get(event_type, "🔔")

    lines = [f"{emoji} <b>SafePay: {event_type.replace('_', ' ').title()}</b>"]
    for key, val in data.items():
        lines.append(f"<b>{key}:</b> {val}")
    return "\n".join(lines)


# ── Event-specific helpers ──

def notify_escrow_event(tx_data: dict, event_type: str):
    """Notify about escrow status changes."""
    trigger_webhook(event_type, {
        "transaction_id": tx_data.get("id"),
        "listing_title": tx_data.get("listing_title"),
        "category": tx_data.get("category"),
        "amount": tx_data.get("amount"),
        "commission": tx_data.get("commission"),
        "status": tx_data.get("status"),
        "buyer_name": tx_data.get("buyer_name"),
        "seller_name": tx_data.get("seller_name"),
        "buyer_id": tx_data.get("buyer_id"),
        "seller_id": tx_data.get("seller_id"),
        "insured": tx_data.get("insured"),
        "insurance_fee": tx_data.get("insurance_fee", 0),
        "logistics_provider": tx_data.get("logistics_provider", ""),
        "tracking_number": tx_data.get("tracking_number", ""),
    })


def notify_listing_event(listing_data: dict):
    """Notify about new listing creation (for scam detection)."""
    trigger_webhook("listing_created", listing_data)


def notify_new_user(user_data: dict):
    """Notify about new user registration."""
    trigger_webhook("new_user", user_data)


def notify_kyc_submitted(user_data: dict):
    """Notify when a user submits KYC verification."""
    trigger_webhook("kyc_submitted", user_data)


def notify_low_wallet(user_data: dict):
    """Notify when user wallet balance is low."""
    trigger_webhook("low_wallet", user_data)
