"""Notification channels: in-app, e-mail (SMTP), LINE Messaging API, Telegram, Discord, generic webhook (Teams)."""
import base64
import html as _html
import json
import os
import re
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import httpx

from .. import config
from ..core import add_notice, get_setting
from ..models import SendLog

TIMEOUT = 20


def _log(db, channel, event, target, ok, detail=""):
    db.add(SendLog(channel=channel, event=event[:60], target=(target or "")[:250], ok=ok, detail=str(detail)[:400]))
    db.commit()


def split_addr(s):
    """Accepts commas, semicolons, new lines, spaces or Thai/full-width commas between addresses."""
    return [a.strip().strip("<>") for a in re.split(r"[,;\s\uFF0C\u3001]+", s or "") if a.strip()]


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def clean_addrs(to, cc=""):
    """(to, cc, invalid): valid addresses only, no duplicates, nobody in both To and Cc.
    to / cc may be a string or a list."""
    seen, out, bad = set(), [], []
    for group in (to, cc):
        lst, keep = (group if isinstance(group, (list, tuple)) else split_addr(group)), []
        for a in lst:
            if not EMAIL_RE.match(a):
                bad.append(a)
            elif a.lower() not in seen:
                seen.add(a.lower())
                keep.append(a)
        out.append(keep)
    return out[0], out[1], bad


def role_emails(db, roles=(), user_ids=()):
    """E-mail addresses of active users who have one of these roles, or these ids."""
    from ..models import Role, User
    roles, user_ids = [r for r in roles or [] if r], [u for u in user_ids or [] if u]
    if not roles and not user_ids:
        return []
    q = db.query(User).outerjoin(Role, Role.id == User.role_id).filter(User.active == True)  # noqa: E712
    out = []
    for u in q.all():
        if (u.role and u.role.code in roles) or u.id in user_ids:
            if u.email and EMAIL_RE.match(u.email.strip()):
                out.append(u.email.strip())
    return out


def recipients(db, cfg, user_ids=()):
    """To / Cc for an event or a schedule: the typed addresses plus the users of the chosen roles."""
    roles = cfg.get("roles") or []
    if isinstance(roles, str):
        roles = [r for r in roles.split(",") if r]
    to = split_addr(cfg.get("to")) + role_emails(db, roles, user_ids)
    to, cc, bad = clean_addrs(to, cfg.get("cc") or "")
    return to, cc, bad


def wrap_html(title, inner):
    return f"""<div style="font-family:Tahoma,Arial,sans-serif;font-size:14px;color:#14213D;max-width:760px">
<div style="border-left:6px solid #F2B01E;padding:4px 0 4px 12px;margin-bottom:14px">
<div style="font-size:18px;font-weight:bold">{_html.escape(title)}</div>
<div style="color:#5B6677;font-size:12px">Daily QC Operation Report</div></div>{inner}
<p style="color:#8A94A3;font-size:12px;margin-top:18px">อีเมลนี้ส่งอัตโนมัติจากโปรแกรม Daily QC Operation Report</p></div>"""


BREVO_URL = os.getenv("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email")
RESEND_URL = os.getenv("RESEND_API_URL", "https://api.resend.com/emails")
BREVO_ACCOUNT_URL = os.getenv("BREVO_ACCOUNT_URL", "https://api.brevo.com/v3/account")
RESEND_DOMAINS_URL = os.getenv("RESEND_DOMAINS_URL", "https://api.resend.com/domains")


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
    if r.status_code in (200, 201, 202):
        return True, "ส่งแล้ว"
    low = r.text.lower()
    hint = ""
    if r.status_code in (401, 403) and "domain" in low:
        hint = "ต้องยืนยันโดเมนของอีเมลผู้ส่งก่อน (หรือส่งได้เฉพาะถึงอีเมลเจ้าของบัญชี)"
    elif r.status_code in (401, 403):
        hint = "API key ไม่ถูกต้อง ถูกยกเลิก หรือบัญชียังไม่เปิดใช้การส่งอีเมล"
    elif "sender" in low:
        hint = "อีเมลผู้ส่งยังไม่ได้ยืนยันกับผู้ให้บริการ (Brevo: Senders > Add a sender)"
    elif r.status_code == 429:
        hint = "ส่งเกินโควตาของแผนฟรีในวันนี้"
    return False, f"{r.status_code} {r.text[:250]}" + (f" | {hint}" if hint else "")


