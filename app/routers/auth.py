import os
import threading
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import config
from ..core import (NOTICE_STATE, audit, current_user, flash, hash_password, home_path, notice_visible, render, user_perms,
                    verify_password)
from ..db import get_db
from ..models import Notice, NoticeRead, User
from ..services import jobs

router = APIRouter()
_fails = {}


def _blocked(key):
    rec = _fails.get(key)
    return bool(rec and rec[0] >= 8 and time.time() - rec[1] < 300)


@router.get("/login")
def login_page(request: Request, next: str = "", db: Session = Depends(get_db)):
    if request.session.get("uid") and (u := db.get(User, request.session["uid"])):
        return RedirectResponse(home_path(u), 303)
    return render(request, "login.html", db=db, next=next)


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form(""),
          db: Session = Depends(get_db)):
    key = f"{request.client.host if request.client else ''}|{username.lower().strip()}"
    if _blocked(key):
        flash(request, "ใส่รหัสผิดหลายครั้ง กรุณารอ 5 นาทีแล้วลองใหม่", "err")
        return RedirectResponse("/login", 303)
    user = db.query(User).filter(User.username == username.strip().lower()).first()
    if not user or not user.active or not verify_password(password, user.password_hash):
        rec = _fails.get(key, [0, 0])
        _fails[key] = [rec[0] + 1, time.time()]
        flash(request, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "err")
        return RedirectResponse("/login" + (f"?next={next}" if next else ""), 303)
    _fails.pop(key, None)
    request.session.clear()
    request.session["uid"] = user.id
    user.last_login = config.now()
    db.commit()
    audit(db, user, "login")
    if user.must_change:
        flash(request, "กรุณาตั้งรหัสผ่านใหม่ก่อนเริ่มใช้งาน", "warn")
        return RedirectResponse("/account", 303)
    return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else home_path(user), 303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@router.get("/account")
def account(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "account.html", db=db)


@router.post("/account")
def account_save(request: Request, full_name: str = Form(""), email: str = Form(""), lang: str = Form("th"),
                 old_password: str = Form(""), new_password: str = Form(""), new_password2: str = Form(""),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.full_name, user.email = full_name.strip(), email.strip()
    user.lang = "en" if lang == "en" else "th"
    if new_password or user.must_change:
        if not verify_password(old_password, user.password_hash):
            flash(request, "รหัสผ่านเดิมไม่ถูกต้อง", "err")
        elif len(new_password) < 6:
            flash(request, "รหัสผ่านใหม่ต้องยาวอย่างน้อย 6 ตัวอักษร", "err")
        elif new_password != new_password2:
            flash(request, "รหัสผ่านใหม่ทั้งสองช่องไม่ตรงกัน", "err")
        else:
            user.password_hash = hash_password(new_password)
            user.must_change = False
            flash(request, "เปลี่ยนรหัสผ่านแล้ว")
            audit(db, user, "change_password")
    else:
        flash(request, "บันทึกข้อมูลบัญชีแล้ว")
    db.commit()
    return RedirectResponse("/account" if user.must_change else home_path(user), 303)


@router.get("/lang/{code}")
def set_lang(code: str, request: Request, db: Session = Depends(get_db)):
    code = "en" if code == "en" else "th"
    request.session["lang"] = code
    uid = request.session.get("uid")
    if uid and (u := db.get(User, uid)):
        u.lang = code
        db.commit()
    back = request.headers.get("referer") or "/"
    return RedirectResponse(back, 303)


# ---------------------------------------------------------------- notices
@router.get("/api/ping")
def ping(request: Request):
    """Cheap "is there anything new?" check for open browser tabs. It reads nothing from the database,
    so a screen left open all day does not keep a free cloud database awake."""
    if not request.session.get("uid"):
        return JSONResponse({"error": "login"}, status_code=401)
    return {"new": NOTICE_STATE["latest"] > int(request.session.get("seen", -1))}


@router.get("/api/notices")
def notices(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    request.session["seen"] = NOTICE_STATE["latest"]
    perms = user_perms(user)
    nr = db.get(NoticeRead, user.id)
    last = nr.last_id if nr else 0
    since = config.now() - timedelta(days=30)
    rows = db.query(Notice).filter(Notice.created_at >= since).order_by(Notice.id.desc()).limit(120).all()
    rows = [r for r in rows if notice_visible(r.perm, perms)][:40]
    return {"unread": sum(1 for r in rows if r.id > last),
            "items": [dict(id=r.id, level=r.level, title=r.title, body=r.body, link=r.link,
                           at=r.created_at.strftime("%d/%m %H:%M"), new=r.id > last) for r in rows]}


@router.post("/api/notices/read")
def notices_read(user: User = Depends(current_user), db: Session = Depends(get_db)):
    top = db.query(Notice.id).order_by(Notice.id.desc()).limit(1).scalar() or 0
    nr = db.get(NoticeRead, user.id)
    if not nr:
        nr = NoticeRead(user_id=user.id)
        db.add(nr)
    nr.last_id = top
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- system
@router.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


@router.api_route("/cron/tick", methods=["GET", "POST", "HEAD"])
def cron_tick(key: str = "", force: int = 0):
    """For an external pinger on free hosts: wakes the app and runs due schedules.
    Nothing is read from the database unless something is actually due (force=1 re-reads the schedules)."""
    if config.CRON_KEY and key != config.CRON_KEY:
        return JSONResponse({"error": "bad key"}, status_code=403)
    jobs.PING["last"], jobs.PING["count"] = config.now(), jobs.PING["count"] + 1
    return {"ran": jobs.tick(force=bool(force)), "time": config.now().strftime("%Y-%m-%d %H:%M:%S")}


@router.post("/system/shutdown")
def shutdown(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not config.ALLOW_SHUTDOWN or "admin" not in user_perms(user):
        return JSONResponse({"error": "ปิดโปรแกรมได้เฉพาะ Admin และเฉพาะเมื่อรันบนเครื่อง PC"}, status_code=403)
    audit(db, user, "shutdown")
    request.session.clear()
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return render(request, "message.html", db=None, bare=True, title="ปิดโปรแกรมแล้ว",
                  text="เซิร์ฟเวอร์ Daily QC Operation Report หยุดทำงานแล้ว เปิดใหม่ได้ด้วยไฟล์ start.bat", back="")
