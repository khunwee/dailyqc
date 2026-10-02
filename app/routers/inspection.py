import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session, joinedload

from .. import config
from ..core import (allowed_lines, audit, can, flash, get_setting, parse_range, pdate, pint, render, require, shift_now)
from ..db import get_db
from ..models import Customer, DefectType, InspDefect, InspRecord, Line, Part, User
from ..services import excel, jobs, reports

router = APIRouter()
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _common(db):
    return dict(customers=db.query(Customer).filter(Customer.active == True).order_by(Customer.sort, Customer.id).all(),  # noqa: E712
                lines=db.query(Line).filter(Line.active == True).order_by(Line.line_no).all())  # noqa: E712


def _filters(request):
    q = request.query_params
    sh = q.get("shift") if q.get("shift") in ("D", "N") else None
    return pint(q.get("customer")), pint(q.get("line")), sh


def _locked(db, user, d):
    days = int(get_setting(db, "general").get("insp_lock_days") or 0)
    return bool(days) and (config.today() - d).days > days and not can(user, "insp.edit_all")


# ---------------------------------------------------------------- entry sheet
@router.get("/inspection/entry")
def entry(request: Request, user: User = Depends(require("insp.entry", "insp.edit_all")), db: Session = Depends(get_db)):
    q = request.query_params
    wd, sh = shift_now(db)
    d = pdate(q.get("date"), wd)
    shift = q.get("shift") if q.get("shift") in ("D", "N") else sh
    c = _common(db)
    my_lines = allowed_lines(user, c["lines"])
    line = next((l for l in my_lines if l.id == pint(q.get("line"))), None) \
        or next((l for l in my_lines if l.id == request.session.get("line")), None) or (my_lines[0] if my_lines else None)
    if not line:
        return render(request, "message.html", db=db, title="ยังไม่มี Line ให้บันทึก",
                      text="บัญชีนี้ยังไม่ได้กำหนด Line ติดต่อ Admin เพื่อกำหนด Line ที่รับผิดชอบ", back="/")
    request.session["line"] = line.id
    cust = next((x for x in c["customers"] if x.id == pint(q.get("customer"))), None)
    if not cust:
        last = db.query(InspRecord.customer_id).filter(InspRecord.line_id == line.id) \
            .order_by(InspRecord.work_date.desc(), InspRecord.id.desc()).first()
        cid = line.customer_id or (last[0] if last else None)
        cust = next((x for x in c["customers"] if x.id == cid), None) or (c["customers"][0] if c["customers"] else None)
    recs = {r.part_id: r for r in db.query(InspRecord).options(joinedload(InspRecord.defects))
            .filter(InspRecord.work_date == d, InspRecord.shift == shift, InspRecord.line_id == line.id).all()}
    parts = []
    if cust:
        for p in db.query(Part).filter(Part.customer_id == cust.id).order_by(Part.sort, Part.id).all():
            if p.id in recs or (p.active and (not p.line_id or p.line_id == line.id)):
                parts.append(p)
    other = [r for pid, r in recs.items() if not cust or r.customer_id != cust.id]
    defects = db.query(DefectType).filter(DefectType.active == True).order_by(DefectType.sort, DefectType.id).all()  # noqa: E712
    rec_json = {pid: dict(defects=[dict(defect_id=x.defect_id, kind=x.kind, qty=x.qty) for x in r.defects])
                for pid, r in recs.items()}
    return render(request, "insp/entry.html", db=db, d=d, shift=shift, line=line, cust=cust, my_lines=my_lines,
                  parts=parts, recs=recs, other_count=len(other), defects=defects, rec_json=json.dumps(rec_json),
                  locked=_locked(db, user, d), prev=(d - timedelta(days=1)).isoformat(),
                  nxt=(d + timedelta(days=1)).isoformat(), **c)


