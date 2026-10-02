"""Scheduled reports, event notifications and daily maintenance.

The ticker runs inside the web process. On a free host that sleeps when idle, call /cron/tick from an
external pinger (cron-job.org, UptimeRobot): every call wakes the app and runs whatever is due."""
import html as _h
import io
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.orm import undefer

from .. import config
from ..core import get_setting, period_range, set_setting
from ..db import SessionLocal
from ..models import InspRecord, Issue, IssuePhoto, Schedule
from . import backup, excel, notify, reports

_lock = threading.Lock()
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def run_bg(fn, *args):
    """Run fn(db, *args) in a thread with its own DB session."""
    def work():
        db = SessionLocal()
        try:
            fn(db, *args)
        except Exception as e:  # noqa: BLE001
            print("[bg]", fn.__name__, e)
        finally:
            db.close()
    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- report content
def _tbl(heads, rows):
    th = "".join(f'<th style="background:#E8ECF2;border:1px solid #B8C0CC;padding:5px 9px;text-align:{"left" if i == 0 else "right"}">{_h.escape(str(x))}</th>'
                 for i, x in enumerate(heads))
    trs = ""
    for r in rows:
        trs += "<tr>" + "".join(
            f'<td style="border:1px solid #B8C0CC;padding:5px 9px;text-align:{"left" if i == 0 else "right"}">{_h.escape(str(x))}</td>'
            for i, x in enumerate(r)) + "</tr>"
    return f'<table style="border-collapse:collapse;font-size:13px;margin:6px 0 16px">{"<tr>" + th + "</tr>"}{trs}</table>'


def _p(v):
    return "-" if v is None else f"{v:.2f}%"


def build_report(db, d1, d2, include_insp=True, include_issue=True, attach=True, with_photos=True):
    """Returns (subject, text, html, attachments)."""
    label = excel.period_label(d1, d2)
    text, html, att = [], "", []
    if include_insp:
        st = reports.insp_stats(db, d1, d2)
        t = st["total"]
        text.append(f"[QC Inspection] {label}\nProduction {t['production']:,} | QC Check {t['qc_check']:,} | "
                    f"OK {t['ok']:,} | NG {t['ng']:,} ({_p(t['ng_pct'])}) | NC {t['nc']:,} ({_p(t['nc_pct'])})")
        rows = []
        for c in st["customers"]:
            if c["qc_check"] or c["production"]:
                rows.append([c["code"], f"{c['production']:,}", f"{c['qc_check']:,}", f"{c['ok']:,}", f"{c['ng']:,}",
                             f"{c['nc']:,}", _p(c["ok_pct"]), _p(c["ng_pct"]), _p(c["nc_pct"])])
                text.append(f"- {c['code']}: Check {c['qc_check']:,} OK {c['ok']:,} NG {c['ng']:,} NC {c['nc']:,}")
        rows.append(["Total", f"{t['production']:,}", f"{t['qc_check']:,}", f"{t['ok']:,}", f"{t['ng']:,}", f"{t['nc']:,}",
                     _p(t["ok_pct"]), _p(t["ng_pct"]), _p(t["nc_pct"])])
        html += f"<h3 style='margin:14px 0 4px'>QC Inspection Report ({_h.escape(label)})</h3>"
        html += _tbl(["Customer", "Production", "QC Check", "OK", "NG", "NC", "% OK", "% NG", "% NC"], rows)
        if st["defects"]:
            html += "<b>Top defects</b>" + _tbl(["Defect", "NG", "NC", "Total"],
                                                [[d["name"], d["ng"], d["nc"], d["total"]] for d in st["defects"][:8]])
            text.append("Top defect: " + ", ".join(f"{d['name']} ({d['total']})" for d in st["defects"][:3]))
        if d1 == d2:
            miss = [f"{s['name']} ({'/'.join(x for x in ('D', 'N') if not s[x])})"
                    for s in reports.submission_status(db, d1) if s["require"] and not (s["D"] and s["N"])]
            if miss:
                html += f"<p style='color:#B7791F'>Line ที่ยังส่งข้อมูลไม่ครบ: {_h.escape(', '.join(miss))}</p>"
        if attach:
            att.append((f"QC_Inspection_Report_{d1:%Y%m%d}-{d2:%Y%m%d}.xlsx", excel.export_inspection(db, d1, d2), XLSX))
    if include_issue:
        si = reports.issue_stats(db, d1, d2)
        text.append(f"[Quality Issue] {label}: {si['total']} case, NG {si['ng_qty']:,} pcs "
                    f"(internal {sum(si['source']['internal'].values())}, customer {sum(si['source']['external'].values())})")
        html += f"<h3 style='margin:14px 0 4px'>QC Report Quality Issue ({_h.escape(label)})</h3>"
        html += _tbl(["Found by"] + si["customers"],
                     [["AAPICO (internal)"] + [si["source"]["internal"][c] for c in si["customers"]],
                      ["Customer"] + [si["source"]["external"][c] for c in si["customers"]]])
        issues = reports.issue_query(db, d1, d2).order_by(Issue.found_date, Issue.id).limit(40).all()
        if issues:
            html += _tbl(["Case", "Date", "Customer", "Part No.", "Problem", "NG", "Status"],
                         [[i.case_no, f"{i.found_date:%d/%m}", i.customer.code if i.customer else "", i.part_no,
                           (i.problem or "")[:70], i.ng_qty or 0, i.status.replace("_", " ")] for i in issues])
            for i in issues[:10]:
                text.append(f"- {i.case_no} {i.customer.code if i.customer else ''} {i.part_no}: {(i.problem or '')[:60]} "
                            f"x{i.ng_qty or 0}")
        else:
            html += "<p>ไม่มี Quality Issue ในช่วงนี้</p>"
        if attach:
            att.append((f"QC_Quality_Issue_{d1:%Y%m%d}-{d2:%Y%m%d}.xlsx",
                        excel.export_issues(db, d1, d2, with_photos=with_photos), XLSX))
    return f"Daily QC Operation Report : {label}", "\n".join(text), html, att


