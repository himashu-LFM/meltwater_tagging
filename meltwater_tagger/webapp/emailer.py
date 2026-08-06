"""Outbound email via SMTP (welcome message + password-reset code).

Provider-agnostic: set SMTP_HOST/PORT/USER/PASSWORD/FROM in the environment
(Gmail/Workspace with an App Password, SendGrid, etc.). If SMTP isn't configured,
sends become no-ops that log a warning — the app never crashes for lack of email.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import config
from logging_setup import get_logger

log = get_logger("emailer")


def is_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def _send(to_email: str, subject: str, text_body: str, html_body: str | None = None) -> bool:
    """Send one email. Returns True on success, False otherwise (never raises)."""
    if not is_configured():
        log.warning("email: SMTP not configured (SMTP_HOST/USER/PASSWORD) — skipping %r to %s",
                    subject, to_email)
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.SMTP_FROM or config.SMTP_USER
        msg["To"] = to_email
        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")

        ctx = ssl.create_default_context()
        if config.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=30) as s:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        log.info("email: sent %r to %s", subject, to_email)
        return True
    except Exception as e:
        log.exception("email: failed to send %r to %s: %s", subject, to_email, e)
        return False


def send_welcome_email(to_email: str, when_str: str) -> bool:
    """Welcome note sent right after a new account is created."""
    app_url = config.APP_BASE_URL
    subject = "🎉 Welcome to Sentiment Tagger — you're all set!"
    text = (
        f"🎉 You're in. Welcome aboard! 👋\n\n"
        f"Your Sentiment Tagger account is ready — time to tag social sentiment at the\n"
        f"speed of thought. ⚡\n\n"
        f"✨ ACCOUNT DETAILS\n"
        f"  📧 Email:           {to_email}\n"
        f"  🕒 Session started: {when_str}\n\n"
        f"WHAT YOU CAN DO\n"
        f"  ⚡ Parallel AI classification — not one post at a time\n"
        f"  🏷️  Any brand — Kaseya, Ninja, and whatever's next\n"
        f"  📊 Full run history — always a click away\n\n"
        f"🚀 Launch it → {app_url}\n\n"
        f"🔐 Didn't create this account? Nothing to do — just tell your admin and we'll\n"
        f"remove it right away.\n\n"
        f"— Sentiment Tagger · operated by ListenFirst Media"
    )
    html = f"""\
<div style="margin:0;padding:0;background:#0a0a0c;">
  <div style="max-width:500px;margin:0 auto;padding:32px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
    <div style="text-align:center;margin-bottom:22px;">
      <span style="font-size:13px;font-weight:700;letter-spacing:1.5px;color:#e9c46a;text-transform:uppercase;">◐&nbsp; Meltwater · Sentiment Tagger</span>
    </div>
    <div style="background:#141417;border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:40px 34px;box-shadow:0 20px 50px rgba(0,0,0,0.45);">
      <div style="font-size:36px;line-height:1;margin-bottom:14px;">🎉</div>
      <h1 style="margin:0 0 10px;font-size:26px;line-height:1.25;color:#ffffff;font-weight:800;letter-spacing:-0.4px;">
        You're in. <span style="color:#fdb913;">Welcome aboard!</span>&nbsp;👋
      </h1>
      <p style="margin:0 0 26px;font-size:15px;line-height:1.65;color:#b7b7bd;">
        Your <b style="color:#ffffff;">Sentiment Tagger</b> account is ready — time to tag social sentiment at the speed of thought.&nbsp;⚡
      </p>
      <div style="background:#1d1d21;border:1px solid rgba(255,255,255,0.06);border-radius:14px;padding:16px 20px;margin-bottom:24px;">
        <div style="font-size:11px;font-weight:700;letter-spacing:1.4px;color:#8a8a90;text-transform:uppercase;margin-bottom:12px;">✨&nbsp; Account details</div>
        <table role="presentation" width="100%" style="border-collapse:collapse;">
          <tr>
            <td style="padding:6px 0;font-size:14px;color:#8a8a90;">📧&nbsp; Email</td>
            <td style="padding:6px 0;font-size:14px;color:#ffffff;font-weight:600;text-align:right;">{to_email}</td>
          </tr>
          <tr>
            <td style="padding:6px 0;font-size:14px;color:#8a8a90;">🕒&nbsp; Session started</td>
            <td style="padding:6px 0;font-size:14px;color:#ffffff;font-weight:600;text-align:right;">{when_str}</td>
          </tr>
        </table>
      </div>
      <div style="margin-bottom:28px;">
        <p style="margin:0 0 10px;font-size:14px;line-height:1.5;color:#e5e5ea;">⚡&nbsp; <b style="color:#ffffff;">Parallel AI classification</b> — not one post at a time</p>
        <p style="margin:0 0 10px;font-size:14px;line-height:1.5;color:#e5e5ea;">🏷️&nbsp; <b style="color:#ffffff;">Any brand</b> — Kaseya, Ninja, and whatever's next</p>
        <p style="margin:0;font-size:14px;line-height:1.5;color:#e5e5ea;">📊&nbsp; <b style="color:#ffffff;">Full run history</b> — always a click away</p>
      </div>
      <a href="{app_url}" style="display:block;text-align:center;background:#fdb913;background-image:linear-gradient(180deg,#ffd24a,#e39a09);color:#1a1200;text-decoration:none;font-size:15px;font-weight:800;padding:15px 20px;border-radius:12px;margin-bottom:24px;box-shadow:0 8px 24px rgba(253,185,19,0.35);">🚀&nbsp; Launch Sentiment Tagger</a>
      <div style="height:1px;background:rgba(255,255,255,0.08);margin:0 0 18px;"></div>
      <p style="margin:0;font-size:13px;line-height:1.6;color:#8a8a90;">
        🔐&nbsp; <b style="color:#c7c7cd;">Didn't create this account?</b> No worries and nothing to do — just tell your admin and we'll remove it right away.
      </p>
    </div>
    <div style="text-align:center;margin:20px 0 0;">
      <span style="display:inline-block;font-size:11px;font-weight:700;letter-spacing:1px;color:#8a8a90;border:1px solid rgba(255,255,255,0.12);border-radius:999px;padding:6px 14px;">◐&nbsp; LISTENFIRST · INTERNAL TOOL</span>
      <p style="font-size:11px;color:#65656b;margin:14px 0 0;line-height:1.6;">
        Internal social-analytics tool operated by ListenFirst Media.<br>
        Not affiliated with, endorsed by, or operated by the brands or platforms it monitors.
      </p>
    </div>
  </div>
