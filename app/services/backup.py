"""Backup / restore and the 45-day photo retention.

A backup is a single ZIP: data.json (every table, text only) + blobs/ (photos and part pictures as .jpg files)
+ two Excel workbooks for people who only want to read the data."""
import io
import json
import tempfile
import zipfile
from datetime import date, datetime, timedelta

from sqlalchemy import Boolean, Date, DateTime, LargeBinary, func, select, text
from sqlalchemy.orm import undefer

from .. import config
from ..core import get_setting, retention_days
from ..db import Base, engine
from ..models import Issue, IssuePhoto

SKIP_TABLES = set()


def _ser(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def write_backup(db, fileobj, include_photos=True, include_excel=True):
    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED) as z:
        meta = {"app": "daily-qc-report", "version": config.APP_VERSION, "created": config.now().isoformat(),
                "photos": include_photos, "tables": {}}
        dump = {}
        conn = db.connection()
        for table in Base.metadata.sorted_tables:
            blob_cols = [c.name for c in table.columns if isinstance(c.type, LargeBinary)]
            plain = [c for c in table.columns if c.name not in blob_cols]
            rows = [dict(zip([c.name for c in plain], [_ser(v) for v in r])) for r in conn.execute(select(*plain))]
            dump[table.name] = rows
            meta["tables"][table.name] = len(rows)
            if blob_cols and "id" in table.c:
                if table.name == "issue_photos" and not include_photos:
                    continue
                for bc in blob_cols:
                    for rid, data in conn.execute(select(table.c.id, table.c[bc]).where(table.c[bc].isnot(None))):
                        if data:
                            z.writestr(f"blobs/{table.name}/{rid}_{bc}.jpg", bytes(data), zipfile.ZIP_STORED)
        z.writestr("data.json", json.dumps(dump, ensure_ascii=False))
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
        if include_excel:
            from . import excel
            d1, d2 = date(2000, 1, 1), date(2100, 1, 1)
            mm = db.execute(text("select min(work_date), max(work_date) from insp_records")).first()
            if mm and mm[0]:
                a, b = mm
                a = a if isinstance(a, date) else date.fromisoformat(str(a)[:10])
                b = b if isinstance(b, date) else date.fromisoformat(str(b)[:10])
                z.writestr("QC_Inspection_Report_all.xlsx", excel.export_inspection(db, a, b))
            z.writestr("QC_Quality_Issue_all.xlsx", excel.export_issues(db, d1, d2, with_photos=False))
        z.writestr("README.txt", "Daily QC Operation Report backup\n"
                                 "data.json  = all tables (text)\nblobs/     = photos and part pictures\n"
                                 "Restore: Admin > สำรองข้อมูล > กู้คืนจากไฟล์สำรอง\n")


def backup_tempfile(db, include_photos=True):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    write_backup(db, tmp, include_photos)
    tmp.close()
    return tmp.name


def restore_backup(db, fileobj):
    """Replace the whole database with the content of a backup ZIP."""
    with zipfile.ZipFile(fileobj) as z:
        dump = json.loads(z.read("data.json"))
        names = set(z.namelist())
        tables = Base.metadata.sorted_tables
        conn = db.connection()
        for table in reversed(tables):
            conn.execute(table.delete())
        counts = {}
        for table in tables:
            rows = dump.get(table.name, [])
            cols = {c.name: c for c in table.columns}
            blob_cols = [c.name for c in table.columns if isinstance(c.type, LargeBinary)]
            out = []
            for r in rows:
                rec = {}
                for k, v in r.items():
                    c = cols.get(k)
                    if c is None:
                        continue
                    if v is not None and isinstance(c.type, DateTime):
                        v = datetime.fromisoformat(v)
                    elif v is not None and isinstance(c.type, Date):
                        v = date.fromisoformat(v[:10])
                    elif v is not None and isinstance(c.type, Boolean):
                        v = bool(v)
                    rec[k] = v
                for bc in blob_cols:
                    fn = f"blobs/{table.name}/{r.get('id')}_{bc}.jpg"
                    rec[bc] = z.read(fn) if fn in names else None
                out.append(rec)
            for i in range(0, len(out), 200):
                conn.execute(table.insert(), out[i:i + 200])
            counts[table.name] = len(out)
        if not config.IS_SQLITE:
            for table in tables:
                if "id" in table.c:
                    conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}','id'), "
                                      f"COALESCE((SELECT MAX(id) FROM \"{table.name}\"), 1))"))
        db.commit()
    return counts