def run_schedule(db, sch: Schedule, manual=False):
    chans = [c for c in (sch.channels or "").split(",") if c]
    if sch.kind == "missing":
        d, sh = config.today(), sch.shift or "D"
        if sh == "N" and config.now().strftime("%H:%M") < "12:00":
            d = d - timedelta(days=1)
        miss = [s["name"] for s in reports.submission_status(db, d) if s["require"] and not s[sh]]
        if not miss and not manual:
            return "ครบทุก Line ไม่ต้องเตือน"
        subject = f"ยังไม่ได้รับข้อมูล QC Inspection กะ {'Day' if sh == 'D' else 'Night'} วันที่ {d:%d/%m/%Y}"
        body = ("Line ที่ยังไม่ส่ง: " + ", ".join(miss)) if miss else "ทุก Line ส่งข้อมูลครบแล้ว"
        res = notify.dispatch(db, "missing_entry", chans, subject, body, to=sch.to_addr, cc=sch.cc_addr,
                              link="/inspection/entry", perm="insp.entry", level="warn")
    else:
        d1, d2 = period_range(sch.period)
        subject, text, html, att = build_report(db, d1, d2, sch.include_insp, sch.include_issue,
                                                sch.attach_excel, sch.with_photos)
        res = notify.dispatch(db, "scheduled_report", chans, f"{sch.name} : {excel.period_label(d1, d2)}", text, html,
                              to=sch.to_addr, cc=sch.cc_addr, attachments=att, link="/", perm="overview.view")
    return ", ".join(f"{k}:{'OK' if v[0] else 'FAIL ' + str(v[1])[:60]}" for k, v in res.items()) or "ไม่ได้เลือกช่องทาง"


