from typing import List

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session, joinedload, undefer

from .. import config
from ..core import (audit, can, current_user, flash, get_setting, parse_range, pdate, pint, render, require,
                    retention_days, shift_now)
from ..db import get_db
from ..models import Customer, DefectType, Issue, IssuePhoto, Line, Part, User
from ..services import excel, jobs, reports
from ..services.images import process_photo

router = APIRouter()
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_PHOTOS = 8
PLACEHOLDER = (b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="180" viewBox="0 0 240 180">'
               b'<rect width="240" height="180" fill="#EDF0F3"/><path d="M80 118l28-34 22 26 14-16 20 24z" fill="#C3CAD5"/>'
               b'<circle cx="96" cy="66" r="10" fill="#C3CAD5"/></svg>')


def _lists(db):
    g = get_setting(db, "general")
    return dict(
        customers=db.query(Customer).filter(Customer.active == True).order_by(Customer.sort, Customer.id).all(),  # noqa: E712
        lines=db.query(Line).filter(Line.active == True).order_by(Line.line_no).all(),  # noqa: E712
        defects=db.query(DefectType).filter(DefectType.active == True).order_by(DefectType.sort, DefectType.id).all(),  # noqa: E712
        categories=g.get("categories") or ["Information"], found_ats=g.get("found_at") or [])


def _filters(request):
    q = request.query_params
    return dict(customer_id=pint(q.get("customer")), source=q.get("source") or None, category=q.get("category") or None,
                status=q.get("status") or None, line_id=pint(q.get("line")), text=(q.get("q") or "").strip() or None)


def can_edit(user, it: Issue):
    """Change the case itself (part, problem, photos, QC action)."""
    return can(user, "issue.edit_all") or (can(user, "issue.create") and it.reported_by == user.id and it.status != "closed")


def can_answer(user, it: Issue):
    """Fill in the Assy answer: root cause, correction, plan / actual date, responsible, prevention.
    Once QC has closed the case the answer is frozen, except for users who may edit every case."""
    return can(user, "issue.edit_all") or (can(user, "issue.answer") and it.status != "closed")


def can_status(user):
    """Only QC changes the status of a case."""
    return can(user, "issue.status") or can(user, "issue.edit_all")


VIEW_CASES = require("issue.view", "issue.answer")      # list and case page: QC and Assy


def _next_case_no(db, d):
    prefix = f"QI-{d:%y%m}-"
    last = db.query(Issue.case_no).filter(Issue.case_no.like(prefix + "%")).order_by(Issue.case_no.desc()).first()
    n = (int(last[0].split("-")[-1]) + 1) if last else 1
    return f"{prefix}{n:03d}"


async def _add_photos(db, it, files, tags):
    max_px = int(get_setting(db, "general").get("photo_max_px") or 1600)
    have = db.query(IssuePhoto).filter(IssuePhoto.issue_id == it.id).count() if it.id else 0
    added, bad = 0, 0
    for i, f in enumerate(files or []):
        if not f or not f.filename:
            continue
        if have + added >= MAX_PHOTOS:
            break
        raw = await f.read()
        if not raw:
            continue
        try:
            full, th, w, h = process_photo(raw, max_px)
        except ValueError:
            bad += 1
            continue
        tag = tags[i] if i < len(tags) and tags[i] in ("NG", "OK") else ""
        it.photos.append(IssuePhoto(data=full, thumb=th, size=len(full), width=w, height=h, tag=tag))
        added += 1
    return added, bad


# ---------------------------------------------------------------- list
@router.get("/issues")
def issue_list(request: Request, page: int = 1, user: User = Depends(VIEW_CASES), db: Session = Depends(get_db)):
    d1, d2, p = parse_range(request, "this_month")
    f = _filters(request)
    q = reports.issue_query(db, d1, d2, **f)
    ans = request.query_params.get("answer") or ""
    no_answer = (Issue.root_cause.is_(None)) | (Issue.root_cause == "")
    if ans == "pending":
        q = q.filter(no_answer)
    elif ans == "done":
        q = q.filter(~no_answer)
    total = q.count()
    per = 30
    rows = q.options(joinedload(Issue.customer), joinedload(Issue.line), joinedload(Issue.photos), joinedload(Issue.reporter)) \
        .order_by(Issue.found_date.desc(), Issue.id.desc()).offset((max(1, page) - 1) * per).limit(per).all()
    return render(request, "issue/list.html", db=db, rows=rows, total=total, page=max(1, page), pages=(total + per - 1) // per,
                  d1=d1, d2=d2, p=p, f=f, ans=ans, **_lists(db))