@router.post("/api/insp/sheet")
async def save_sheet(request: Request, user: User = Depends(require("insp.entry", "insp.edit_all")),
                     db: Session = Depends(get_db)):
    body = await request.json()
    d = pdate(body.get("date"))
    shift = body.get("shift")
    line = db.get(Line, pint(body.get("line_id")) or 0)
    if not d or shift not in ("D", "N") or not line:
        return JSONResponse({"error": "ข้อมูลวันที่ กะ หรือ Line ไม่ครบ"}, status_code=400)
    if line not in allowed_lines(user, [line]):
        return JSONResponse({"error": f"บัญชีนี้ไม่มีสิทธิ์บันทึกข้อมูลของ {line.name}"}, status_code=403)
    if d > config.today() + timedelta(days=1):
        return JSONResponse({"error": "บันทึกล่วงหน้าเกิน 1 วันไม่ได้"}, status_code=400)
    if _locked(db, user, d):
        return JSONResponse({"error": "ข้อมูลวันนี้ถูกล็อกแล้ว ให้หัวหน้า QC เป็นผู้แก้ไข"}, status_code=403)
    valid_def = {x.id for x in db.query(DefectType.id)}
    saved, removed, warns, ids = 0, 0, [], []
    for row in body.get("rows", []):
        part = db.get(Part, pint(row.get("part_id")) or 0)
        if not part:
            continue
        v = {k: max(0, pint(row.get(k), 0) or 0) for k in reports.KEYS}
        remark = (row.get("remark") or "").strip()[:500]
        rec = db.query(InspRecord).filter_by(work_date=d, shift=shift, line_id=line.id, part_id=part.id).first()
        if not any(v.values()) and not remark:
            if rec:
                db.delete(rec)
                removed += 1
            continue
        if not rec:
            rec = InspRecord(work_date=d, shift=shift, line_id=line.id, part_id=part.id, created_by=user.id)
            db.add(rec)
        rec.customer_id = part.customer_id
        for k, val in v.items():
            setattr(rec, k, val)
        rec.remark, rec.updated_by, rec.updated_at, rec.is_demo = remark, user.id, config.now(), False
        rec.defects.clear()
        for x in row.get("defects") or []:
            qty = pint(x.get("qty"), 0) or 0
            if qty > 0 and pint(x.get("defect_id")) in valid_def:
                rec.defects.append(InspDefect(defect_id=int(x["defect_id"]), kind="NC" if x.get("kind") == "NC" else "NG", qty=qty))
        if v["qc_check"] and v["ok"] + v["ng"] + v["nc"] != v["qc_check"]:
            warns.append(f"{part.part_no}: OK+NG+NC = {v['ok'] + v['ng'] + v['nc']} ไม่เท่ากับ QC Check {v['qc_check']}")
        db.flush()
        ids.append(rec.id)
        saved += 1
    db.commit()
    audit(db, user, "insp_save", f"{d} {shift} {line.name}: saved {saved}, removed {removed}")
    if ids:
        jobs.run_bg(jobs.ng_alert, ids)
    return {"ok": True, "saved": saved, "removed": removed, "warnings": warns}


# ---------------------------------------------------------------- records
@router.get("/inspection/records")
def records(request: Request, page: int = 1, user: User = Depends(require("insp.view")), db: Session = Depends(get_db)):
    d1, d2, p = parse_range(request, "last7")
    cid, lid, sh = _filters(request)
    q = reports._insp_filter(db.query(InspRecord), d1, d2, cid, lid, sh)
    text = (request.query_params.get("q") or "").strip()
    if text:
        q = q.join(Part, Part.id == InspRecord.part_id).filter(Part.part_no.ilike(f"%{text}%") | Part.part_name.ilike(f"%{text}%"))
    total = q.count()
    per = 100
    page = max(1, page)
    rows = q.options(joinedload(InspRecord.part), joinedload(InspRecord.line), joinedload(InspRecord.customer),
                     joinedload(InspRecord.defects).joinedload(InspDefect.defect)) \
        .order_by(InspRecord.work_date.desc(), InspRecord.shift, InspRecord.line_id, InspRecord.part_id) \
        .offset((page - 1) * per).limit(per).all()
    sums = reports.insp_stats(db, d1, d2, cid, lid, sh)["total"] if not text else None
    return render(request, "insp/records.html", db=db, rows=rows, total=total, page=page, pages=(total + per - 1) // per,
                  d1=d1, d2=d2, p=p, cid=cid, lid=lid, sh=sh, text=text, sums=sums, **_common(db))