# ---------------------------------------------------------------- events
def issue_created(db, issue_id):
    ev = get_setting(db, "events")["issue_new"]
    if not ev.get("enabled"):
        return
    it = db.get(Issue, issue_id)
    if not it:
        return
    src = "ลูกค้า" if it.source == "external" else "ภายใน"
    subject = f"Quality Issue ใหม่ {it.case_no} ({src}) {it.customer.code if it.customer else ''}"
    body = (f"Part: {it.part_no} {it.part_name}\nModel: {it.model}\nปัญหา: {it.problem}\nจำนวน NG: {it.ng_qty or 0} pc.\n"
            f"พบที่: {it.found_at} {it.line.name if it.line else ''}  วันที่ {it.found_date:%d/%m/%Y}\n"
            f"ผู้แจ้ง: {(it.reporter.full_name or it.reporter.username) if it.reporter else '-'}")
    ph = db.query(IssuePhoto).options(undefer(IssuePhoto.data)).filter(IssuePhoto.issue_id == it.id).first()
    notify.dispatch(db, "issue_new", ev.get("channels"), subject, body, to=ev.get("to"), cc=ev.get("cc"),
                    photo=bytes(ph.data) if ph and ph.data else None, link=f"/issues/{it.id}", perm="issue.view",
                    level="alert" if it.source == "external" else "warn")


def ng_alert(db, record_ids):
    ev = get_setting(db, "events")["ng_alert"]
    if not ev.get("enabled") or not record_ids:
        return
    th, mn = float(ev.get("threshold_pct") or 5), int(ev.get("min_check") or 0)
    bad = []
    for r in db.query(InspRecord).filter(InspRecord.id.in_(record_ids)).all():
        if r.qc_check and r.qc_check >= mn:
            pct = (r.ng + r.nc) / r.qc_check * 100
            if pct >= th:
                bad.append((r, pct))
    if not bad:
        return
    r0 = bad[0][0]
    subject = f"NG/NC เกิน {th:g}% : {r0.line.name} กะ {'Day' if r0.shift == 'D' else 'Night'} {r0.work_date:%d/%m/%Y}"
    body = "\n".join(f"- {r.customer.code} {r.part.part_no}: Check {r.qc_check} NG {r.ng} NC {r.nc} ({pct:.1f}%)"
                     for r, pct in bad)
    notify.dispatch(db, "ng_alert", ev.get("channels"), subject, body, to=ev.get("to"), cc=ev.get("cc"),
                    link=f"/inspection/records?from={r0.work_date}&to={r0.work_date}&line={r0.line_id}",
                    perm="insp.view", level="alert")


# ---------------------------------------------------------------- maintenance + ticker
def daily_maintenance(db):
    out = []
    ev = get_setting(db, "events")["photo_expiry"]
    st = backup.photo_stats(db)
    if ev.get("enabled") and st["soon"]:
        subject = f"รูป Quality Issue {st['soon']} รูป จะถูกลบภายใน {st['soon_days']} วัน"
        body = (f"ระบบเก็บรูปไว้ {st['keep']} วัน รูปที่ใกล้ครบกำหนดมีขนาดรวม {st['soon_size'] / 1e6:.1f} MB\n"
                f"ถ้าต้องการเก็บรูปไว้ ให้ดาวน์โหลดที่เมนู สำรองข้อมูล ก่อนครบกำหนด (ข้อความของเคสไม่ถูกลบ)")
        att = None
        if ev.get("mail_zip") and "email" in (ev.get("channels") or []) and st["soon_size"] < 18_000_000:
            buf = io.BytesIO()
            backup.photos_zip(db, buf, older_than_days=st["keep"] - st["soon_days"])
            att = [(f"photos_expiring_{config.today():%Y%m%d}.zip", buf.getvalue(), "application/zip")]
        chans = ev.get("channels") or []
        # attachments only make sense by e-mail
        notify.dispatch(db, "photo_expiry", [c for c in chans if c != "email"], subject, body,
                        link="/admin/backup", perm="admin", level="warn")
        if "email" in chans:
            notify.dispatch(db, "photo_expiry", ["email"], subject, body, to=ev.get("to"), cc=ev.get("cc"),
                            attachments=att, link="/admin/backup")
        out.append(f"expiry notice {st['soon']}")
    try:
        name = backup.local_auto_backup(db)      # before purging, so the last local backup still has the photos
        if name:
            out.append("backup " + name)
    except Exception as e:  # noqa: BLE001
        out.append(f"backup failed {e}")
    n = backup.purge_photos(db)
    if n:
        out.append(f"purged {n} photos")
    return "; ".join(out) or "nothing to do"