# ---------------------------------------------------------------- SMTP
SMTP_PRESETS = {      # host -> what usually goes wrong, shown with the error
    "gmail": "Gmail: ต้องเปิด 2-Step Verification แล้วใช้ App password 16 ตัว (ไม่ใช่รหัสผ่านปกติของ Gmail)",
    "outlook": "Microsoft 365 / Outlook: ผู้ดูแลระบบต้องเปิด Authenticated SMTP ของบัญชีนี้ และบัญชีที่ใช้ MFA ต้องใช้ App password",
}


def _provider(host):
    h = (host or "").lower()
    if "gmail" in h or "google" in h:
        return "gmail"
    if "office365" in h or "outlook" in h or "hotmail" in h or "live.com" in h:
        return "outlook"
    return ""


def explain(e, s=None):
    """A Thai explanation of an e-mail error that tells the admin what to change."""
    s = s or {}
    msg = str(e)
    low = msg.lower()
    prov = _provider(s.get("host"))
    if isinstance(e, smtplib.SMTPAuthenticationError) or " 535" in f" {msg}" or "authentication" in low:
        if prov == "gmail":
            return SMTP_PRESETS["gmail"]
        if prov == "outlook" or "5.7.139" in msg or "smtpclientauthentication" in low:
            return SMTP_PRESETS["outlook"]
        return "ชื่อผู้ใช้หรือรหัสผ่านของบัญชีอีเมลไม่ถูกต้อง หรือบัญชีนี้ไม่อนุญาตให้ส่งผ่าน SMTP"
    if "auth extension not supported" in low or "smtp auth extension" in low:
        return "เซิร์ฟเวอร์ต้องการการเข้ารหัสก่อน Login ให้เลือกการเข้ารหัสเป็น STARTTLS (port 587)"
    if "wrong version number" in low or "unknown protocol" in low or "record layer failure" in low:
        return "การเข้ารหัสไม่ตรงกับ port: port 465 ต้องเลือก SSL ส่วน port 587 ต้องเลือก STARTTLS"
    if "certificate verify failed" in low:
        return "ตรวจใบรับรอง (certificate) ของ SMTP server ไม่ผ่าน ตรวจชื่อ SMTP server ให้ตรงกับที่ผู้ให้บริการกำหนด"
    if isinstance(e, socket.gaierror) or "name or service not known" in low or "getaddrinfo" in low or "nodename nor servname" in low:
        return "ไม่พบ SMTP server ชื่อนี้ ตรวจการสะกด หรือเครื่องนี้ยังไม่ได้ต่ออินเทอร์เน็ต"
    if isinstance(e, (TimeoutError, socket.timeout, ConnectionRefusedError)) or "timed out" in low or "refused" in low \
            or "unreachable" in low or "errno 101" in low or "errno 110" in low or "errno 111" in low:
        if config.IS_HOSTED:
            return "ต่อ SMTP server ไม่ได้: Host ฟรีอย่าง Render ปิด port 25 / 465 / 587 ให้เปลี่ยนวิธีส่งเป็น Brevo API หรือ Resend API"
        return "ต่อ SMTP server ไม่ได้: Firewall หรือ Proxy ของโรงงานอาจปิด port นี้ ลอง port อื่น (465 / 587 / 2525) หรือใช้ Brevo API"
    if isinstance(e, smtplib.SMTPSenderRefused) or "sender" in low and ("refused" in low or "not allowed" in low or "553" in msg):
        return "Server ไม่ยอมให้ใช้อีเมลผู้ส่งนี้ ให้ใส่อีเมลผู้ส่งเป็นอีเมลเดียวกับบัญชีที่ Login"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "Server ปฏิเสธผู้รับทุกคน ตรวจที่อยู่อีเมลผู้รับ"
    if isinstance(e, smtplib.SMTPDataError) and ("size" in low or "552" in msg):
        return "ไฟล์แนบใหญ่เกินที่ Server รับได้"
    return ""


def _security(s):
    """The security mode actually used. A port that only works one way overrides a wrong choice."""
    port = int(s.get("port") or 587)
    sec = s.get("security") or "starttls"
    if port == 465:
        return "ssl"
    if sec == "ssl" and port in (25, 587, 2525):
        return "starttls"
    return sec


