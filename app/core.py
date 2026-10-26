"""Shared helpers: passwords, permissions, settings, template rendering, date ranges."""
import calendar
import hashlib
import hmac
import json
import secrets
from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .models import AuditLog, Notice, NoticeRead, Setting, User

# ---------------------------------------------------------------- permissions
PERMS = [
    ("overview.view", "ดูภาพรวม Daily QC", "View daily QC overview"),
    ("insp.view", "ดูข้อมูล QC Inspection", "View inspection data"),
    ("insp.entry", "บันทึกผลตรวจของ Line ตัวเอง", "Enter inspection results (own lines)"),
    ("insp.edit_all", "แก้ไข/ลบผลตรวจทุก Line และย้อนหลัง", "Edit or delete any inspection record"),
    ("insp.export", "Export QC Inspection เป็น Excel", "Export inspection data"),
    ("issue.view", "ดู Quality Issue", "View quality issues"),
    ("issue.create", "แจ้งปัญหา Quality Issue", "Report a quality issue"),
    ("issue.answer", "ตอบปัญหา: Root Cause, Correction, Plan / Actual date, ผู้รับผิดชอบ, Prevention",
     "Answer cases: root cause, correction, plan / actual date, responsible, prevention"),
    ("issue.status", "เปลี่ยนสถานะเคส (เปิดอยู่ / กำลังแก้ไข / ปิดแล้ว)", "Change case status (open / in progress / closed)"),
    ("issue.edit_all", "แก้ไข/ลบเคสของทุกคน", "Edit or delete any case"),
    ("issue.export", "Export Quality Issue เป็น Excel", "Export quality issues"),
    ("master.manage", "จัดการ Part, ลูกค้า, Line, อาการเสีย", "Manage parts, customers, lines, defects"),
    ("admin", "ผู้ดูแลระบบ (ผู้ใช้ ตั้งค่า แจ้งเตือน สำรองข้อมูล)", "System administration"),
]
ALL_PERMS = [p[0] for p in PERMS]
DEFAULT_ROLES = [
    ("admin", "Admin", ALL_PERMS, True),
    ("manager", "QC Supervisor / Engineer", [p for p in ALL_PERMS if p != "admin"], False),
    ("qc", "QC ประจำ Line", ["overview.view", "insp.view", "insp.entry", "insp.export",
                             "issue.view", "issue.create", "issue.status", "issue.export"], False),
    ("assy", "Assy (ตอบปัญหา Quality Issue)", ["issue.answer"], False),
    ("viewer", "Viewer (ดูอย่างเดียว)", ["overview.view", "insp.view", "issue.view"], False),
]

DEFAULT_SETTINGS = {
    "general": {
        "app_name": "Daily QC Operation Report", "company": "AHR",
        "shift_day_start": "08:00", "shift_night_start": "20:00",
        "insp_lock_days": 3, "photo_retention_days": 45, "keep_thumb": True, "photo_max_px": 1600,
        "found_at": ["AAPICO", "FTM", "CHANGAN", "GWM", "BYD", "EAC", "GA"],
        "categories": ["Information", "QR", "CR"],
        "auto_local_backup": True, "local_backup_keep": 14, "base_url": "",
    },
    # method: smtp | brevo | resend. The two API methods send over HTTPS (port 443), which also works on
    # free hosts that block the SMTP ports.
    "smtp": {"method": "smtp", "host": "", "port": 587, "security": "starttls", "user": "", "password": "",
             "sender": "", "sender_name": "Daily QC Report", "api_key": ""},
    "line": {"token": "", "to": ""},
    "telegram": {"token": "", "chat_id": ""},
    "discord": {"url": ""},
    "webhook": {"url": ""},
    # roles: send the e-mail to every active user of these roles who has an e-mail address
    # reporter: also to the QC who reported the case
    "events": {
        "issue_new": {"enabled": True, "channels": ["inapp"], "to": "", "cc": "", "roles": ["assy", "manager"]},
        "issue_answered": {"enabled": True, "channels": ["inapp"], "to": "", "cc": "", "roles": ["manager"],
                           "reporter": True},
        "issue_status": {"enabled": True, "channels": ["inapp"], "to": "", "cc": "", "roles": ["assy"],
                         "reporter": False, "closed_only": False},
        "ng_alert": {"enabled": True, "threshold_pct": 5.0, "min_check": 20, "channels": ["inapp"], "to": "", "cc": "",
                     "roles": ["manager"]},
        "photo_expiry": {"enabled": True, "days_before": 7, "channels": ["inapp"], "to": "", "cc": "",
                         "mail_zip": False, "roles": ["admin"]},
    },
}
# (key, Thai title, English title, Thai explanation, English explanation, case-related)
EVENTS = [
    ("issue_new", "มี Quality Issue ใหม่", "A quality issue is reported",
     "แจ้งทันทีที่ QC บันทึกเคส พร้อมรูปแรกของเคส ให้ Assy เข้ามาตอบ", "Sent as soon as QC saves a case, with its first photo, so Assy can answer.", False),
    ("issue_answered", "Assy ตอบปัญหาแล้ว", "Assy answered a case",
     "แจ้งเมื่อ Assy บันทึกหรือแก้ไข Root Cause / Correction / Prevention", "Sent when Assy saves or changes the root cause, correction or prevention.", True),
    ("issue_status", "QC เปลี่ยนสถานะเคส", "QC changed a case status",
     "แจ้งเมื่อเคสเปลี่ยนเป็น กำลังแก้ไข หรือ ปิดแล้ว", "Sent when a case moves to in progress or closed.", True),
    ("ng_alert", "NG + NC สูงเกินเกณฑ์", "NG + NC above the limit",
     "ตรวจทุกครั้งที่ QC บันทึกผลตรวจประจำกะ", "Checked every time QC saves a shift sheet.", False),
    ("photo_expiry", "รูปใกล้ถูกลบตามกำหนด", "Photos about to be removed",
     "เตือนล่วงหน้าให้ดาวน์โหลดรูปเก็บไว้ก่อน", "A reminder to download the photos first.", False),
]
CHANNELS = [("inapp", "ในโปรแกรม"), ("email", "Email"), ("line", "LINE"), ("telegram", "Telegram"),
            ("discord", "Discord"), ("webhook", "Webhook / Teams")]