def _due_slot(sch: Schedule, now: datetime):
    """Latest scheduled moment that is <= now (looking back up to 12 hours), or None."""
    try:
        hh, mm = [int(x) for x in sch.at_time.split(":")]
    except ValueError:
        return None
    for back in (0, 1):
        day = now.date() - timedelta(days=back)
        slot = datetime(day.year, day.month, day.day, hh, mm)
        if slot > now or now - slot > timedelta(hours=12):
            continue
        if sch.freq == "weekly" and str(day.weekday()) not in (sch.weekdays or "").split(","):
            continue
        if sch.freq == "monthly" and day.day != int(sch.monthday or 1):
            continue
        if sch.freq == "daily" and sch.weekdays and str(day.weekday()) not in sch.weekdays.split(","):
            continue
        return slot
    return None


# What is due is decided from a small in-memory copy of the schedules, so an idle tick never touches the
# database. That matters on free PostgreSQL (Neon): a database that is queried every 30 seconds can never go to
# sleep and would burn through the monthly free compute hours.
_plan = {"at": 0.0, "slots": [], "maint": ""}
PLAN_TTL = 6 * 3600


def invalidate():
    """Call after schedules change (or after a restore) so the next tick re-reads them."""
    _plan["at"] = 0.0


def _load_plan(db):
    _plan["slots"] = [SimpleNamespace(freq=s.freq, weekdays=s.weekdays, monthday=s.monthday, at_time=s.at_time,
                                      last_slot=s.last_slot or "")
                      for s in db.query(Schedule).filter(Schedule.enabled == True).all()]  # noqa: E712
    _plan["maint"] = get_setting(db, "maint").get("last_date", "")
    _plan["at"] = time.time()


def _plan_due(now):
    for s in _plan["slots"]:
        slot = _due_slot(s, now)
        if slot and s.last_slot < slot.strftime("%Y-%m-%d %H:%M"):
            return True
    return _plan["maint"] != str(now.date()) and now.strftime("%H:%M") >= "00:30"


def tick(force=False):
    """Run everything that is due. Safe to call often and from several places."""
    if not _lock.acquire(blocking=False):
        return "busy"
    done = []
    db = None
    try:
        now = config.now()
        if not force and time.time() - _plan["at"] < PLAN_TTL and not _plan_due(now):
            return "idle"
        db = SessionLocal()
        for sch in db.query(Schedule).filter(Schedule.enabled == True).all():  # noqa: E712
            slot = _due_slot(sch, now)
            if not slot:
                continue
            key = slot.strftime("%Y-%m-%d %H:%M")
            if (sch.last_slot or "") >= key:
                continue
            sch.last_slot = key
            db.commit()
            try:
                sch.last_result = f"{now:%d/%m %H:%M} {run_schedule(db, sch)}"[:250]
            except Exception as e:  # noqa: BLE001
                sch.last_result = f"{now:%d/%m %H:%M} ERROR {e}"[:250]
            db.commit()
            done.append(sch.name)
        m = get_setting(db, "maint")
        if m.get("last_date") != str(now.date()) and now.strftime("%H:%M") >= "00:30":
            set_setting(db, "maint", {"last_date": str(now.date())})
            res = daily_maintenance(db)
            set_setting(db, "maint", {"last_date": str(now.date()), "last_result": f"{now:%d/%m %H:%M} {res}"})
            done.append("maintenance")
        _load_plan(db)
    except Exception as e:  # noqa: BLE001
        print("[tick]", e)
    finally:
        if db is not None:
            db.close()
        _lock.release()
    return ", ".join(done) or "idle"


def start_ticker():
    def loop():
        time.sleep(8)
        while True:
            try:
                tick()
            except Exception as e:  # noqa: BLE001
                print("[ticker]", e)
            time.sleep(30)
    threading.Thread(target=loop, daemon=True, name="ticker").start()
