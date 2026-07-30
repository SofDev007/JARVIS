"""Email connector plugin: unread message headers over IMAP.

Three deliberate boundaries:

* **Headers only.** ``email.check_email`` fetches envelope headers
  (From/Subject) of unread messages — never bodies, never attachments.
  Reading mail *content* is a knowledge-engine ingestion decision a
  human should make explicitly, not a connector side effect.
* **Password by name.** The manifest's ``password_secret`` is a *name*
  in the M11 secrets manager; the value is resolved with
  ``api.secret(...)`` inside the handler at the last moment and is never
  logged, returned, or placed anywhere the audit log can see.
* **stdlib only.** ``imaplib`` + ``email.header`` — no dependencies, and
  the import is inside the handler so the plugin loads (and the kernel
  starts) even on odd Python builds.
"""

from __future__ import annotations

from typing import Any, Mapping

from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel


def _decode(raw: str) -> str:
    from email.header import decode_header

    parts = []
    for value, charset in decode_header(raw or ""):
        if isinstance(value, bytes):
            parts.append(value.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(value)
    return "".join(parts).strip()


def register(api) -> None:
    config = api.config
    host = str(config.get("host", ""))
    username = str(config.get("username", ""))
    password_secret = str(config.get("password_secret", "email_password"))
    mailbox = str(config.get("mailbox", "INBOX"))
    default_limit = int(config.get("limit", 5))

    def validate(params: Mapping[str, Any]) -> None:
        limit = params.get("limit")
        if limit is not None and (not isinstance(limit, int)
                                  or not 1 <= limit <= 25):
            raise ValueError("'limit' must be an integer in 1..25")

    def handle(params: Mapping[str, Any]) -> str:
        import imaplib

        if not host or not username:
            raise ValueError(
                "email plugin is not configured — set host and username in "
                "plugins/examples/email_imap/plugin.yaml"
            )
        limit = int(params.get("limit") or default_limit)
        password = api.secret(password_secret)  # by name, at the last moment
        client = imaplib.IMAP4_SSL(host)
        try:
            client.login(username, password)
            del password  # used once, immediately forgotten
            client.select(mailbox, readonly=True)  # never mutate the mailbox
            status, data = client.search(None, "UNSEEN")
            if status != "OK":
                raise ValueError(f"IMAP search failed: {status}")
            ids = data[0].split()
            headers: list[str] = []
            for message_id in ids[-limit:]:
                status, parts = client.fetch(
                    message_id,
                    "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])",
                )
                if status != "OK" or not parts or parts[0] is None:
                    continue
                import email as email_module

                message = email_module.message_from_bytes(parts[0][1])
                sender = _decode(message.get("From", "?"))
                subject = _decode(message.get("Subject", "(no subject)"))
                headers.append(f"{sender}: {subject}")
            if not ids:
                return f"no unread mail in {mailbox}"
            listing = " | ".join(headers)
            return (f"{len(ids)} unread in {mailbox}"
                    + (f", latest {len(headers)}: {listing}" if headers
                       else ""))
        finally:
            try:
                client.logout()
            except Exception:
                pass

    api.register_action(ActionSpec(
        name="check_email",
        description=("Count unread mail and list the latest senders/"
                     "subjects (headers only; password via secrets "
                     "manager)."),
        risk=RiskLevel.SENSITIVE,
        handler=handle,
        validate=validate,
    ))

    # -- send_email (DANGEROUS: leaves the machine, reaches other people) ----
    smtp_host = str(config.get("smtp_host", ""))
    smtp_port = int(config.get("smtp_port", 587))
    allowed_recipients = [
        str(domain).lower().lstrip("@")
        for domain in config.get("allowed_recipient_domains", [])
    ]

    def _recipient_allowed(address: str) -> bool:
        if not allowed_recipients:
            return True  # no allow-list configured → any recipient
        domain = address.rsplit("@", 1)[-1].lower()
        return domain in allowed_recipients

    def validate_send(params: Mapping[str, Any]) -> None:
        import re

        to = params.get("to")
        if not isinstance(to, str) or not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+$",
                                                   to.strip()):
            raise ValueError("a valid 'to' email address is required")
        if not _recipient_allowed(to.strip()):
            raise ValueError(
                f"recipient domain not in allowed_recipient_domains "
                f"{allowed_recipients} — refusing to send to {to!r}")
        subject = params.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("a non-empty 'subject' is required")
        if len(subject) > 300:
            raise ValueError("'subject' exceeds 300 characters")
        body = params.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("a non-empty 'body' is required")
        if len(body) > 20000:
            raise ValueError("'body' exceeds 20000 characters")

    def handle_send(params: Mapping[str, Any]) -> str:
        import smtplib
        from email.message import EmailMessage

        if not smtp_host or not username:
            raise ValueError(
                "sending is not configured — set smtp_host and username in "
                "plugins/examples/email_imap/plugin.yaml")
        to = str(params["to"]).strip()
        message = EmailMessage()
        message["From"] = username
        message["To"] = to
        message["Subject"] = str(params["subject"]).strip()
        message.set_content(str(params["body"]))
        password = api.secret(password_secret)  # by name, last moment
        client = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        try:
            client.starttls()
            client.login(username, password)
            del password  # used once, forgotten
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:
                pass
        return f"sent email to {to} — subject: {message['Subject']}"

    api.register_action(ActionSpec(
        name="send_email",
        description=("Send an email (to, subject, body). Leaves the machine "
                     "and reaches a real person — always confirmed."),
        risk=RiskLevel.DANGEROUS,
        handler=handle_send,
        validate=validate_send,
    ))
