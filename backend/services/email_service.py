"""Loomis transactional email."""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

from backend.settings import ROOT, settings

SMTP_SETUP_HINT = "Укажите SMTP_HOST, SMTP_USER и SMTP_PASSWORD в файле .env."

COPY = {
    "ru": {
        "register": "Подтвердите адрес для регистрации в Loomis",
        "link": "Подтвердите привязку электронной почты к Loomis",
        "reset": "Подтвердите сброс пароля Loomis",
        "instructions": "Введите этот код в Loomis, чтобы продолжить.",
        "expiry": "Код действует 10 минут.",
    },
    "en": {
        "register": "Verify your email to register with Loomis",
        "link": "Confirm your email address for Loomis",
        "reset": "Confirm your Loomis password reset",
        "instructions": "Enter this code in Loomis to continue.",
        "expiry": "The code expires in 10 minutes.",
    },
}


def verification_message(address: str, code: str, purpose: str, language: str) -> EmailMessage:
    language = language if language in COPY else "ru"
    copy = COPY[language]
    message = EmailMessage()
    message["Subject"] = copy[purpose]
    message["From"] = settings.smtp_from or settings.smtp_user
    message["To"] = address
    message.set_content(f'Loomis\n\n{copy[purpose]}\n{copy["instructions"]}\n\n{code}\n\n{copy["expiry"]}', charset="utf-8")
    message.add_alternative(f"""<!doctype html>
<html lang="{language}">
<body style="margin:0;background:#09090b;color:#f4f4f5;font-family:Arial,sans-serif">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="24" style="background:#09090b">
    <tr><td align="center">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="28"
             style="max-width:480px;background:#121215;border:1px solid #44444a;border-radius:24px;color:#f4f4f5">
        <tr><td align="center">
          <img src="cid:loomis-lighter@loomis" alt="" width="48" height="48">
          <h1 style="margin:10px 0 24px;font-size:28px">Loomis</h1>
          <h2 style="font-size:19px">{copy[purpose]}</h2>
          <p style="color:#d4d4d8">{copy["instructions"]}</p>
          <p style="padding:18px 6px;border:1px solid #44444a;border-radius:12px;background:#09090b;
                    font:bold 30px monospace;letter-spacing:0.16em">{code}</p>
          <p style="margin-bottom:0;color:#a1a1aa">{copy["expiry"]}</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>""", subtype="html", charset="utf-8")
    message.get_payload()[-1].add_related(
        (ROOT / "desktop/ui/favicon.png").read_bytes(), maintype="image", subtype="png",
        cid="<loomis-lighter@loomis>", disposition="inline",
    )
    return message


def _deliver(message: EmailMessage) -> None:
    context = ssl.create_default_context()
    sender = parseaddr(message["From"])[1]
    if not sender:
        raise ValueError("SMTP_FROM or SMTP_USER is required")
    if settings.smtp_port == 465:
        connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10, context=context)
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    with connection as client:
        if settings.smtp_port != 465:
            client.starttls(context=context)
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message, from_addr=sender)


async def send_verification(address: str, code: str, purpose: str, language: str) -> None:
    if not settings.smtp_host.strip():
        raise ValueError(SMTP_SETUP_HINT)
    await asyncio.to_thread(_deliver, verification_message(address, code, purpose, language))
