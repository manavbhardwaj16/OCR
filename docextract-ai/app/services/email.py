"""Outbound email service.

Uses SMTP when ``SMTP_HOST`` is configured; otherwise logs the email content to
stdout (dev-mode fallback). Both functions never raise — email failures are
logged but do not break the signup or verification flow.
"""
from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("email")


def _smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from_email)


def _build_verification_message(to_email: str, token: str, base_url: str) -> EmailMessage:
    link = f"{base_url.rstrip('/')}/verify-email?token={token}"
    msg = EmailMessage()
    msg["Subject"] = "Verify your DocExtract AI account"
    msg["From"] = settings.smtp_from_email or "noreply@docextract.ai"
    msg["To"] = to_email
    msg.set_content(
        "Welcome to DocExtract AI!\n\n"
        f"Click the link below to verify your email and get your API key:\n{link}\n\n"
        "This link expires in 24 hours.\n"
        "If you did not sign up, ignore this email."
    )
    msg.add_alternative(
        f"""<!doctype html>
<html><body style="font-family:system-ui,-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:32px;color:#111">
  <h2 style="margin:0 0 16px">Welcome to DocExtract AI</h2>
  <p>Click the button below to verify your email and unlock your API key.</p>
  <p style="text-align:center;margin:24px 0">
    <a href="{link}" style="background:#111;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600">
      Verify email
    </a>
  </p>
  <p style="color:#666;font-size:13px">Or open this link:<br><a href="{link}">{link}</a></p>
  <p style="color:#999;font-size:12px;margin-top:24px">This link expires in 24 hours. If you did not sign up, ignore this email.</p>
</body></html>""",
        subtype="html",
    )
    return msg


def _build_welcome_message(to_email: str, company_name: str, api_key: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Your DocExtract AI API key"
    msg["From"] = settings.smtp_from_email or "noreply@docextract.ai"
    msg["To"] = to_email
    msg.set_content(
        f"Welcome {company_name}!\n\n"
        f"Your API key:\n{api_key}\n\n"
        "Save this key — you cannot retrieve it again. To rotate it, use the "
        "API Keys page in the dashboard.\n\n"
        "Quick start:\n"
        "  curl -X POST {url}/api/v1/extract \\\n"
        '    -H "X-API-Key: {key}" \\\n'
        '    -F "file=@invoice.pdf"\n'.format(
            url=settings.app_base_url, key=api_key
        )
    )
    return msg


def _send_sync(msg: EmailMessage) -> None:
    if not _smtp_configured():
        # Dev-mode fallback — log the entire email to stdout so devs can copy
        # the verification link out of the logs.
        log.info(
            "email_dev_mode_no_smtp_configured",
            subject=msg["Subject"],
            to=msg["To"],
            body=msg.get_content(),
        )
        return
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                # Server doesn't support STARTTLS — proceed plain
                pass
            if settings.smtp_user and settings.smtp_password:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
            log.info("email_sent", to=msg["To"], subject=msg["Subject"])
    except Exception as exc:
        log.warning(
            "email_send_failed",
            error=str(exc),
            to=msg["To"],
            subject=msg["Subject"],
        )


async def send_verification_email(
    to_email: str, token: str, base_url: str | None = None
) -> None:
    msg = _build_verification_message(
        to_email, token, base_url or settings.app_base_url
    )
    await asyncio.to_thread(_send_sync, msg)


async def send_welcome_email(
    to_email: str, company_name: str, api_key: str
) -> None:
    msg = _build_welcome_message(to_email, company_name, api_key)
    await asyncio.to_thread(_send_sync, msg)
