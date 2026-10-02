"""Notification channels: in-app, e-mail (SMTP), LINE Messaging API, Telegram, Discord, generic webhook (Teams)."""
import base64
import html as _html
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import httpx

from .. import config
from ..core import add_notice, get_setting
from ..models import SendLog

TIMEOUT = 20


def _log(db, channel, event, target, ok, detail=""):
    db.add(SendLog(channel=channel, event=event[:60], target=(target or "")[:250], ok=ok, detail=str(detail)[:400]))
    db.commit()


def split_addr(s):
    return [a.strip() for a in (s or "").replace(";", ",").replace("\n", ",").split(",") if a.strip()]


def wrap_html(title, inner):
    return f"""<div style="font-family:Tahoma,Arial,sans-serif;font-size:14px;color:#14213D;max-width:760px">
<div style="border-left:6px solid #F2B01E;padding:4px 0 4px 12px;margin-bottom:14px">
<div style="font-size:18px;font-weight:bold">{_html.escape(title)}</div>
<div style="color:#5B6677;font-size:12px">Daily QC Operation Report</div></div>{inner}
<p style="color:#8A94A3;font-size:12px;margin-top:18px">อีเมลนี้ส่งอัตโนมัติจากโปรแกรม Daily QC Operation Report</p></div>"""


BREVO_URL = os.getenv("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email")
RESEND_URL = os.getenv("RESEND_API_URL", "https://api.resend.com/emails")


def _send_api(s, to_l, cc_l, subject, html, attachments):
    """E-mail through a provider's HTTPS API. Used where the SMTP ports are blocked (e.g. Render's free plan)."""
    sender = s.get("sender") or s.get("user")
    name = s.get("sender_name") or "Daily QC Report"
    if not sender:
        return False, "ยังไม่ได้ใส่อีเมลผู้ส่ง"
    att = [(n, base64.b64encode(d).decode()) for n, d, _ in attachments or []]
    if s["method"] == "brevo":
        body = {"sender": {"name": name, "email": sender}, "to": [{"email": a} for a in to_l],
                "subject": subject, "htmlContent": html}
        if cc_l:
            body["cc"] = [{"email": a} for a in cc_l]
        if att:
            body["attachment"] = [{"name": n, "content": c} for n, c in att]
        r = httpx.post(BREVO_URL, timeout=60, json=body, headers={"api-key": s["api_key"], "accept": "application/json"})
    else:
        body = {"from": f"{name} <{sender}>", "to": to_l, "subject": subject, "html": html}
        if cc_l:
            body["cc"] = cc_l
        if att:
            body["attachments"] = [{"filename": n, "content": c} for n, c in att]
        r = httpx.post(RESEND_URL, timeout=60, json=body, headers={"Authorization": f"Bearer {s['api_key']}"})
    return (True, "ส่งแล้ว") if r.status_code in (200, 201, 202) else (False, f"{r.status_code} {r.text[:300]}")