# ---------------------------------------------------------------- photo retention
def photo_stats(db):
    keep = retention_days(db)
    n = config.now()
    total, size = db.query(func.count(IssuePhoto.id), func.coalesce(func.sum(IssuePhoto.size), 0)) \
        .filter(IssuePhoto.purged_at.is_(None)).first()
    soon_days = int(get_setting(db, "events")["photo_expiry"].get("days_before") or 7)
    cutoff_soon = n - timedelta(days=keep - soon_days)
    soon, soon_size = db.query(func.count(IssuePhoto.id), func.coalesce(func.sum(IssuePhoto.size), 0)) \
        .filter(IssuePhoto.purged_at.is_(None), IssuePhoto.created_at < cutoff_soon).first()
    purged = db.query(func.count(IssuePhoto.id)).filter(IssuePhoto.purged_at.isnot(None)).scalar()
    oldest = db.query(func.min(IssuePhoto.created_at)).filter(IssuePhoto.purged_at.is_(None)).scalar()
    return dict(keep=keep, total=total or 0, size=int(size or 0), soon=soon or 0, soon_size=int(soon_size or 0),
                soon_days=soon_days, purged=purged or 0, oldest=oldest,
                next_purge=(oldest + timedelta(days=keep)) if oldest else None)


def photos_zip(db, fileobj, older_than_days=None):
    """ZIP of full-size photos, grouped by case. older_than_days=None means every photo still stored."""
    q = db.query(IssuePhoto).options(undefer(IssuePhoto.data)).filter(IssuePhoto.purged_at.is_(None))
    if older_than_days is not None:
        q = q.filter(IssuePhoto.created_at < config.now() - timedelta(days=older_than_days))
    cases = {i.id: i for i in db.query(Issue).all()}
    n = 0
    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_STORED) as z:
        index = ["case_no,found_date,customer_part,problem,photo_file,tag,uploaded"]
        for ph in q.order_by(IssuePhoto.issue_id, IssuePhoto.id).yield_per(20):
            if not ph.data:
                continue
            it = cases.get(ph.issue_id)
            folder = f"{it.found_date:%Y-%m}/{it.case_no}_{(it.part_no or '').replace('/', '-')}" if it else "unknown"
            name = f"{folder}/{ph.id}{('_' + ph.tag) if ph.tag else ''}.jpg"
            z.writestr(name, bytes(ph.data))
            if it:
                index.append(f'{it.case_no},{it.found_date},{it.part_no},"{(it.problem or "").replace(chr(34), "")[:80]}",'
                             f'{name},{ph.tag},{ph.created_at:%Y-%m-%d}')
            n += 1
        z.writestr("index.csv", "\ufeff" + "\n".join(index))
    return n


def purge_photos(db):
    """Drop full-size photos older than the retention period. Text and (optionally) the small thumbnail stay."""
    keep = retention_days(db)
    keep_thumb = bool(get_setting(db, "general").get("keep_thumb", True))
    cutoff = config.now() - timedelta(days=keep)
    ids = [r[0] for r in db.query(IssuePhoto.id).filter(IssuePhoto.purged_at.is_(None), IssuePhoto.created_at < cutoff)]
    for i in range(0, len(ids), 100):
        vals = {"data": None, "purged_at": config.now(), "size": 0}
        if not keep_thumb:
            vals["thumb"] = None
        db.query(IssuePhoto).filter(IssuePhoto.id.in_(ids[i:i + 100])).update(vals, synchronize_session=False)
        db.commit()
    if ids and config.IS_SQLITE:
        try:
            with engine.connect() as c:
                c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
                c.execution_options(isolation_level="AUTOCOMMIT").exec_driver_sql("VACUUM")
        except Exception:  # noqa: BLE001
            pass
    return len(ids)


def local_auto_backup(db):
    """On a PC the disk is permanent, so keep a rolling set of full backups in data/backups."""
    g = get_setting(db, "general")
    if config.IS_HOSTED or not g.get("auto_local_backup", True):
        return None
    config.BACKUP_DIR.mkdir(exist_ok=True)
    path = config.BACKUP_DIR / f"dailyqc_backup_{config.now():%Y%m%d}.zip"
    with open(path, "wb") as f:
        write_backup(db, f, include_photos=True, include_excel=False)
    files = sorted(config.BACKUP_DIR.glob("dailyqc_backup_*.zip"))
    for old in files[:-max(1, int(g.get("local_backup_keep") or 14))]:
        old.unlink(missing_ok=True)
    return path.name


def db_size(db):
    try:
        if config.IS_SQLITE:
            p = config.DATA_DIR / "dailyqc.db"
            return p.stat().st_size if p.exists() else 0
        return int(db.execute(text("select pg_database_size(current_database())")).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0
