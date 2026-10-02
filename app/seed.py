"""First-run master data (from the workbooks QC already uses) and an optional demo data set."""
import csv
import json
import os
import random
from datetime import timedelta

from . import config
from .core import DEFAULT_ROLES, hash_password
from .models import (Customer, DefectType, InspDefect, InspRecord, Issue, IssuePhoto, Line, Part, Role, Schedule, User)
from .services.images import process_photo

CUSTOMERS = [("Ford", "Ford / FTM  (P703-U704)", "#1F4FD8"), ("ChangAn", "ChangAn  (C857)", "#0E7490"),
             ("CCB", "GWM  Cross Car Beam (P01)", "#7C3AED"), ("BYD", "BYD  (ATTO / DOLPHIN)", "#D9480F"),
             ("EAC", "EAC  (P01 / P05)", "#475569")]
LINE_CUSTOMER = {"changan": "ChangAn", "byd": "BYD", "eac": "EAC", "gwm": "CCB"}
DEFECTS = [
    ("Nut / Bolt", ["Nut off-set", "Nut cold weld", "Missing nut", "Sub nut wrong spec", "Sub nut wrong position",
                    "Missing bolt", "Bolt cold weld", "Stud ทะลุ", "รูนัทไม่ตรงกับ CF"]),
    ("Welding", ["Welding NG", "แนวเชื่อมทะลุ", "Missing spot", "Missing tack weld", "Cold weld", "Part โดนหัวปืน Spot"]),
    ("Jig / Assembly", ["วางงานไม่ลง Jig", "Pin ไม่ลง", "ประกอบพาร์ทไม่ลง Jig", "ประกอบพาร์ทผิดรุ่น", "พาร์ทกระดก",
                        "Missing part", "Missing bush", "Missing brkt", "Brkt wrong position", "Part off set"]),
    ("Dimension", ["Hole misaligned", "Hole datum NG", "Trim line NG", "Gap NG"]),
    ("Part damage", ["Part ทะลุ", "Part เสียรูป", "Bush เสียรูป", "เจีย Part เสียรูป", "เจีย Part บาง",
                     "Part ตัดมาไม่ครบ Process"]),
    ("Other", ["อื่น ๆ (ระบุในหมายเหตุ)"]),
]


def seed_initial(db):
    if db.query(Role).count() == 0:
        for code, name, perms, system in DEFAULT_ROLES:
            db.add(Role(code=code, name=name, perms=json.dumps(perms), is_system=system))
        db.commit()
    if db.query(User).count() == 0:
        admin = db.query(Role).filter_by(code="admin").one()
        db.add(User(username="admin", full_name="Administrator", role_id=admin.id,
                    password_hash=hash_password(config.ADMIN_PASSWORD), must_change=True))
        db.commit()
    if os.getenv("RESET_ADMIN_PASSWORD", "").lower() in ("1", "true", "yes"):
        # rescue switch for a host: set it, restart, log in with ADMIN_PASSWORD, then remove the variable again
        admin = db.query(Role).filter_by(code="admin").one()
        u = db.query(User).filter_by(username="admin").first() or User(username="admin", full_name="Administrator")
        u.role_id, u.active, u.must_change = admin.id, True, True
        u.password_hash = hash_password(config.ADMIN_PASSWORD)
        db.add(u)
        db.commit()
        print("  RESET_ADMIN_PASSWORD is set: the admin password was reset. Remove this variable now.")
    if db.query(Customer).count() == 0:
        for i, (code, name, color) in enumerate(CUSTOMERS):
            db.add(Customer(code=code, name=name, color=color, sort=i))
        db.commit()
    cust = {c.code: c for c in db.query(Customer).all()}
    if db.query(Line).count() == 0:
        f = config.SEED_DIR / "LineList.csv"
        if f.exists():
            for row in csv.DictReader(f.open(encoding="utf-8-sig")):
                name = row["LineName"].strip()
                cc = next((v for k, v in LINE_CUSTOMER.items() if k in name.lower()), None)
                db.add(Line(line_no=int(row["LineNo"]), name=name, company=row.get("Company", "AHR").strip(),
                            customer_id=cust[cc].id if cc in cust else None))
        db.commit()
    if db.query(DefectType).count() == 0:
        n = 0
        for group, names in DEFECTS:
            for name in names:
                n += 1
                db.add(DefectType(name=name, group_name=group, sort=n))
        db.commit()
    if db.query(Part).count() == 0:
        f = config.SEED_DIR / "parts.json"
        if f.exists():
            for i, p in enumerate(json.loads(f.read_text(encoding="utf-8"))):
                if p["customer"] not in cust:
                    continue
                img = None
                ip = config.SEED_DIR / "part_images" / p["image"] if p.get("image") else None
                if ip and ip.exists():
                    img = ip.read_bytes()
                db.add(Part(customer_id=cust[p["customer"]].id, process=p["process"], part_no=p["part_no"],
                            part_name=p["part_name"], model=p["model"], variant=p["variant"], image=img,
                            has_image=bool(img), sort=i))
            db.commit()
    if db.query(Schedule).count() == 0:
        db.add_all([
            Schedule(name="Daily QC report (ผลของเมื่อวาน)", kind="report", period="yesterday", freq="daily",
                     weekdays="0,1,2,3,4,5", at_time="08:30", channels="email", enabled=False),
            Schedule(name="Weekly Quality Issue report", kind="report", include_insp=False, period="last_week",
                     freq="weekly", weekdays="0", at_time="09:00", channels="email", enabled=False),
            Schedule(name="Monthly QC summary", kind="report", period="last_month", freq="monthly", monthday=1,
                     weekdays="", at_time="09:00", channels="email", with_photos=False, enabled=False),
            Schedule(name="เตือน Line ที่ยังไม่ส่งข้อมูลกะ Day", kind="missing", shift="D", freq="daily",
                     weekdays="0,1,2,3,4,5", at_time="19:30", channels="inapp", enabled=False),
        ])
        db.commit()


