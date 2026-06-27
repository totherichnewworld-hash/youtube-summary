#!/usr/bin/env python3
"""
Deliver a summary by email over SMTP.

Configuration comes from environment variables (set these as GitHub Actions
secrets):

    SMTP_HOST     e.g. smtp.gmail.com
    SMTP_PORT     e.g. 587 (STARTTLS) or 465 (SSL)
    SMTP_USER     SMTP username (often the full email address)
    SMTP_PASS     SMTP password or app password
    MAIL_FROM     From: address (defaults to SMTP_USER)
    MAIL_TO       To: address(es), comma-separated

For Gmail use an App Password (Google Account -> Security -> App passwords),
host smtp.gmail.com, port 587.

`email_configured()` lets callers fall back to local files when email isn't set
up, so the pipeline still works without an SMTP account.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def email_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))


def send_email(subject: str, body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    mail_from = os.environ.get("MAIL_FROM", user)
    recipients = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=context)
            s.login(user, password)
            s.send_message(msg)
