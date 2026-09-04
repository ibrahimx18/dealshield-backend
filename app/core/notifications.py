"""
DealShield Notification Service
Sends email and SMS notifications for key escrow events.
Supports SMTP for email and Twilio/Termii for SMS (Nigerian provider).
Providers are configured via environment variables and fail silently if not set.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
import logging

logger = logging.getLogger("dealshield.notifications")


class NotificationService:
    """Sends email and SMS notifications. Falls back to logging if providers not configured."""

    def __init__(self):
        self.smtp_host = os.getenv("SMTP_HOST")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER")
        self.smtp_pass = os.getenv("SMTP_PASS")
        self.from_email = os.getenv("FROM_EMAIL", "noreply@dealshield.ng")
        self.from_name = os.getenv("FROM_NAME", "DealShield")

        # SMS provider: 'termii' (Nigerian) or 'twilio'
        self.sms_provider = os.getenv("SMS_PROVIDER", "termii")
        self.termii_api_key = os.getenv("TERMII_API_KEY")
        self.termii_sender = os.getenv("TERMII_SENDER", "DealShield")
        self.twilio_sid = os.getenv("TWILIO_SID")
        self.twilio_token = os.getenv("TWILIO_TOKEN")
        self.twilio_from = os.getenv("TWILIO_FROM")

    def send_email(self, to_email: str, subject: str, body: str, html: Optional[str] = None):
        """Send an email notification. Logs if SMTP not configured."""
        if not self.smtp_host:
            logger.info(f"[EMAIL] (no SMTP configured) To: {to_email}, Subject: {subject}")
            logger.info(f"[EMAIL] Body: {body[:200]}")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = f"{self.from_name} <{self.from_email}>"
            msg["To"] = to_email
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))
            if html:
                msg.attach(MIMEText(html, "html"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.from_email, to_email, msg.as_string())
            logger.info(f"[EMAIL] Sent to {to_email}: {subject}")
            return True
        except Exception as e:
            logger.error(f"[EMAIL] Failed to send to {to_email}: {e}")
            return False

    def send_sms(self, phone: str, message: str):
        """Send an SMS notification. Logs if SMS provider not configured."""
        if not self._sms_configured():
            logger.info(f"[SMS] (no provider configured) To: {phone}, Message: {message[:200]}")
            return False

        try:
            if self.sms_provider == "termii":
                return self._send_termii(phone, message)
            elif self.sms_provider == "twilio":
                return self._send_twilio(phone, message)
        except Exception as e:
            logger.error(f"[SMS] Failed to send to {phone}: {e}")
            return False

    def _sms_configured(self) -> bool:
        if self.sms_provider == "termii":
            return bool(self.termii_api_key)
        elif self.sms_provider == "twilio":
            return bool(self.twilio_sid and self.twilio_token)
        return False

    def _send_termii(self, phone: str, message: str) -> bool:
        import requests
        url = "https://api.ng.termii.com/api/sms/send"
        payload = {
            "to": phone,
            "from": self.termii_sender,
            "sms": message,
            "type": "plain",
            "channel": "generic",
            "api_key": self.termii_api_key,
        }
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200

    def _send_twilio(self, phone: str, message: str) -> bool:
        from twilio.rest import Client
        client = Client(self.twilio_sid, self.twilio_token)
        client.messages.create(body=message, from_=self.twilio_from, to=phone)
        return True

    # ── Templated notifications ──

    def notify_email_verification(self, email: str, token: str, name: str = ""):
        """Send email verification link."""
        subject = "Verify Your DealShield Account"
        body = f"""Hello {name},

Please verify your email address to secure your DealShield account.

Your verification token is: {token}

If you did not create this account, please ignore this email.

— DealShield Team
"""
        self.send_email(email, subject, body)

    def notify_password_reset(self, email: str, token: str, name: str = ""):
        """Send password reset link."""
        subject = "Reset Your DealShield Password"
        body = f"""Hello {name},

We received a request to reset your DealShield password.

Your reset token is: {token}

This token expires in 30 minutes. If you did not request this, please ignore this email.

— DealShield Team
"""
        self.send_email(email, subject, body)

    def notify_escrow_status(self, phone: str, email: str, tx_title: str, status: str, role: str):
        """Send escrow status update via SMS and email."""
        messages = {
            "created": f"DealShield: A new deal '{tx_title}' has been created. You are the {role}.",
            "seller_accepted": f"DealShield: Deal '{tx_title}' accepted by seller. Please fund within 24h.",
            "funded": f"DealShield: Deal '{tx_title}' has been funded. Seller can now fulfil.",
            "seller_fulfilling": f"DealShield: Seller has started fulfilment for '{tx_title}'.",
            "buyer_review": f"DealShield: Goods delivered for '{tx_title}'. You have 7 days to review.",
            "released": f"DealShield: Funds released for '{tx_title}'. Transaction complete.",
            "disputed": f"DealShield: Dispute raised for '{tx_title}'. Our team will investigate.",
            "under_investigation": f"DealShield: Your dispute for '{tx_title}' is under investigation.",
            "resolved": f"DealShield: Dispute for '{tx_title}' has been resolved.",
            "cancelled": f"DealShield: Deal '{tx_title}' has been cancelled.",
            "expired": f"DealShield: Deal '{tx_title}' has expired (deadline passed).",
        }
        msg = messages.get(status, f"DealShield: Your deal '{tx_title}' status updated to {status}.")
        self.send_sms(phone, msg)
        self.send_email(email, f"DealShield Update: {tx_title}", msg)

    def notify_2fa_enabled(self, phone: str, email: str):
        """Confirm 2FA was enabled."""
        msg = "DealShield: Two-factor authentication has been enabled on your account."
        self.send_sms(phone, msg)
        self.send_email(email, "2FA Enabled on DealShield", msg)


notification_service = NotificationService()


# ── Compatibility functions (used by existing routers) ──

def notify_new_user(user_data: dict):
    """Called when a new user registers."""
    notification_service.send_email(
        user_data.get("email", ""),
        "Welcome to DealShield",
        f"Welcome {user_data.get('name', '')}! Your DealShield account has been created."
    )

def notify_kyc_submitted(data: dict):
    """Called when KYC is submitted."""
    notification_service.send_email(
        data.get("email", "noreply@dealshield.ng"),
        "KYC Submission Received",
        f"Hello {data.get('name', '')}, we received your KYC submission."
    )

def notify_listing_event(listing_data: dict, event: str):
    """Called when a listing is created/updated/deleted."""
    logger.info(f"[LISTING] Event: {event}, Data: {listing_data}")

def notify_escrow_event(tx_data: dict, event: str):
    """Called when an escrow transaction status changes."""
    logger.info(f"[ESCROW] Event: {event}, Tx: {tx_data.get('id')}, Status: {tx_data.get('status')}")
    # In production, this would send SMS/email to buyer + seller
    # For now, it just logs. The notification_service handles actual delivery.