# ---------------------------------------------------------------- demo data
DEMO_USERS = [("demo_qc", "QC Line A (demo)", "qc"), ("demo_sup", "QC Supervisor (demo)", "manager"),
              ("demo_asm", "Assembly (demo)", "assembly")]


def load_demo(db, days=24):
    """Synthetic inspection numbers for the last few weeks + the nine cases from the sample weekly report.
    Everything is flagged is_demo so it can be removed in one click."""
    rnd = random.Random(2026)
    today = config.today()
    cust = {c.code: c for c in db.query(Customer).all()}
    lines = db.query(Line).order_by(Line.line_no).all()
    defects = db.query(DefectType).filter(DefectType.group_name != "Other").all()
    roles = {r.code: r for r in db.query(Role).all()}
    for un, fn, rc in DEMO_USERS:
        if not db.query(User).filter_by(username=un).first() and rc in roles:
            la = next((l for l in lines if l.name.endswith(" A")), None)
            db.add(User(username=un, full_name=fn, role_id=roles[rc].id, password_hash=hash_password("demo1234"),
                        line_ids=str(la.id) if (rc == "qc" and la) else ""))
    db.commit()
    reporter = db.query(User).filter_by(username="demo_qc").first()

    # which parts run on which line: named lines take their customer, Line A-F share the Ford parts
    plan = {}
    ford_lines = [l for l in lines if not l.customer_id]
    for l in lines:
        if l.customer_id:
            plan[l.id] = db.query(Part).filter(Part.customer_id == l.customer_id, Part.active == True).all()  # noqa: E712
    if "Ford" in cust and ford_lines:
        fp = db.query(Part).filter(Part.customer_id == cust["Ford"].id).order_by(Part.sort).all()
        for i, p in enumerate(fp):
            plan.setdefault(ford_lines[i % len(ford_lines)].id, []).append(p)
    existing = {(r.work_date, r.shift, r.line_id, r.part_id) for r in
                db.query(InspRecord.work_date, InspRecord.shift, InspRecord.line_id, InspRecord.part_id)
                .filter(InspRecord.work_date >= today - timedelta(days=days))}
    n = 0
    for back in range(days, -1, -1):
        d = today - timedelta(days=back)
        if d.weekday() == 6:
            continue
        for l in lines:
            parts = plan.get(l.id, [])
            if not parts:
                continue
            for sh in ("D", "N"):
                if back == 0 and sh == "N":
                    continue
                if back == 0 and l.line_no % 3 == 0:
                    continue                      # leave a few lines "not sent yet" today
                for p in rnd.sample(parts, k=min(len(parts), rnd.randint(2, 5))):
                    if (d, sh, l.id, p.id) in existing:
                        continue
                    prod = rnd.choice([20, 30, 40, 52, 60, 66, 80, 100, 120])
                    chk = prod if rnd.random() < .8 else max(5, int(prod * rnd.uniform(.5, .95)))
                    ng = 0 if rnd.random() < .78 else rnd.randint(1, max(1, chk // 20))
                    nc = 0 if rnd.random() < .62 else rnd.randint(1, max(1, chk // 9))
                    rec = InspRecord(work_date=d, shift=sh, line_id=l.id, part_id=p.id, customer_id=p.customer_id,
                                     production=prod, qc_check=chk, ok=chk - ng - nc, ng=ng, nc=nc, is_demo=True,
                                     created_by=reporter.id if reporter else None)
                    for kind, q in (("NG", ng), ("NC", nc)):
                        left = q
                        while left > 0:
                            take = left if rnd.random() < .7 else rnd.randint(1, left)
                            rec.defects.append(InspDefect(defect_id=rnd.choice(defects[:18]).id, kind=kind, qty=take))
                            left -= take
                    db.add(rec)
                    n += 1
        db.commit()

    # the nine cases of "Weekly report AHR 5-11 Jan 26", moved to this week so the dashboards have something to show
    m = 0
    f = config.SEED_DIR / "demo_cases.json"
    if f.exists() and db.query(Issue).filter(Issue.is_demo == True).count() == 0:  # noqa: E712
        dmap = {d.name.lower(): d for d in db.query(DefectType).all()}
        cases = json.loads(f.read_text(encoding="utf-8"))
        for c in cases:
            cu = cust.get(c["customer"])
            part = db.query(Part).filter(Part.part_no == c["part_no"]).first()
            day = int(c["found_date"][-2:])                       # 6, 7, 8 January
            fd = today - timedelta(days=(8 - day) + 1)
            dt = dmap.get(c["defect"].lower())
            if not dt:
                dt = DefectType(name=c["defect"], group_name="Other", sort=900)
                db.add(dt)
                db.flush()
                dmap[c["defect"].lower()] = dt
            line = next((l for l in lines if cu and l.customer_id == cu.id), None) or (ford_lines[c["no"] % len(ford_lines)] if ford_lines else None)
            it = Issue(case_no=f"DEMO-{c['no']:03d}", category=c["category"], source=c["source"],
                       customer_id=cu.id if cu else None, line_id=line.id if line else None,
                       part_id=part.id if part else None, part_no=c["part_no"], part_name=c["part_name"], model=c["model"],
                       defect_id=dt.id, problem=c["problem"], found_date=fd, shift="D", found_at=c["found_at"],
                       ng_qty=c["ng_qty"], action=c.get("action", ""), root_cause=c.get("root_cause", ""),
                       correction=c.get("correction", ""), prevention=c.get("prevention", ""), status=c["status"],
                       is_demo=True, reported_by=reporter.id if reporter else None,
                       closed_at=config.now() if c["status"] == "closed" else None)
            for fn in c.get("photos", []):
                pf = config.SEED_DIR / "demo_photos" / fn
                if pf.exists():
                    full, th, w, h = process_photo(pf.read_bytes())
                    it.photos.append(IssuePhoto(data=full, thumb=th, size=len(full), width=w, height=h))
            db.add(it)
            m += 1
        db.commit()
    return n, m


def remove_demo(db):
    ids = [r[0] for r in db.query(InspRecord.id).filter(InspRecord.is_demo == True)]  # noqa: E712
    for i in range(0, len(ids), 300):
        db.query(InspDefect).filter(InspDefect.record_id.in_(ids[i:i + 300])).delete(synchronize_session=False)
        db.query(InspRecord).filter(InspRecord.id.in_(ids[i:i + 300])).delete(synchronize_session=False)
    iss = db.query(Issue).filter(Issue.is_demo == True).all()  # noqa: E712
    for it in iss:
        db.delete(it)
    db.commit()
    for un, _, _ in DEMO_USERS:
        u = db.query(User).filter_by(username=un).first()
        if u:
            used = db.query(InspRecord).filter((InspRecord.created_by == u.id) | (InspRecord.updated_by == u.id)).count() \
                + db.query(Issue).filter(Issue.reported_by == u.id).count()
            if used:
                u.active = False
            else:
                db.delete(u)
    db.commit()
    return len(ids), len(iss)
