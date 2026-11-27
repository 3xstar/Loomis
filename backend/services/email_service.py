"""Loomis transactional email."""

import asyncio
import logging
import smtplib
import ssl
from email.headerregistry import Address
from email.message import EmailMessage
from email.mime.image import MIMEImage
from io import BytesIO

import httpx
from PIL import Image, ImageOps

from backend.settings import ROOT, settings

logger = logging.getLogger(__name__)
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
DEV_MODE_HINT = "[Dev Mode] Код подтверждения записан в журнал сервера."
LOGO_PATH = "desktop/ui/loomis-inverse-2048.png"
LOGO_URL = f"https://raw.githubusercontent.com/3xstar/Loomis/main/{LOGO_PATH}"
LOGO = '<img src="cid:loomis_logo" width="40" height="40" alt="Loomis" style="display:block;border:0">'


COPY = {
    "ru": {
        "unlink": "Подтвердите отвязку электронной почты от Loomis",
        "warning": "Никому не сообщайте этот код.",
        "register": "Подтвердите адрес для регистрации в Loomis",
        "link": "Подтвердите привязку электронной почты к Loomis",
        "reset": "Подтвердите сброс пароля Loomis",
        "instructions": "Введите этот код в Loomis, чтобы продолжить.",
        "expiry": "Код действует 10 минут.",
    },
    "en": {
        "unlink": "Confirm unlinking your email from Loomis",
        "warning": "Never share this code with anyone.",
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
    message["From"] = _sender(settings.email_sender)
    message["To"] = address
    message.set_content(f'Loomis\n\n{copy[purpose]}\n{copy["instructions"]}\n\n{code}\n\n{copy["expiry"]}\n{copy["warning"]}', charset="utf-8")
    message.add_alternative(f"""<!doctype html>
<html lang="{language}">
<body style="margin:0;background:#09090b;color:#f4f4f5;font-family:Arial,sans-serif">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="24" style="background:#09090b">
    <tr><td align="center">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="28"
             style="max-width:480px;background:#121215;border:1px solid #44444a;border-radius:24px;color:#f4f4f5">
        <tr><td style="padding:0 28px"><div style="height:1px;background:#73737a;font-size:1px;line-height:1px">&nbsp;</div></td></tr>
        <tr><td align="center">
          {LOGO}
          <h1 style="margin:10px 0 24px;font-size:28px;line-height:1.45">Loomis</h1>
          <h2 style="font-size:19px;line-height:1.45">{copy[purpose]}</h2>
          <p style="color:#d4d4d8">{copy["instructions"]}</p>
          <p style="padding:18px 6px;border:1px solid #44444a;border-radius:12px;background:#09090b;
                    font:bold 30px monospace;letter-spacing:8px;user-select:all">{code}</p>
          <p style="margin:0;color:#b5b5ba;font-size:13px">{copy["expiry"]}</p>
          <p style="margin:20px 0 0;padding-top:16px;border-top:1px solid #44444a;
                    color:#b5b5ba;font-size:13px;line-height:1.45">{copy["warning"]}</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>""", subtype="html", charset="utf-8")
    return message


def _sender(address: str) -> Address:
    return Address(display_name=settings.email_sender_name, addr_spec=address)


async def _deliver_brevo(message: EmailMessage, api_key: str) -> None:
    payload = {
        "sender": {"email": settings.email_sender, "name": settings.email_sender_name},
        "to": [{"email": message["To"].addresses[0].addr_spec}],
        "subject": str(message["Subject"]),
        "htmlContent": message.get_body(("html",)).get_content().replace("cid:loomis_logo", LOGO_URL),
        "textContent": message.get_body(("plain",)).get_content(),
    }
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(BREVO_URL, headers={"api-key": api_key}, json=payload)
        response.raise_for_status()


def _deliver(message: EmailMessage) -> None:
    context = ssl.create_default_context()
    sender = message["From"].addresses[0].addr_spec
    if settings.smtp_from:
        sender = settings.smtp_from
        message.replace_header("From", _sender(sender))
    with Image.open(ROOT / LOGO_PATH) as logo, BytesIO() as png:
        ImageOps.pad(logo.convert("RGBA"), (48, 48), method=Image.Resampling.LANCZOS).save(png, format="PNG")
        attachment = MIMEImage(png.getvalue(), _subtype="png")
    attachment.add_header("Content-ID", "<loomis_logo>")
    attachment.add_header("Content-Disposition", "inline")
    html = message.get_body(("html",))
    html.make_related()
    html.attach(attachment)
    host = settings.smtp_host.strip() or "smtp.gmail.com"
    if settings.smtp_port == 465:
        connection = smtplib.SMTP_SSL(host, settings.smtp_port, timeout=10, context=context)
    else:
        connection = smtplib.SMTP(host, settings.smtp_port, timeout=10)
    with connection as client:
        if settings.smtp_port != 465:
            client.starttls(context=context)
        client.login(settings.smtp_user or sender, settings.smtp_password)
        client.send_message(message, from_addr=sender)


def verification_hint() -> str | None:
    if (settings.api_host in {"127.0.0.1", "::1", "localhost"}
            and not settings.brevo_api_key.strip() and not settings.smtp_password.strip()):
        return DEV_MODE_HINT
    return None


async def send_verification(address: str, code: str, purpose: str, language: str) -> str | None:
    hint = verification_hint()
    if hint:
        logger.warning("[Dev Mode] Loomis %s code for %s: %s", purpose, address, code)
        return hint

    message = verification_message(address, code, purpose, language)
    api_key = settings.brevo_api_key.strip()
    if api_key:
        try:
            await _deliver_brevo(message, api_key)
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning("Brevo OTP delivery failed with HTTP %s", exc.response.status_code)
        except httpx.RequestError as exc:
            logger.warning("Brevo OTP delivery failed (%s)", type(exc).__name__)

    if not settings.smtp_password.strip():
        raise OSError("No SMTP fallback available")
    await asyncio.to_thread(_deliver, message)
    return None