@router.post("/inspection/records/{rid}/delete")
def record_delete(rid: int, request: Request, user: User = Depends(require("insp.entry", "insp.edit_all")),
                  db: Session = Depends(get_db)):
    rec = db.get(InspRecord, rid)
    if rec:
        line = db.get(Line, rec.line_id)
        if line not in allowed_lines(user, [line]) or _locked(db, user, rec.work_date):
            flash(request, "ไม่มีสิทธิ์ลบรายการนี้", "err")
        else:
            audit(db, user, "insp_delete", f"{rec.work_date} {rec.shift} {line.name} part {rec.part_id}")
            db.delete(rec)
            db.commit()
            flash(request, "ลบรายการแล้ว")
    return RedirectResponse(request.headers.get("referer") or "/inspection/records", 303)


# ---------------------------------------------------------------- charts
@router.get("/inspection/charts")
def charts(request: Request, user: User = Depends(require("insp.view")), db: Session = Depends(get_db)):
    d1, d2, p = parse_range(request, "this_month")
    cid, lid, sh = _filters(request)
    group = request.query_params.get("group") or "auto"
    st = reports.insp_stats(db, d1, d2, cid, lid, sh, group)
    return render(request, "insp/charts.html", db=db, st=st, d1=d1, d2=d2, p=p, cid=cid, lid=lid, sh=sh, group=group,
                  **_common(db))


@router.get("/api/insp/stats")
def api_stats(request: Request, user: User = Depends(require("insp.view")), db: Session = Depends(get_db)):
    d1, d2, _ = parse_range(request, "this_month")
    cid, lid, sh = _filters(request)
    return reports.insp_stats(db, d1, d2, cid, lid, sh, request.query_params.get("group") or "auto")


# ---------------------------------------------------------------- report for Assembly (pivot)
@router.get("/inspection/report")
def report(request: Request, user: User = Depends(require("insp.view")), db: Session = Depends(get_db)):
    d1, d2, p = parse_range(request, "last7")
    cid, lid, sh = _filters(request)
    c = _common(db)
    cust = next((x for x in c["customers"] if x.id == cid), None) or (c["customers"][0] if c["customers"] else None)
    mode = "day" if (d2 - d1).days <= 31 else "month"
    buckets = reports.all_buckets(d1, d2, mode)
    cell, parts, totals = {}, [], {}
    cols, dcell = {}, {}
    if cust:
        recs = reports._insp_filter(db.query(InspRecord), d1, d2, cust.id, lid, None).all()
        for r in recs:
            k, _ = reports.bucket_key(r.work_date, mode)
            slot = cell.setdefault(r.part_id, {}).setdefault((k, r.shift), dict.fromkeys(reports.KEYS, 0))
            tot = totals.setdefault(r.part_id, dict.fromkeys(reports.KEYS, 0))
            for key in reports.KEYS:
                slot[key] += getattr(r, key) or 0
                tot[key] += getattr(r, key) or 0
        parts = [x for x in db.query(Part).filter(Part.customer_id == cust.id).order_by(Part.sort, Part.id).all() if x.id in cell]
        cols, dcell = reports.defect_matrix(db, d1, d2, cust.id, lid, None)
    st = reports.insp_stats(db, d1, d2, cust.id if cust else None, lid, None)
    return render(request, "insp/report.html", db=db, d1=d1, d2=d2, p=p, cust=cust, cid=cust.id if cust else None, lid=lid,
                  sh=None, mode=mode, buckets=buckets, cell=cell, parts=parts, totals=totals, cols=cols, dcell=dcell,
                  st=st, keys=reports.KEYS, **c)


@router.get("/inspection/export.xlsx")
def export(request: Request, user: User = Depends(require("insp.export")), db: Session = Depends(get_db)):
    d1, d2, _ = parse_range(request, "this_month")
    cid, lid, sh = _filters(request)
    data = excel.export_inspection(db, d1, d2, cid, lid, sh)
    audit(db, user, "insp_export", f"{d1} - {d2}")
    name = f"QC_Inspection_Report_{d1:%Y%m%d}-{d2:%Y%m%d}.xlsx"
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'})