# ---------------------------------------------------------------- passwords
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
        return hmac.compare_digest(calc, h)
    except Exception:
        return False


# ---------------------------------------------------------------- settings
def get_setting(db: Session, key: str) -> dict:
    base = json.loads(json.dumps(DEFAULT_SETTINGS.get(key, {})))
    row = db.get(Setting, key)
    if row and row.value:
        try:
            saved = json.loads(row.value)
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    base[k].update(v)
                else:
                    base[k] = v
        except ValueError:
            pass
    return base


def set_setting(db: Session, key: str, value: dict):
    row = db.get(Setting, key)
    if not row:
        row = Setting(key=key)
        db.add(row)
    row.value = json.dumps(value, ensure_ascii=False)
    db.commit()


def email_ready(db) -> bool:
    s = get_setting(db, "smtp")
    return bool(s.get("api_key")) if s.get("method") in ("brevo", "resend") else bool(s.get("host"))


def retention_days(db) -> int:
    d = int(get_setting(db, "general").get("photo_retention_days") or config.PHOTO_RETENTION_MAX)
    return max(1, min(d, config.PHOTO_RETENTION_MAX))


# ---------------------------------------------------------------- auth
class NeedLogin(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, perm=""):
        self.perm = perm


def user_perms(user: User) -> set:
    if not user or not user.role:
        return set()
    if user.role.code == "admin":
        return set(ALL_PERMS)
    try:
        return set(json.loads(user.role.perms or "[]"))
    except ValueError:
        return set()


def can(user: User, perm: str) -> bool:
    return perm in user_perms(user)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.active:
        request.session.pop("uid", None)
        raise NeedLogin()
    request.state.user = user
    return user


def require(*perms):
    def dep(user: User = Depends(current_user)) -> User:
        have = user_perms(user)
        if perms and not any(p in have for p in perms):
            raise Forbidden(perms[0])
        return user
    return dep


def allowed_lines(user: User, lines):
    """Lines this user may enter data for."""
    own = user.lines()
    if not own or can(user, "insp.edit_all"):
        return list(lines)
    return [l for l in lines if l.id in own]


def audit(db: Session, user, action: str, detail: str = ""):
    db.add(AuditLog(username=(user.username if user else "system"), action=action, detail=detail[:400]))
    db.commit()


# Highest notice id, kept in memory so browsers can ask "anything new?" without a database query.
NOTICE_STATE = {"latest": 0}


def add_notice(db: Session, title: str, body: str = "", link: str = "", perm: str = "", level: str = "info"):
    n = Notice(title=title[:200], body=body, link=link, perm=perm, level=level)
    db.add(n)
    db.commit()
    NOTICE_STATE["latest"] = max(NOTICE_STATE["latest"], n.id or 0)


def sync_notice_state(db: Session):
    NOTICE_STATE["latest"] = db.query(Notice.id).order_by(Notice.id.desc()).limit(1).scalar() or 0


def flash(request: Request, msg: str, kind: str = "ok"):
    request.session.setdefault("flash", []).append([kind, msg])


# ---------------------------------------------------------------- dates
def shift_now(db) -> tuple:
    """(work_date, shift) for the current moment. Night shift after midnight belongs to the day before."""
    g = get_setting(db, "general")
    n = config.now()
    hm = n.strftime("%H:%M")
    ds, ns = g.get("shift_day_start", "08:00"), g.get("shift_night_start", "20:00")
    if ds <= hm < ns:
        return n.date(), "D"
    return (n.date() if hm >= ns else n.date() - timedelta(days=1)), "N"


