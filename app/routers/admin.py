import csv
import io
import json
import os
import tempfile

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .. import config, seed
from ..core import sync_notice_state  # noqa: I001
from ..core import (ALL_PERMS, CHANNELS, DEFAULT_SETTINGS, PERIODS, PERMS, audit, email_ready, flash, get_setting, hash_password, pint,
                    render, require, set_setting)
from ..db import get_db
from ..models import (AuditLog, Customer, DefectType, InspRecord, Issue, IssuePhoto, Line, Part, Role, Schedule, SendLog,
                      User)
from ..services import backup, jobs, notify
from ..services.images import part_thumb

router = APIRouter(prefix="/admin")
ADMIN = require("admin")
MASTER = require("master.manage", "admin")


def back(request, default):
    return RedirectResponse(request.headers.get("referer") or default, 303)


# ================================================================ users
@router.get("/users")
def users(request: Request, edit: int = 0, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    return render(request, "admin/users.html", db=db, users=db.query(User).order_by(User.active.desc(), User.username).all(),
                  roles=db.query(Role).order_by(Role.id).all(), lines=db.query(Line).order_by(Line.line_no).all(),
                  ed=db.get(User, edit) if edit else None, new=request.query_params.get("new"))


@router.post("/users/save")
async def users_save(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    f = await request.form()
    uid = pint(f.get("id"))
    username = (f.get("username") or "").strip().lower()
    if not username:
        flash(request, "กรุณาใส่ชื่อผู้ใช้", "err")
        return back(request, "/admin/users")
    dup = db.query(User).filter(User.username == username, User.id != (uid or 0)).first()
    if dup:
        flash(request, f"ชื่อผู้ใช้ {username} มีอยู่แล้ว", "err")
        return back(request, "/admin/users")
    u = db.get(User, uid) if uid else None
    pw = (f.get("password") or "").strip()
    if not u:
        if len(pw) < 6:
            flash(request, "ผู้ใช้ใหม่ต้องมีรหัสผ่านอย่างน้อย 6 ตัวอักษร", "err")
            return back(request, "/admin/users")
        u = User(username=username, password_hash=hash_password(pw), must_change=bool(f.get("must_change")))
        db.add(u)
    elif pw:
        if len(pw) < 6:
            flash(request, "รหัสผ่านต้องยาวอย่างน้อย 6 ตัวอักษร", "err")
            return back(request, "/admin/users")
        u.password_hash = hash_password(pw)
        u.must_change = bool(f.get("must_change"))
    u.username = username
    u.full_name = (f.get("full_name") or "").strip()
    u.email = (f.get("email") or "").strip()
    role = db.get(Role, pint(f.get("role_id")) or 0)
    active = bool(f.get("active"))
    admins = db.query(User).join(Role).filter(Role.code == "admin", User.active == True, User.id != (u.id or 0)).count()  # noqa: E712
    if u.id and u.role and u.role.code == "admin" and admins == 0 and (not active or (role and role.code != "admin")):
        flash(request, "ต้องมี Admin ที่ใช้งานได้อย่างน้อย 1 คน", "err")
        return back(request, "/admin/users")
    if role:
        u.role_id = role.id
    u.active = active
    u.line_ids = ",".join(x for x in f.getlist("line_ids") if x.isdigit())
    db.commit()
    audit(db, user, "user_save", f"{u.username} role={role.code if role else ''} active={u.active}")
    flash(request, f"บันทึกผู้ใช้ {u.username} แล้ว")
    return RedirectResponse("/admin/users", 303)


@router.post("/users/{uid}/delete")
def users_delete(uid: int, request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u or u.id == user.id:
        flash(request, "ลบบัญชีที่กำลังใช้งานอยู่ไม่ได้", "err")
        return RedirectResponse("/admin/users", 303)
    used = db.query(InspRecord).filter((InspRecord.created_by == uid) | (InspRecord.updated_by == uid)).count() \
        + db.query(Issue).filter(Issue.reported_by == uid).count()
    if used:
        u.active = False
        flash(request, f"{u.username} มีข้อมูลที่เคยบันทึกไว้ จึงปิดการใช้งานแทนการลบ", "warn")
    else:
        db.delete(u)
        flash(request, f"ลบผู้ใช้ {u.username} แล้ว")
    db.commit()
    audit(db, user, "user_delete", u.username)
    return RedirectResponse("/admin/users", 303)


# ================================================================ roles
@router.get("/roles")
def roles(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    rs = db.query(Role).order_by(Role.id).all()
    counts = dict(db.query(User.role_id, func.count(User.id)).group_by(User.role_id).all())
    return render(request, "admin/roles.html", db=db, roles=rs, PERMS=PERMS, counts=counts,
                  rperms={r.id: (set(ALL_PERMS) if r.code == "admin" else set(json.loads(r.perms or "[]"))) for r in rs})


@router.post("/roles/save")
async def roles_save(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    f = await request.form()
    for r in db.query(Role).all():
        if r.code == "admin":
            continue
        r.name = (f.get(f"name_{r.id}") or r.name).strip()
        r.perms = json.dumps([p for p in ALL_PERMS if f.get(f"p_{r.id}_{p}") and p != "admin"])
    code = (f.get("new_code") or "").strip().lower().replace(" ", "_")
    if code:
        if db.query(Role).filter_by(code=code).first():
            flash(request, f"มีกลุ่มสิทธิ์รหัส {code} อยู่แล้ว", "err")
        else:
            db.add(Role(code=code, name=(f.get("new_name") or code).strip(), perms="[]"))
    db.commit()
    audit(db, user, "roles_save")
    flash(request, "บันทึกสิทธิ์แล้ว")
    return RedirectResponse("/admin/roles", 303)


@router.post("/roles/{rid}/delete")
def roles_delete(rid: int, request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    r = db.get(Role, rid)
    if r and not r.is_system and db.query(User).filter(User.role_id == rid).count() == 0:
        db.delete(r)
        db.commit()
        flash(request, "ลบกลุ่มสิทธิ์แล้ว")
    else:
        flash(request, "ลบไม่ได้ ยังมีผู้ใช้อยู่ในกลุ่มนี้ หรือเป็นกลุ่มของระบบ", "err")
    return RedirectResponse("/admin/roles", 303)


# ================================================================ simple masters (customers / lines / defects)
MASTERS = {
    "customers": dict(model=Customer, title=("ลูกค้า", "Customers"), order=[Customer.sort, Customer.id], fields=[
        ("code", "รหัสลูกค้า", "text", 12), ("name", "ชื่อเต็ม / หมายเหตุ", "text", 30), ("color", "สีในกราฟ", "color", 6),
        ("sort", "ลำดับ", "number", 5), ("active", "ใช้งาน", "bool", 4)]),
    "lines": dict(model=Line, title=("Line ผลิต", "Production lines"), order=[Line.line_no, Line.id], fields=[
        ("line_no", "Line No.", "number", 6), ("name", "ชื่อ Line", "text", 18), ("company", "Company", "text", 8),
        ("customer_id", "ลูกค้าหลักของ Line", "customer", 14), ("require_daily", "ต้องส่งข้อมูลทุกวัน", "bool", 6),
        ("active", "ใช้งาน", "bool", 4)]),
    "defects": dict(model=DefectType, title=("อาการเสีย (Defect)", "Defect types"), order=[DefectType.sort, DefectType.id], fields=[
        ("name", "ชื่ออาการเสีย", "text", 34), ("group_name", "กลุ่ม", "text", 16), ("sort", "ลำดับ", "number", 5),
        ("active", "ใช้งาน", "bool", 4)]),
}


@router.get("/master/{kind}")
def master(kind: str, request: Request, user: User = Depends(MASTER), db: Session = Depends(get_db)):
    m = MASTERS.get(kind)
    if not m:
        return RedirectResponse("/admin/master/customers", 303)
    return render(request, "admin/master.html", db=db, kind=kind, m=m, rows=db.query(m["model"]).order_by(*m["order"]).all(),
                  customers=db.query(Customer).order_by(Customer.sort).all())


@router.post("/master/{kind}")
async def master_save(kind: str, request: Request, user: User = Depends(MASTER), db: Session = Depends(get_db)):
    m = MASTERS.get(kind)
    f = await request.form()
    rows = db.query(m["model"]).all()

    def fill(obj, key):
        for name, _, typ, _ in m["fields"]:
            raw = f.get(f"{name}_{key}")
            if typ == "bool":
                setattr(obj, name, bool(raw))
            elif typ in ("number",):
                setattr(obj, name, pint(raw, 0) or 0)
            elif typ == "customer":
                setattr(obj, name, pint(raw))
            else:
                setattr(obj, name, (raw or "").strip())
    for r in rows:
        if f.get(f"row_{r.id}"):
            fill(r, r.id)
    first = m["fields"][0][0] if kind != "lines" else "name"
    if (f.get(f"{first}_new") or "").strip():
        obj = m["model"]()
        fill(obj, "new")
        if hasattr(obj, "active"):
            obj.active = True
        db.add(obj)
    try:
        db.commit()
        flash(request, "บันทึกแล้ว")
        audit(db, user, "master_save", kind)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        flash(request, f"บันทึกไม่ได้ ข้อมูลซ้ำหรือไม่ครบ ({str(e)[:80]})", "err")
    return RedirectResponse(f"/admin/master/{kind}", 303)


@router.post("/master/{kind}/{rid}/delete")
def master_delete(kind: str, rid: int, request: Request, user: User = Depends(MASTER), db: Session = Depends(get_db)):
    m = MASTERS.get(kind)
    obj = db.get(m["model"], rid)
    if obj:
        try:
            db.delete(obj)
            db.commit()
            flash(request, "ลบแล้ว")
        except Exception:  # noqa: BLE001
            db.rollback()
            obj = db.get(m["model"], rid)
            obj.active = False
            db.commit()
            flash(request, "รายการนี้มีข้อมูลอ้างอิงอยู่ จึงปิดการใช้งานแทนการลบ", "warn")
    return RedirectResponse(f"/admin/master/{kind}", 303)


# ================================================================ parts
@router.get("/parts")
def parts(request: Request, customer: int = 0, edit: int = 0, q: str = "", user: User = Depends(MASTER),
          db: Session = Depends(get_db)):
    customers = db.query(Customer).order_by(Customer.sort).all()
    cid = customer or (customers[0].id if customers else 0)
    qq = db.query(Part).filter(Part.customer_id == cid)
    if q:
        qq = qq.filter(Part.part_no.ilike(f"%{q}%") | Part.part_name.ilike(f"%{q}%"))
    return render(request, "admin/parts.html", db=db, customers=customers, cid=cid, q=q,
                  parts=qq.order_by(Part.sort, Part.id).all(), ed=db.get(Part, edit) if edit else None,
                  new=request.query_params.get("new"), lines=db.query(Line).order_by(Line.line_no).all(),
                  counts=dict(db.query(Part.customer_id, func.count(Part.id)).group_by(Part.customer_id).all()))


@router.post("/parts/save")
async def parts_save(request: Request, image: UploadFile = File(None), user: User = Depends(MASTER),
                     db: Session = Depends(get_db)):
    f = await request.form()
    p = db.get(Part, pint(f.get("id")) or 0)
    if not p:
        p = Part()
        db.add(p)
    p.customer_id = pint(f.get("customer_id"))
    p.part_no = (f.get("part_no") or "").strip()
    if not p.customer_id or not p.part_no:
        db.rollback()
        flash(request, "ต้องระบุลูกค้าและ Part No.", "err")
        return back(request, "/admin/parts")
    p.part_name = (f.get("part_name") or "").strip()
    p.model = (f.get("model") or "").strip()
    p.variant = (f.get("variant") or "").strip()
    p.process = (f.get("process") or "").strip()
    p.line_id = pint(f.get("line_id"))
    p.sort = pint(f.get("sort"), 0) or 0
    p.active = bool(f.get("active"))
    if image is not None and image.filename:
        try:
            p.image = part_thumb(await image.read())
            p.has_image = True
            p.image_ver = (p.image_ver or 0) + 1
        except ValueError as e:
            flash(request, str(e), "err")
    elif f.get("remove_image"):
        p.image, p.has_image = None, False
    db.commit()
    audit(db, user, "part_save", p.part_no)
    flash(request, f"บันทึก Part {p.part_no} แล้ว")
    return RedirectResponse(f"/admin/parts?customer={p.customer_id}", 303)


@router.post("/parts/{pid}/delete")
def parts_delete(pid: int, request: Request, user: User = Depends(MASTER), db: Session = Depends(get_db)):
    p = db.get(Part, pid)
    if p:
        cid = p.customer_id
        used = db.query(InspRecord).filter(InspRecord.part_id == pid).count() + db.query(Issue).filter(Issue.part_id == pid).count()
        if used:
            p.active = False
            flash(request, "Part นี้มีข้อมูลผลตรวจอยู่ จึงปิดการใช้งานแทนการลบ", "warn")
        else:
            db.delete(p)
            flash(request, "ลบ Part แล้ว")
        db.commit()
        return RedirectResponse(f"/admin/parts?customer={cid}", 303)
    return RedirectResponse("/admin/parts", 303)


@router.get("/parts/export.csv")
def parts_export(user: User = Depends(MASTER), db: Session = Depends(get_db)):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["customer", "process", "part_no", "part_name", "model", "variant", "line", "active"])
    lines = {l.id: l.name for l in db.query(Line).all()}
    for p in db.query(Part).join(Customer).order_by(Customer.sort, Part.sort, Part.id).all():
        w.writerow([p.customer.code, p.process, p.part_no, p.part_name, p.model, p.variant, lines.get(p.line_id, ""),
                    1 if p.active else 0])
    return Response("\ufeff" + out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="parts.csv"'})


@router.post("/parts/import")
async def parts_import(request: Request, file: UploadFile = File(...), user: User = Depends(MASTER),
                       db: Session = Depends(get_db)):
    raw = await file.read()
    rows = []
    try:
        if file.filename.lower().endswith((".xlsx", ".xlsm")):
            import openpyxl
            ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
            it = ws.iter_rows(values_only=True)
            head = [str(h or "").strip().lower() for h in next(it)]
            rows = [dict(zip(head, ["" if v is None else str(v).strip() for v in r])) for r in it]
        else:
            txt = raw.decode("utf-8-sig", errors="replace")
            rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(io.StringIO(txt))]
    except Exception as e:  # noqa: BLE001
        flash(request, f"อ่านไฟล์ไม่ได้: {e}", "err")
        return RedirectResponse("/admin/parts", 303)
    cust = {c.code.lower(): c for c in db.query(Customer).all()}
    lines = {l.name.lower(): l.id for l in db.query(Line).all()}
    add = upd = skip = 0
    for r in rows:
        c = cust.get((r.get("customer") or "").lower())
        pn = r.get("part_no") or r.get("part no.") or r.get("part no") or ""
        if not c or not pn:
            skip += 1
            continue
        p = db.query(Part).filter(Part.customer_id == c.id, Part.part_no == pn, Part.process == (r.get("process") or "")).first()
        if not p:
            p = Part(customer_id=c.id, part_no=pn, sort=900 + add)
            db.add(p)
            add += 1
        else:
            upd += 1
        p.process = r.get("process") or ""
        p.part_name = r.get("part_name") or r.get("part name") or p.part_name or ""
        p.model = r.get("model") or p.model or ""
        p.variant = r.get("variant") or p.variant or ""
        p.line_id = lines.get((r.get("line") or "").lower())
        if r.get("active") not in (None, ""):
            p.active = r.get("active") not in ("0", "false", "no", "n")
    db.commit()
    audit(db, user, "parts_import", f"add {add} update {upd} skip {skip}")
    flash(request, f"นำเข้า Part: เพิ่มใหม่ {add} รายการ, ปรับปรุง {upd} รายการ" + (f", ข้าม {skip} แถวที่ไม่มีลูกค้าหรือ Part No." if skip else ""))
    return RedirectResponse("/admin/parts", 303)


# ================================================================ settings
@router.get("/settings")
def settings(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    return render(request, "admin/settings.html", db=db, s=get_setting(db, "general"), max_keep=config.PHOTO_RETENTION_MAX)


@router.post("/settings")
async def settings_save(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    f = await request.form()
    s = get_setting(db, "general")
    for k in ("app_name", "company", "shift_day_start", "shift_night_start", "base_url"):
        s[k] = (f.get(k) or "").strip() or DEFAULT_SETTINGS["general"][k]
    s["base_url"] = (f.get("base_url") or "").strip().rstrip("/")
    s["insp_lock_days"] = max(0, pint(f.get("insp_lock_days"), 3) or 0)
    s["photo_retention_days"] = max(1, min(config.PHOTO_RETENTION_MAX, pint(f.get("photo_retention_days"), 45) or 45))
    s["photo_max_px"] = max(800, min(2400, pint(f.get("photo_max_px"), 1600) or 1600))
    s["keep_thumb"] = bool(f.get("keep_thumb"))
    s["auto_local_backup"] = bool(f.get("auto_local_backup"))
    s["local_backup_keep"] = max(1, pint(f.get("local_backup_keep"), 14) or 14)
    s["found_at"] = [x.strip() for x in (f.get("found_at") or "").replace("\n", ",").split(",") if x.strip()]
    s["categories"] = [x.strip() for x in (f.get("categories") or "").replace("\n", ",").split(",") if x.strip()] or ["Information"]
    set_setting(db, "general", s)
    audit(db, user, "settings_save")
    flash(request, "บันทึกการตั้งค่าแล้ว")
    return RedirectResponse("/admin/settings", 303)


# ================================================================ notifications
@router.get("/notify")
def notify_page(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    return render(request, "admin/notify.html", db=db, smtp=get_setting(db, "smtp"), line=get_setting(db, "line"),
                  tg=get_setting(db, "telegram"), dc=get_setting(db, "discord"), wh=get_setting(db, "webhook"),
                  ev=get_setting(db, "events"), email_ok=email_ready(db),
                  logs=db.query(SendLog).order_by(SendLog.id.desc()).limit(15).all())


@router.post("/notify")
async def notify_save(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    f = await request.form()
    g = lambda k: (f.get(k) or "").strip()  # noqa: E731
    smtp = get_setting(db, "smtp")
    smtp.update(method=g("smtp_method") if g("smtp_method") in ("smtp", "brevo", "resend") else "smtp",
                host=g("smtp_host"), port=pint(g("smtp_port"), 587) or 587, security=g("smtp_security") or "starttls",
                user=g("smtp_user"), sender=g("smtp_sender"), sender_name=g("smtp_sender_name") or "Daily QC Report")
    if g("smtp_password"):
        smtp["password"] = g("smtp_password")
    if g("smtp_api_key"):
        smtp["api_key"] = g("smtp_api_key")
    set_setting(db, "smtp", smtp)
    for key, fields in (("line", ("token", "to")), ("telegram", ("token", "chat_id")), ("discord", ("url",)), ("webhook", ("url",))):
        cur = get_setting(db, key)
        for fld in fields:
            v = g(f"{key}_{fld}")
            if fld == "token" and not v:
                continue                         # leave a saved secret untouched when the box is left empty
            cur[fld] = v
        set_setting(db, key, cur)
    ev = get_setting(db, "events")
    for name in ("issue_new", "ng_alert", "photo_expiry"):
        ev[name]["enabled"] = bool(f.get(f"{name}_enabled"))
        ev[name]["channels"] = [c for c, _ in CHANNELS if f.get(f"{name}_ch_{c}")]
        ev[name]["to"], ev[name]["cc"] = g(f"{name}_to"), g(f"{name}_cc")
    try:
        ev["ng_alert"]["threshold_pct"] = float(g("ng_alert_threshold") or 5)
    except ValueError:
        pass
    ev["ng_alert"]["min_check"] = pint(g("ng_alert_min"), 20) or 0
    ev["photo_expiry"]["days_before"] = max(1, min(30, pint(g("photo_expiry_days"), 7) or 7))
    ev["photo_expiry"]["mail_zip"] = bool(f.get("photo_expiry_mail_zip"))
    set_setting(db, "events", ev)
    audit(db, user, "notify_save")
    flash(request, "บันทึกการแจ้งเตือนแล้ว")
    return RedirectResponse("/admin/notify", 303)


@router.post("/notify/test/{channel}")
def notify_test(channel: str, request: Request, to: str = Form(""), user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    res = notify.dispatch(db, "test", [channel], "ทดสอบการแจ้งเตือน Daily QC Operation Report",
                          f"ข้อความทดสอบ ส่งโดย {user.username} เวลา {config.now():%d/%m/%Y %H:%M}", to=to or user.email,
                          link="/", perm="admin")
    ok, detail = res.get(channel, (False, "ไม่รู้จักช่องทางนี้"))
    flash(request, f"ส่งทดสอบทาง {channel} สำเร็จ" if ok else f"ส่งทาง {channel} ไม่สำเร็จ: {detail}", "ok" if ok else "err")
    return RedirectResponse("/admin/notify", 303)


# ================================================================ schedules
@router.get("/schedules")
def schedules(request: Request, edit: int = 0, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    return render(request, "admin/schedules.html", db=db, rows=db.query(Schedule).order_by(Schedule.id).all(),
                  ed=db.get(Schedule, edit) if edit else None, new=request.query_params.get("new"),
                  smtp_ready=email_ready(db), maint=get_setting(db, "maint"),
                  cron_key=config.CRON_KEY)


@router.post("/schedules/save")
async def schedules_save(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    f = await request.form()
    s = db.get(Schedule, pint(f.get("id")) or 0)
    if not s:
        s = Schedule()
        db.add(s)
    s.name = (f.get("name") or "").strip() or "รายงานอัตโนมัติ"
    s.kind = "missing" if f.get("kind") == "missing" else "report"
    s.include_insp, s.include_issue = bool(f.get("include_insp")), bool(f.get("include_issue"))
    s.period = f.get("period") if f.get("period") in [p[0] for p in PERIODS] else "yesterday"
    s.freq = f.get("freq") if f.get("freq") in ("daily", "weekly", "monthly") else "daily"
    s.weekdays = ",".join(x for x in f.getlist("weekdays") if x in "0123456")
    s.monthday = max(1, min(28, pint(f.get("monthday"), 1) or 1))
    t = (f.get("at_time") or "08:00")[:5]
    s.at_time = t if len(t) == 5 and t[2] == ":" else "08:00"
    s.shift = f.get("shift") if f.get("shift") in ("D", "N") else "D"
    s.to_addr, s.cc_addr = (f.get("to_addr") or "").strip(), (f.get("cc_addr") or "").strip()
    s.channels = ",".join(c for c, _ in CHANNELS if f.get(f"ch_{c}"))
    s.attach_excel, s.with_photos, s.enabled = bool(f.get("attach_excel")), bool(f.get("with_photos")), bool(f.get("enabled"))
    s.last_slot = config.now().strftime("%Y-%m-%d %H:%M")      # never fire for a time that has already passed today
    db.commit()
    jobs.invalidate()
    audit(db, user, "schedule_save", s.name)
    flash(request, f"บันทึกกำหนดการ “{s.name}” แล้ว")
    return RedirectResponse("/admin/schedules", 303)


@router.post("/schedules/{sid}/run")
def schedules_run(sid: int, request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    s = db.get(Schedule, sid)
    if s:
        try:
            res = jobs.run_schedule(db, s, manual=True)
            s.last_result = f"{config.now():%d/%m %H:%M} (ส่งเอง) {res}"[:250]
            db.commit()
            flash(request, f"ส่ง “{s.name}” แล้ว: {res}", "ok" if "FAIL" not in res else "err")
        except Exception as e:  # noqa: BLE001
            flash(request, f"ส่งไม่สำเร็จ: {e}", "err")
    return RedirectResponse("/admin/schedules", 303)


@router.post("/schedules/{sid}/delete")
def schedules_delete(sid: int, request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    s = db.get(Schedule, sid)
    if s:
        db.delete(s)
        db.commit()
        jobs.invalidate()
        flash(request, "ลบกำหนดการแล้ว")
    return RedirectResponse("/admin/schedules", 303)


# ================================================================ backup
@router.get("/backup")
def backup_page(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    local = []
    if config.BACKUP_DIR.exists():
        local = [dict(name=p.name, size=p.stat().st_size) for p in sorted(config.BACKUP_DIR.glob("*.zip"), reverse=True)]
    counts = dict(insp=db.query(InspRecord).count(), issues=db.query(Issue).count(),
                  demo=db.query(InspRecord).filter(InspRecord.is_demo == True).count()  # noqa: E712
                  + db.query(Issue).filter(Issue.is_demo == True).count(),  # noqa: E712
                  thumbs=db.query(IssuePhoto).filter(IssuePhoto.purged_at.isnot(None)).count())
    return render(request, "admin/backup.html", db=db, ps=backup.photo_stats(db), dbsize=backup.db_size(db), local=local,
                  counts=counts, is_sqlite=config.IS_SQLITE, s=get_setting(db, "general"), maint=get_setting(db, "maint"))


def _send_tmp(path, name):
    return FileResponse(path, filename=name, media_type="application/zip", background=BackgroundTask(os.unlink, path))


@router.get("/backup/full.zip")
def backup_full(photos: int = 1, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    path = backup.backup_tempfile(db, include_photos=bool(photos))
    audit(db, user, "backup_download", f"photos={photos}")
    return _send_tmp(path, f"dailyqc_backup_{config.now():%Y%m%d_%H%M}{'' if photos else '_text'}.zip")


@router.get("/backup/photos.zip")
def backup_photos(soon: int = 0, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    st = backup.photo_stats(db)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    backup.photos_zip(db, tmp, older_than_days=(st["keep"] - st["soon_days"]) if soon else None)
    tmp.close()
    audit(db, user, "photos_download", f"soon={soon}")
    return _send_tmp(tmp.name, f"dailyqc_photos_{'expiring_' if soon else ''}{config.now():%Y%m%d}.zip")


@router.get("/backup/local/{name}")
def backup_local(name: str, user: User = Depends(ADMIN)):
    p = config.BACKUP_DIR / os.path.basename(name)
    if not p.exists():
        return RedirectResponse("/admin/backup", 303)
    return FileResponse(p, filename=p.name, media_type="application/zip")


@router.post("/backup/restore")
async def backup_restore(request: Request, file: UploadFile = File(...), confirm: str = Form(""),
                         user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    if confirm.strip().upper() != "RESTORE":
        flash(request, "ยังไม่ได้พิมพ์คำว่า RESTORE เพื่อยืนยัน จึงยังไม่ได้กู้คืน", "err")
        return RedirectResponse("/admin/backup", 303)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    while chunk := await file.read(1 << 20):
        tmp.write(chunk)
    tmp.close()
    try:
        counts = backup.restore_backup(db, tmp.name)
        jobs.invalidate()
        sync_notice_state(db)
        flash(request, f"กู้คืนข้อมูลแล้ว: ผลตรวจ {counts.get('insp_records', 0)} รายการ, เคส {counts.get('issues', 0)} เคส, "
                       f"รูป {counts.get('issue_photos', 0)} รูป กรุณา Login ใหม่")
        request.session.clear()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        flash(request, f"กู้คืนไม่สำเร็จ ข้อมูลเดิมยังอยู่ครบ ({str(e)[:160]})", "err")
    finally:
        os.unlink(tmp.name)
    return RedirectResponse("/login", 303)


@router.post("/backup/purge")
def backup_purge(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    n = backup.purge_photos(db)
    audit(db, user, "purge_photos", str(n))
    flash(request, f"ลบรูปที่เกินกำหนดแล้ว {n} รูป" if n else "ยังไม่มีรูปที่เกินกำหนดเก็บ")
    return RedirectResponse("/admin/backup", 303)


@router.post("/backup/local-now")
def backup_local_now(request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    name = backup.local_auto_backup(db)
    flash(request, f"สร้างไฟล์สำรอง {name} ในโฟลเดอร์ data/backups แล้ว" if name else "ใช้ได้เฉพาะเมื่อรันบนเครื่อง PC และเปิดการสำรองอัตโนมัติไว้", "ok" if name else "warn")
    return RedirectResponse("/admin/backup", 303)


@router.post("/demo/{action}")
def demo(action: str, request: Request, user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    if action == "load":
        n, m = seed.load_demo(db)
        flash(request, f"ใส่ข้อมูลตัวอย่างแล้ว: ผลตรวจ {n} รายการ, Quality Issue {m} เคส "
                       f"(บัญชีทดสอบ demo_qc / demo_sup / demo_asm รหัส demo1234)")
    else:
        n, m = seed.remove_demo(db)
        flash(request, f"ลบข้อมูลตัวอย่างแล้ว: ผลตรวจ {n} รายการ, เคส {m} เคส")
    audit(db, user, f"demo_{action}")
    return RedirectResponse("/admin/backup", 303)


# ================================================================ logs
@router.get("/logs")
def logs(request: Request, tab: str = "audit", user: User = Depends(ADMIN), db: Session = Depends(get_db)):
    rows = (db.query(SendLog).order_by(SendLog.id.desc()) if tab == "send" else db.query(AuditLog).order_by(AuditLog.id.desc())).limit(300).all()
    return render(request, "admin/logs.html", db=db, rows=rows, tab=tab)