# ---------------------------------------------------------------- create / edit
@router.get("/issues/new")
def issue_new(request: Request, user: User = Depends(require("issue.create")), db: Session = Depends(get_db)):
    wd, sh = shift_now(db)
    own = user.lines()
    it = Issue(found_date=wd, shift=sh, source="internal", category="Information", ng_qty=1, status="open",
               line_id=own[0] if own else None, found_at="AAPICO")
    return render(request, "issue/form.html", db=db, it=it, is_new=True, max_photos=MAX_PHOTOS,
                  keep_days=retention_days(db), **_lists(db))


def _apply(db, it, form, user, is_new):
    """Each group of fields is written only by the people allowed to change it,
    whatever the browser sends."""
    g = lambda k, d="": (form.get(k) or d).strip()  # noqa: E731
    if is_new or can_edit(user, it):
        it.source = "external" if g("source") == "external" else "internal"
        it.category = g("category", "Information")
        it.customer_id = pint(g("customer_id"))
        it.line_id = pint(g("line_id"))
        part = db.get(Part, pint(g("part_id")) or 0)
        it.part_id = part.id if part else None
        it.part_no = g("part_no") or (part.part_no if part else "")
        it.part_name = g("part_name") or (part.part_name if part else "")
        it.model = g("model") or (part.model if part else "")
        if part and not it.customer_id:
            it.customer_id = part.customer_id
        it.defect_id = pint(g("defect_id"))
        it.problem = g("problem")
        if not it.problem and it.defect_id:
            it.problem = db.get(DefectType, it.defect_id).name
        it.found_date = pdate(g("found_date"), it.found_date or config.today())
        it.shift = g("shift") if g("shift") in ("D", "N") else ""
        it.found_at = g("found_at")
        it.ng_qty = max(0, pint(g("ng_qty"), 0) or 0)
        it.lot_no = g("lot_no")
        if "action" in form:
            it.action = g("action")
    if not is_new and "root_cause" in form and can_answer(user, it):
        it.root_cause, it.correction, it.prevention = g("root_cause"), g("correction"), g("prevention")
        it.plan_date, it.actual_date = pdate(g("plan_date")), pdate(g("actual_date"))
        it.responsible = g("responsible")
    if not is_new and "status" in form and can_status(user):
        st = g("status", it.status or "open")
        if st in ("open", "in_progress", "closed") and st != it.status:
            it.status = st
            it.closed_at = config.now() if st == "closed" else None
    it.updated_at = config.now()


@router.post("/issues/new")
async def issue_create(request: Request, photos: List[UploadFile] = File(default=[]),
                       user: User = Depends(require("issue.create")), db: Session = Depends(get_db)):
    form = await request.form()
    it = Issue(reported_by=user.id, status="open", found_date=config.today())
    _apply(db, it, form, user, True)
    if not it.customer_id or not (it.part_no or it.problem):
        flash(request, "กรุณาเลือกลูกค้า และระบุ Part หรือรายละเอียดปัญหา", "err")
        return RedirectResponse("/issues/new", 303)
    it.case_no = _next_case_no(db, it.found_date)
    added, bad = await _add_photos(db, it, photos, form.getlist("tags"))
    db.add(it)
    db.commit()
    audit(db, user, "issue_create", f"{it.case_no} {it.part_no} photos={added}")
    flash(request, f"บันทึกเคส {it.case_no} แล้ว" + (f" (แนบรูป {added} รูป)" if added else " (ยังไม่มีรูปแนบ)"),
          "ok" if added else "warn")
    if bad:
        flash(request, f"มี {bad} ไฟล์ที่ไม่ใช่รูปภาพ จึงไม่ได้แนบ", "warn")
    jobs.run_bg(jobs.issue_created, it.id)
    return RedirectResponse(f"/issues/{it.id}", 303)