def send_email(db, to, cc, subject, html, attachments=None, event="email"):
    s = get_setting(db, "smtp")
    to_l, cc_l = split_addr(to), split_addr(cc)
    api = s.get("method") in ("brevo", "resend")
    if not to_l or (api and not s.get("api_key")) or (not api and not s.get("host")):
        _log(db, "email", event, to, False, "ยังไม่ได้ตั้งค่า Email หรือไม่มีผู้รับ")
        return False, "ยังไม่ได้ตั้งค่า Email หรือไม่มีผู้รับ"
    if api:
        try:
            ok, detail = _send_api(s, to_l, cc_l, subject, html, attachments)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        _log(db, "email", event, ", ".join(to_l + cc_l), ok, subject if ok else detail)
        return ok, detail
    msg = EmailMessage()
    sender = s.get("sender") or s.get("user")
    msg["From"] = formataddr((s.get("sender_name") or "Daily QC Report", sender))
    msg["To"] = ", ".join(to_l)
    if cc_l:
        msg["Cc"] = ", ".join(cc_l)
    msg["Subject"] = subject
    msg.set_content("รายงานนี้เป็น HTML กรุณาเปิดด้วยโปรแกรมอีเมลที่รองรับ")
    msg.add_alternative(html, subtype="html")
    for name, data, mime in attachments or []:
        mt, st = mime.split("/", 1)
        msg.add_attachment(data, maintype=mt, subtype=st, filename=name)
    try:
        port = int(s.get("port") or 587)
        if s.get("security") == "ssl":
            srv = smtplib.SMTP_SSL(s["host"], port, timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(s["host"], port, timeout=TIMEOUT)
            if s.get("security") == "starttls":
                srv.starttls(context=ssl.create_default_context())
        if s.get("user"):
            srv.login(s["user"], s.get("password") or "")
        srv.send_message(msg, to_addrs=to_l + cc_l)
        srv.quit()
        _log(db, "email", event, ", ".join(to_l + cc_l), True, subject)
        return True, "ส่งแล้ว"
    except Exception as e:  # noqa: BLE001
        hint = " (Host ฟรีบางเจ้าปิด port SMTP 25/465/587 ให้เปลี่ยนวิธีส่งเป็น Brevo API หรือ Resend API)" \
            if config.IS_HOSTED and isinstance(e, (TimeoutError, OSError)) else ""
        _log(db, "email", event, ", ".join(to_l + cc_l), False, f"{e}{hint}")
        return False, f"{e}{hint}"


def send_line(db, text, event="line"):
    s = get_setting(db, "line")
    if not s.get("token") or not s.get("to"):
        return False, "ยังไม่ได้ตั้งค่า LINE"
    ok_all, last = True, ""
    for target in split_addr(s["to"]):
        try:
            r = httpx.post("https://api.line.me/v2/bot/message/push", timeout=TIMEOUT,
                           headers={"Authorization": f"Bearer {s['token']}"},
                           json={"to": target, "messages": [{"type": "text", "text": text[:4900]}]})
            ok = r.status_code == 200
            last = "" if ok else r.text
        except Exception as e:  # noqa: BLE001
            ok, last = False, str(e)
        ok_all &= ok
        _log(db, "line", event, target, ok, last)
    return ok_all, last or "ส่งแล้ว"


def send_telegram(db, text, photo=None, document=None, event="telegram"):
    s = get_setting(db, "telegram")
    if not s.get("token") or not s.get("chat_id"):
        return False, "ยังไม่ได้ตั้งค่า Telegram"
    base = f"https://api.telegram.org/bot{s['token']}"
    ok_all, last = True, ""
    for chat in split_addr(s["chat_id"]):
        try:
            if photo:
                r = httpx.post(base + "/sendPhoto", timeout=TIMEOUT, data={"chat_id": chat, "caption": text[:1000]},
                               files={"photo": ("photo.jpg", photo, "image/jpeg")})
            else:
                r = httpx.post(base + "/sendMessage", timeout=TIMEOUT, json={"chat_id": chat, "text": text[:4000]})
            ok = r.status_code == 200
            last = "" if ok else r.text
            if ok and document:
                for name, data, mime in document:
                    httpx.post(base + "/sendDocument", timeout=60, data={"chat_id": chat},
                               files={"document": (name, data, mime)})
        except Exception as e:  # noqa: BLE001
            ok, last = False, str(e)
        ok_all &= ok
        _log(db, "telegram", event, chat, ok, last)
    return ok_all, last or "ส่งแล้ว"


def send_discord(db, text, files=None, event="discord"):
    s = get_setting(db, "discord")
    if not s.get("url"):
        return False, "ยังไม่ได้ตั้งค่า Discord"
    try:
        if files:
            fs = {f"files[{i}]": (n, d, m) for i, (n, d, m) in enumerate(files[:8])}
            r = httpx.post(s["url"], timeout=60, data={"payload_json": json.dumps({"content": text[:1900]})}, files=fs)
        else:
            r = httpx.post(s["url"], timeout=TIMEOUT, json={"content": text[:1900]})
        ok = r.status_code in (200, 204)
        detail = "" if ok else r.text
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)
    _log(db, "discord", event, "webhook", ok, detail)
    return ok, detail or "ส่งแล้ว"


def send_webhook(db, text, title="", event="webhook"):
    s = get_setting(db, "webhook")
    if not s.get("url"):
        return False, "ยังไม่ได้ตั้งค่า Webhook"
    try:
        r = httpx.post(s["url"], timeout=TIMEOUT, json={"title": title, "text": text, "source": "daily-qc-report"})
        ok = r.status_code < 300
        detail = "" if ok else r.text
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)
    _log(db, "webhook", event, s["url"][:60], ok, detail)
    return ok, detail or "ส่งแล้ว"


def dispatch(db, event, channels, subject, text, html=None, to="", cc="", attachments=None, photo=None,
             link="", perm="", level="info"):
    """Send one message through every selected channel. Never raises."""
    res = {}
    base = (get_setting(db, "general").get("base_url") or "").rstrip("/")
    full = text + (f"\n{base}{link}" if base and link else "")
    for ch in channels or []:
        try:
            if ch == "inapp":
                add_notice(db, subject, text, link, perm, level)
                res[ch] = (True, "")
            elif ch == "email":
                inner = html or "<pre style='font-family:inherit;white-space:pre-wrap'>" + _html.escape(text) + "</pre>"
                if base and link:
                    inner += f'<p><a href="{base}{link}">เปิดดูในโปรแกรม</a></p>'
                att = list(attachments or [])
                if photo:
                    att.append(("photo.jpg", photo, "image/jpeg"))
                res[ch] = send_email(db, to, cc, subject, wrap_html(subject, inner), att, event)
            elif ch == "line":
                res[ch] = send_line(db, f"{subject}\n{full}", event)
            elif ch == "telegram":
                res[ch] = send_telegram(db, f"{subject}\n{full}", photo, attachments, event)
            elif ch == "discord":
                fl = list(attachments or [])
                if photo:
                    fl.append(("photo.jpg", photo, "image/jpeg"))
                res[ch] = send_discord(db, f"**{subject}**\n{full}", fl, event)
            elif ch == "webhook":
                res[ch] = send_webhook(db, full, subject, event)
        except Exception as e:  # noqa: BLE001
            res[ch] = (False, str(e))
            _log(db, ch, event, "", False, e)
    return res