def period_range(period: str, ref: date = None):
    t = ref or config.today()
    if period == "today":
        return t, t
    if period == "yesterday":
        return t - timedelta(days=1), t - timedelta(days=1)
    if period == "last7":
        return t - timedelta(days=6), t
    if period == "last30":
        return t - timedelta(days=29), t
    if period == "this_week":
        s = t - timedelta(days=t.weekday())
        return s, s + timedelta(days=6)
    if period == "last_week":
        s = t - timedelta(days=t.weekday() + 7)
        return s, s + timedelta(days=6)
    if period == "last_month":
        e = t.replace(day=1) - timedelta(days=1)
        return e.replace(day=1), e
    if period == "this_year":
        return date(t.year, 1, 1), date(t.year, 12, 31)
    return t.replace(day=1), t.replace(day=calendar.monthrange(t.year, t.month)[1])   # this_month


PERIODS = [("today", "วันนี้", "Today"), ("yesterday", "เมื่อวาน", "Yesterday"), ("last7", "7 วันล่าสุด", "Last 7 days"),
           ("this_week", "สัปดาห์นี้", "This week"), ("last_week", "สัปดาห์ก่อน", "Last week"),
           ("this_month", "เดือนนี้", "This month"), ("last_month", "เดือนก่อน", "Last month"),
           ("last30", "30 วันล่าสุด", "Last 30 days"), ("this_year", "ปีนี้", "This year")]


def pdate(s, default=None):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def parse_range(request: Request, default="this_month"):
    q = request.query_params
    p = q.get("p") or ""
    d1, d2 = pdate(q.get("from")), pdate(q.get("to"))
    if p and p != "custom":
        d1, d2 = period_range(p)
    elif not (d1 and d2):
        p = default
        d1, d2 = period_range(default)
    else:
        p = "custom"
        if d2 < d1:
            d1, d2 = d2, d1
    return d1, d2, p


def pint(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- templates
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
MONTHS_TH = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
MONTHS_EN = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DOW_TH = ["จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา."]


@pass_context
def L(ctx, th, en=None):
    return en if (ctx.get("lang") == "en" and en) else th


def fnum(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "0"


def fpct(a, b, digits=1):
    try:
        return f"{(a / b * 100):.{digits}f}%" if b else "–"
    except Exception:
        return "–"


@pass_context
def fdate(ctx, d):
    if not d:
        return ""
    m = MONTHS_EN if ctx.get("lang") == "en" else MONTHS_TH
    return f"{d.day} {m[d.month]} {d.year}"


def fdt(d):
    return d.strftime("%d/%m/%Y %H:%M") if d else ""


templates.env.globals.update(L=L, PERIODS=PERIODS, CHANNELS=CHANNELS, quote=quote)
templates.env.filters.update(n=fnum, d=fdate, dt=fdt)
templates.env.globals["pct"] = fpct


def notice_visible(perm: str, perms: set) -> bool:
    """A notice's perm may name alternatives: "issue.view|issue.answer"."""
    return not perm or any(p in perms for p in perm.split("|"))


def unread_count(db, user) -> int:
    perms = user_perms(user)
    nr = db.get(NoticeRead, user.id)
    last = nr.last_id if nr else 0
    rows = db.query(Notice.id, Notice.perm).filter(Notice.id > last).order_by(Notice.id.desc()).limit(200).all()
    return sum(1 for r in rows if notice_visible(r.perm, perms))


def home_path(user) -> str:
    """First page this user may open. An Assy account only answers quality issues."""
    have = user_perms(user)
    if have & {"overview.view", "insp.view", "issue.view"}:
        return "/"
    if "insp.entry" in have or "insp.edit_all" in have:
        return "/inspection/entry"
    if "issue.answer" in have or "issue.create" in have:
        return "/issues"
    return "/account"


def render(request: Request, name: str, **ctx):
    user = getattr(request.state, "user", None)
    db = ctx.get("db")
    g = get_setting(db, "general") if db else DEFAULT_SETTINGS["general"]
    base = {
        "request": request, "user": user, "perms": user_perms(user) if user else set(),
        "lang": (user.lang if user else request.session.get("lang", "th")) or "th",
        "flashes": request.session.pop("flash", []), "g": g, "today": config.today(), "now": config.now(),
        "allow_shutdown": config.ALLOW_SHUTDOWN, "is_hosted": config.IS_HOSTED, "version": config.APP_VERSION,
        "unread": unread_count(db, user) if (db and user) else 0, "path": request.url.path,
        "no_db_warning": config.IS_HOSTED and config.IS_SQLITE,
    }
    ctx.pop("db", None)
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)