@router.get("/issues/report")
def issue_report(request: Request, user: User = Depends(require("issue.view")), db: Session = Depends(get_db)):
    d1, d2, p = parse_range(request, "this_week")
    f = _filters(request)
    st = reports.issue_stats(db, d1, d2, **f)
    rows = reports.issue_query(db, d1, d2, **f).options(
        joinedload(Issue.customer), joinedload(Issue.line), joinedload(Issue.photos), joinedload(Issue.defect)) \
        .order_by(Issue.found_date, Issue.id).all()
    return render(request, "issue/report.html", db=db, rows=rows, st=st, d1=d1, d2=d2, p=p, f=f,
                  label=excel.period_label(d1, d2), **_lists(db))


@router.get("/issues/charts")
def issue_charts(request: Request, user: User = Depends(require("issue.view")), db: Session = Depends(get_db)):
    d1, d2, p = parse_range(request, "this_month")
    f = _filters(request)
    st = reports.issue_stats(db, d1, d2, group=request.query_params.get("group") or "auto", **f)
    return render(request, "issue/charts.html", db=db, st=st, d1=d1, d2=d2, p=p, f=f,
                  group=request.query_params.get("group") or "auto", **_lists(db))


@router.get("/issues/export.xlsx")
def issue_export(request: Request, photos: int = 1, user: User = Depends(require("issue.export")),
                 db: Session = Depends(get_db)):
    d1, d2, _ = parse_range(request, "this_month")
    data = excel.export_issues(db, d1, d2, with_photos=bool(photos), **_filters(request))
    audit(db, user, "issue_export", f"{d1} - {d2} photos={photos}")
    name = f"QC_Quality_Issue_{d1:%Y%m%d}-{d2:%Y%m%d}.xlsx"
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/issues/{iid}")
def issue_detail(iid: int, request: Request, user: User = Depends(VIEW_CASES), db: Session = Depends(get_db)):
    it = db.get(Issue, iid)
    if not it:
        return render(request, "message.html", db=db, title="ไม่พบเคสนี้", text="เคสอาจถูกลบไปแล้ว", back="/issues")
    return render(request, "issue/detail.html", db=db, it=it, editable=can_edit(user, it), answerable=can_answer(user, it),
                  status_ok=can_status(user), keep_days=retention_days(db), max_photos=MAX_PHOTOS)


@router.get("/issues/{iid}/edit")
def issue_edit(iid: int, request: Request, user: User = Depends(VIEW_CASES), db: Session = Depends(get_db)):
    it = db.get(Issue, iid)
    if not it or not (can_edit(user, it) or can_answer(user, it) or can_status(user)):
        flash(request, "ไม่มีสิทธิ์แก้ไขเคสนี้", "err")
        return RedirectResponse(f"/issues/{iid}" if it else "/issues", 303)
    return render(request, "issue/form.html", db=db, it=it, is_new=False, max_photos=MAX_PHOTOS,
                  keep_days=retention_days(db), full_edit=can_edit(user, it), answerable=can_answer(user, it),
                  status_ok=can_status(user), **_lists(db))


@router.post("/issues/{iid}/edit")
async def issue_update(iid: int, request: Request, photos: List[UploadFile] = File(default=[]),
                       user: User = Depends(VIEW_CASES), db: Session = Depends(get_db)):
    it = db.get(Issue, iid)
    if not it or not (can_edit(user, it) or can_answer(user, it) or can_status(user)):
        flash(request, "ไม่มีสิทธิ์แก้ไขเคสนี้", "err")
        return RedirectResponse("/issues", 303)
    form = await request.form()
    ans_keys = ("root_cause", "correction", "prevention", "responsible", "plan_date", "actual_date")
    before = tuple(str(getattr(it, k) or "") for k in ans_keys)
    old_status = it.status
    _apply(db, it, form, user, False)
    added, bad = (await _add_photos(db, it, photos, form.getlist("tags"))) if can_edit(user, it) else (0, 0)
    db.commit()
    if tuple(str(getattr(it, k) or "") for k in ans_keys) != before:
        jobs.run_bg(jobs.issue_answered, it.id, user.id)
    if it.status != old_status:
        jobs.run_bg(jobs.issue_status_changed, it.id, old_status, it.status, user.id)
    audit(db, user, "issue_update", f"{it.case_no} status={it.status}")
    flash(request, f"บันทึกเคส {it.case_no} แล้ว" + (f" เพิ่มรูป {added} รูป" if added else ""))
    return RedirectResponse(f"/issues/{iid}", 303)