def _password(s):
    pw = s.get("password") or ""
    # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces are not part of it
    return pw.replace(" ", "") if _provider(s.get("host")) == "gmail" else pw


def smtp_open(s, steps=None):
    """Connect, encrypt and log in. With a list in `steps`, every stage is recorded for the diagnostics page."""
    def step(name, detail=""):
        if steps is not None:
            steps.append((name, True, detail))
    host, port = (s.get("host") or "").strip(), int(s.get("port") or 587)
    sec = _security(s)
    socket.getaddrinfo(host, port)
    step("DNS", host)
    ctx = ssl.create_default_context()
    if sec == "ssl":
        srv = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
        step("connect", f"{host}:{port} SSL")
    else:
        srv = smtplib.SMTP(host, port, timeout=TIMEOUT)
        step("connect", f"{host}:{port}")
        srv.ehlo()
        if sec == "starttls" or srv.has_extn("starttls"):
            srv.starttls(context=ctx)
            srv.ehlo()
            step("tls", "STARTTLS")
    if s.get("user"):
        srv.login(s["user"].strip(), _password(s))
        step("login", s["user"].strip())
    return srv


def html_to_text(html):
    t = re.sub(r"(?is)<(script|style).*?</\1>", "", html or "")
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", t)
    t = re.sub(r"(?i)</t[dh]>", "\t", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = _html.unescape(t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


_last_fail = {}


def _fail_notice(db, event, detail):
    """An e-mail that fails is easy to miss, so admins get one in-app notice per event per 30 minutes."""
    now = time.time()
    if now - _last_fail.get(event, 0) < 1800 or event == "test":
        return
    _last_fail[event] = now
    try:
        add_notice(db, f"ส่งอีเมลไม่สำเร็จ ({event})", str(detail)[:300], "/admin/notify", "admin", "alert")
    except Exception:  # noqa: BLE001
        pass


MAX_ATTACH = 18 * 1024 * 1024


def send_email(db, to, cc, subject, html, attachments=None, event="email", text=""):
    """Returns (ok, detail). Every attempt is written to the send log, with the reason when it fails."""
    s = get_setting(db, "smtp")
    to_l, cc_l, bad = clean_addrs(to, cc)
    api = s.get("method") in ("brevo", "resend")
    note = f" (ข้ามที่อยู่ที่ไม่ถูกต้อง: {', '.join(bad)})" if bad else ""
    problem = ""
    if api and not s.get("api_key"):
        problem = "ยังไม่ได้ใส่ API key ของ Email"
    elif not api and not s.get("host"):
        problem = "ยังไม่ได้ตั้งค่า SMTP server"
    elif not (s.get("sender") or s.get("user")):
        problem = "ยังไม่ได้ใส่อีเมลผู้ส่ง"
    elif not to_l:
        problem = "ไม่มีผู้รับ: ใส่อีเมลในช่อง To หรือเลือกกลุ่มผู้ใช้ที่มีอีเมล" + note
    if problem:
        _log(db, "email", event, ", ".join(to if isinstance(to, (list, tuple)) else split_addr(to))[:250], False, problem)
        _fail_notice(db, event, problem)
        return False, problem
    attachments = list(attachments or [])
    if sum(len(d) for _, d, _ in attachments) > MAX_ATTACH:
        attachments = []
        html += "<p style='color:#B7791F'>ไฟล์แนบใหญ่เกินกว่าจะส่งทางอีเมลได้ กรุณาดาวน์โหลดจากโปรแกรม</p>"
        note += " (ไฟล์แนบใหญ่เกิน จึงไม่ได้แนบ)"
    if api:
        try:
            ok, detail = _send_api(s, to_l, cc_l, subject, html, attachments)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{e} {explain(e, s)}".strip()
        _log(db, "email", event, ", ".join(to_l + cc_l), ok, (subject + note) if ok else detail)
        if not ok:
            _fail_notice(db, event, detail)
        return ok, (detail + note) if ok else detail
    msg = EmailMessage()
    sender = (s.get("sender") or s.get("user")).strip()
    msg["From"] = formataddr((s.get("sender_name") or "Daily QC Report", sender))
    msg["To"] = ", ".join(to_l)
    if cc_l:
        msg["Cc"] = ", ".join(cc_l)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(text or html_to_text(html) or subject)
    msg.add_alternative(html, subtype="html")
    for name, data, mime in attachments:
        mt, st = mime.split("/", 1)
        msg.add_attachment(data, maintype=mt, subtype=st, filename=name)
    try:
        srv = smtp_open(s)
        try:
            refused = srv.send_message(msg, from_addr=sender, to_addrs=to_l + cc_l)
        finally:
            try:
                srv.quit()
            except Exception:  # noqa: BLE001
                pass
        if refused:
            note += " (server ไม่รับ: " + ", ".join(refused) + ")"
        _log(db, "email", event, ", ".join(a for a in to_l + cc_l if a not in (refused or {})), True, subject + note)
        return True, "ส่งแล้ว" + note
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"[:250]
        hint = explain(e, s)
        full = f"{detail} | {hint}" if hint else detail
        _log(db, "email", event, ", ".join(to_l + cc_l), False, full)
        _fail_notice(db, event, full)
        return False, full


def check_email(db):
    """Step-by-step check for the diagnostics panel: list of (step, ok, detail). Sends nothing."""
    s = get_setting(db, "smtp")
    steps = []
    if s.get("method") in ("brevo", "resend"):
        if not s.get("api_key"):
            return [("config", False, "ยังไม่ได้ใส่ API key")]
        try:
            if s["method"] == "brevo":
                r = httpx.get(BREVO_ACCOUNT_URL, timeout=TIMEOUT, headers={"api-key": s["api_key"], "accept": "application/json"})
            else:
                r = httpx.get(RESEND_DOMAINS_URL, timeout=TIMEOUT, headers={"Authorization": f"Bearer {s['api_key']}"})
        except Exception as e:  # noqa: BLE001
            return [("connect", False, f"{e}")]
        if r.status_code in (401, 403):
            return [("connect", True, s["method"]), ("login", False, f"{r.status_code}: API key ไม่ถูกต้องหรือถูกยกเลิก สร้าง key ใหม่แล้ววางอีกครั้ง")]
        if r.status_code >= 400:
            return [("connect", True, s["method"]), ("login", False, f"{r.status_code} {r.text[:200]}")]
        steps = [("connect", True, s["method"]), ("login", True, "API key ใช้ได้")]
        if s["method"] == "resend":
            try:
                doms = [d.get("name") for d in r.json().get("data", []) if d.get("status") == "verified"]
            except ValueError:
                doms = []
            dom = (s.get("sender") or "").split("@")[-1].lower()
            steps.append(("sender", dom in [d.lower() for d in doms],
                          f"โดเมน {dom} ยืนยันแล้ว" if dom in [d.lower() for d in doms] else
                          f"โดเมน {dom or '-'} ยังไม่ได้ยืนยันใน Resend จึงส่งได้เฉพาะถึงอีเมลเจ้าของบัญชี"))
        else:
            steps.append(("sender", bool(s.get("sender")), s.get("sender") or "ยังไม่ได้ใส่อีเมลผู้ส่ง (ต้องเป็นอีเมลที่ยืนยันแล้วใน Brevo > Senders)"))
        return steps
    if not s.get("host"):
        return [("config", False, "ยังไม่ได้ใส่ SMTP server")]
    try:
        srv = smtp_open(s, steps)
        srv.quit()
        if not s.get("user"):
            steps.append(("login", True, "ไม่ได้ Login (ใช้ได้กับ relay ภายในบริษัทเท่านั้น)"))
    except Exception as e:  # noqa: BLE001
        names = ["DNS", "connect", "tls", "login"]
        done = {n for n, _, _ in steps}
        failed = next((n for n in names if n not in done), "send")
        if failed == "tls" and _security(s) == "none":
            failed = "login"
        steps.append((failed, False, f"{type(e).__name__}: {str(e)[:160]} | {explain(e, s) or 'ดูข้อความ error'}"))
    return steps


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
                res[ch] = send_email(db, to, cc, subject, wrap_html(subject, inner), att, event,
                                     text=f"{subject}\n\n{full}\n\n-- Daily QC Operation Report")
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