</div>"""
    return _send(to_email, subject, text, html)


def send_reset_code_email(to_email: str, code: str, minutes: int = 10) -> bool:
    """Password-reset 6-digit code."""
    subject = "🔐 Your Sentiment Tagger reset code"
    text = (
        f"🔐 Password reset\n\n"
        f"Use this code to reset your Sentiment Tagger password:\n\n"
        f"    {code}\n\n"
        f"⏳ It expires in {minutes} minutes.\n"
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"— Sentiment Tagger · operated by ListenFirst Media"
    )
    html = f"""\
<div style="margin:0;padding:0;background:#0a0a0c;">
  <div style="max-width:500px;margin:0 auto;padding:32px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
    <div style="text-align:center;margin-bottom:22px;">
      <span style="font-size:13px;font-weight:700;letter-spacing:1.5px;color:#e9c46a;text-transform:uppercase;">◐&nbsp; Meltwater · Sentiment Tagger</span>
    </div>
    <div style="background:#141417;border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:40px 34px;box-shadow:0 20px 50px rgba(0,0,0,0.45);">
      <div style="font-size:34px;line-height:1;margin-bottom:12px;">🔐</div>
      <h1 style="margin:0 0 10px;font-size:24px;line-height:1.25;color:#ffffff;font-weight:800;letter-spacing:-0.4px;">
        Reset your password
      </h1>
      <p style="margin:0 0 24px;font-size:15px;line-height:1.65;color:#b7b7bd;">
        Enter this code back in the app to set a new password.
      </p>
      <div style="background:#1d1d21;border:1px solid rgba(253,185,19,0.25);border-radius:14px;padding:22px;text-align:center;margin-bottom:22px;">
        <div style="font-size:34px;font-weight:800;letter-spacing:10px;color:#fdb913;">{code}</div>
      </div>
      <p style="margin:0 0 6px;font-size:14px;line-height:1.6;color:#e5e5ea;">⏳&nbsp; This code expires in <b style="color:#ffffff;">{minutes} minutes</b>.</p>
      <div style="height:1px;background:rgba(255,255,255,0.08);margin:16px 0 16px;"></div>
      <p style="margin:0;font-size:13px;line-height:1.6;color:#8a8a90;">
        🙅&nbsp; <b style="color:#c7c7cd;">Didn't request this?</b> You can safely ignore this email — your password stays the same.
      </p>
    </div>
    <div style="text-align:center;margin:20px 0 0;">
      <span style="display:inline-block;font-size:11px;font-weight:700;letter-spacing:1px;color:#8a8a90;border:1px solid rgba(255,255,255,0.12);border-radius:999px;padding:6px 14px;">◐&nbsp; LISTENFIRST · INTERNAL TOOL</span>
    </div>
  </div>
</div>"""
    return _send(to_email, subject, text, html)