@router.post("/issues/{iid}/status")
def issue_status(iid: int, request: Request, status: str = Form(...), user: User = Depends(require("issue.status", "issue.edit_all")),
                 db: Session = Depends(get_db)):
    it = db.get(Issue, iid)
    if it and status in ("open", "in_progress", "closed") and status != it.status:
        old_status = it.status
        it.status = status
        it.closed_at = config.now() if status == "closed" else None
        it.updated_at = config.now()
        db.commit()
        audit(db, user, "issue_status", f"{it.case_no} -> {status}")
        jobs.run_bg(jobs.issue_status_changed, it.id, old_status, status, user.id)
        flash(request, "เปลี่ยนสถานะเคสแล้ว")
    return RedirectResponse(f"/issues/{iid}", 303)


@router.post("/issues/{iid}/delete")
def issue_delete(iid: int, request: Request, user: User = Depends(require("issue.create", "issue.edit_all")),
                 db: Session = Depends(get_db)):
    it = db.get(Issue, iid)
    if it and can_edit(user, it):
        audit(db, user, "issue_delete", f"{it.case_no} {it.part_no}")
        db.delete(it)
        db.commit()
        flash(request, "ลบเคสแล้ว")
        return RedirectResponse("/issues", 303)
    flash(request, "ไม่มีสิทธิ์ลบเคสนี้", "err")
    return RedirectResponse(f"/issues/{iid}", 303)


@router.post("/issues/photo/{pid}/delete")
def photo_delete(pid: int, request: Request, user: User = Depends(require("issue.create", "issue.edit_all")),
                 db: Session = Depends(get_db)):
    ph = db.get(IssuePhoto, pid)
    if ph and can_edit(user, ph.issue):
        iid = ph.issue_id
        db.delete(ph)
        db.commit()
        flash(request, "ลบรูปแล้ว")
        return RedirectResponse(f"/issues/{iid}", 303)
    flash(request, "ไม่มีสิทธิ์ลบรูปนี้", "err")
    return RedirectResponse("/issues", 303)


@router.post("/issues/photo/{pid}/tag")
def photo_tag(pid: int, request: Request, tag: str = Form(""), user: User = Depends(require("issue.create", "issue.edit_all")),
              db: Session = Depends(get_db)):
    ph = db.get(IssuePhoto, pid)
    if ph and can_edit(user, ph.issue):
        ph.tag = tag if tag in ("NG", "OK") else ""
        db.commit()
    return RedirectResponse(f"/issues/{ph.issue_id}" if ph else "/issues", 303)


# ---------------------------------------------------------------- images
@router.get("/photo/{pid}")
def photo(pid: int, t: int = 0, user: User = Depends(VIEW_CASES), db: Session = Depends(get_db)):
    ph = db.query(IssuePhoto).options(undefer(IssuePhoto.thumb if t else IssuePhoto.data), undefer(IssuePhoto.thumb)) \
        .filter(IssuePhoto.id == pid).first()
    data = (ph.thumb if t else (ph.data or ph.thumb)) if ph else None
    if not data:
        return Response(PLACEHOLDER, media_type="image/svg+xml")
    return Response(bytes(data), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/part-image/{pid}")
def part_image(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.query(Part).options(undefer(Part.image)).filter(Part.id == pid).first()
    if not p or not p.image:
        return Response(PLACEHOLDER, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=3600"})
    return Response(bytes(p.image), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@router.get("/api/parts")
def api_parts(customer: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = db.query(Part).filter(Part.active == True)  # noqa: E712
    if customer:
        q = q.filter(Part.customer_id == customer)
    return [dict(id=p.id, customer_id=p.customer_id, part_no=p.part_no, part_name=p.part_name, model=p.model,
                 variant=p.variant, process=p.process, has_image=p.has_image, v=p.image_ver)
            for p in q.order_by(Part.sort, Part.id).all()]
