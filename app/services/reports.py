"""Aggregations used by dashboards, Excel exports and scheduled reports."""
from collections import OrderedDict, defaultdict
from datetime import timedelta

from sqlalchemy import func

from .. import config
from ..core import MONTHS_EN
from ..models import Customer, DefectType, InspDefect, InspRecord, Issue, Line, Part

KEYS = ("production", "qc_check", "ok", "ng", "nc")


def _zero():
    return {k: 0 for k in KEYS}


def _add(dst, row):
    for k in KEYS:
        dst[k] += int(getattr(row, k) or 0)


def rates(t):
    c = t["qc_check"] or 0
    t["ok_pct"] = round(t["ok"] / c * 100, 2) if c else None
    t["ng_pct"] = round(t["ng"] / c * 100, 2) if c else None
    t["nc_pct"] = round(t["nc"] / c * 100, 2) if c else None
    return t


def bucket_mode(d1, d2, group="auto"):
    if group in ("day", "week", "month"):
        return group
    days = (d2 - d1).days + 1
    return "day" if days <= 62 else ("week" if days <= 190 else "month")


def bucket_key(d, mode):
    if mode == "month":
        return d.strftime("%Y-%m"), f"{MONTHS_EN[d.month]} {d.strftime('%y')}"
    if mode == "week":
        s = d - timedelta(days=d.weekday())
        return s.isoformat(), f"W{s.isocalendar()[1]} ({s.day}/{s.month})"
    return d.isoformat(), f"{d.day}/{d.month}"


def all_buckets(d1, d2, mode, clip=False):
    """Ordered {key: label}. With clip=True the axis stops at today, so charts do not show empty future days."""
    out = OrderedDict()
    if clip and d2 > config.today() >= d1:
        d2 = config.today()
    d = d1
    while d <= d2:
        k, lab = bucket_key(d, mode)
        out.setdefault(k, lab)
        d += timedelta(days=1)
    return out


def _insp_filter(q, d1, d2, customer_id=None, line_id=None, shift=None, part_id=None):
    q = q.filter(InspRecord.work_date >= d1, InspRecord.work_date <= d2)
    if customer_id:
        q = q.filter(InspRecord.customer_id == customer_id)
    if line_id:
        q = q.filter(InspRecord.line_id == line_id)
    if shift in ("D", "N"):
        q = q.filter(InspRecord.shift == shift)
    if part_id:
        q = q.filter(InspRecord.part_id == part_id)
    return q


SUMS = [func.coalesce(func.sum(getattr(InspRecord, k)), 0).label(k) for k in KEYS]


def insp_stats(db, d1, d2, customer_id=None, line_id=None, shift=None, group="auto", part_id=None):
    f = dict(customer_id=customer_id, line_id=line_id, shift=shift, part_id=part_id)
    mode = bucket_mode(d1, d2, group)
    total = _zero()
    buckets = all_buckets(d1, d2, mode, clip=True)
    trend = {k: _zero() for k in buckets}
    cust_trend = defaultdict(lambda: {k: _zero() for k in buckets})
    by_customer = defaultdict(_zero)
    for r in _insp_filter(db.query(InspRecord.work_date, InspRecord.customer_id, *SUMS), d1, d2, **f) \
            .group_by(InspRecord.work_date, InspRecord.customer_id).all():
        _add(total, r)
        k, lab = bucket_key(r.work_date, mode)
        if k not in buckets:
            buckets[k] = lab
            trend[k] = _zero()
        _add(trend[k], r)
        _add(cust_trend[r.customer_id].setdefault(k, _zero()), r)
        _add(by_customer[r.customer_id], r)

    customers = db.query(Customer).order_by(Customer.sort, Customer.id).all()
    cust_rows = [dict(id=c.id, code=c.code, color=c.color, **rates(dict(by_customer[c.id])))
                 for c in customers if c.id in by_customer or c.active]

    lines = {l.id: l.name for l in db.query(Line).all()}
    by_line = [dict(name=lines.get(r.line_id, "?"), **rates({k: int(getattr(r, k)) for k in KEYS}))
               for r in _insp_filter(db.query(InspRecord.line_id, *SUMS), d1, d2, **f).group_by(InspRecord.line_id).all()]
    by_line.sort(key=lambda x: x["name"])
    by_shift = {r.shift: rates({k: int(getattr(r, k)) for k in KEYS})
                for r in _insp_filter(db.query(InspRecord.shift, *SUMS), d1, d2, **f).group_by(InspRecord.shift).all()}

    parts = {}
    for r in _insp_filter(db.query(InspRecord.part_id, *SUMS), d1, d2, **f).group_by(InspRecord.part_id).all():
        parts[r.part_id] = rates({k: int(getattr(r, k)) for k in KEYS})
    pinfo = {p.id: p for p in db.query(Part).filter(Part.id.in_(list(parts) or [0])).all()}
    by_part = [dict(id=i, part_no=pinfo[i].part_no, part_name=pinfo[i].part_name, model=pinfo[i].model, **v)
               for i, v in parts.items() if i in pinfo]
    by_part.sort(key=lambda x: -(x["ng"] + x["nc"]))

    dq = db.query(InspDefect.defect_id, InspDefect.kind, func.sum(InspDefect.qty).label("q")) \
        .join(InspRecord, InspRecord.id == InspDefect.record_id)
    dq = _insp_filter(dq, d1, d2, **f).group_by(InspDefect.defect_id, InspDefect.kind).all()
    dnames = {d.id: d.name for d in db.query(DefectType).all()}
    dd = defaultdict(lambda: {"ng": 0, "nc": 0})
    for r in dq:
        dd[r.defect_id]["ng" if r.kind == "NG" else "nc"] += int(r.q or 0)
    defects = [dict(name=dnames.get(i, "?"), ng=v["ng"], nc=v["nc"], total=v["ng"] + v["nc"]) for i, v in dd.items()]
    defects.sort(key=lambda x: -x["total"])
    tot = sum(x["total"] for x in defects) or 1
    cum = 0
    for x in defects:
        cum += x["total"]
        x["cum_pct"] = round(cum / tot * 100, 1)

    return dict(
        mode=mode, total=rates(total), customers=cust_rows, by_line=by_line, by_shift=by_shift,
        by_part=by_part, defects=defects,
        trend=dict(labels=list(buckets.values()), keys=list(buckets.keys()),
                   **{k: [trend[b][k] for b in buckets] for k in KEYS},
                   ng_pct=[rates(dict(trend[b]))["ng_pct"] for b in buckets],
                   nc_pct=[rates(dict(trend[b]))["nc_pct"] for b in buckets],
                   ok_pct=[rates(dict(trend[b]))["ok_pct"] for b in buckets]),
        cust_trend=[dict(code=c.code, color=c.color,
                         ng_nc=[cust_trend[c.id].get(b, _zero())["ng"] + cust_trend[c.id].get(b, _zero())["nc"] for b in buckets],
                         ok_pct=[rates(dict(cust_trend[c.id].get(b, _zero())))["ok_pct"] for b in buckets])
                    for c in customers if c.id in cust_trend],
    )


def defect_matrix(db, d1, d2, customer_id, line_id=None, shift=None):
    """Part x defect matrix (NG + NC), the layout of the 'กราฟ' sheets in the original workbook."""
    q = db.query(InspRecord.part_id, InspDefect.defect_id, func.sum(InspDefect.qty).label("q")) \
        .join(InspRecord, InspRecord.id == InspDefect.record_id)
    q = _insp_filter(q, d1, d2, customer_id=customer_id, line_id=line_id, shift=shift) \
        .group_by(InspRecord.part_id, InspDefect.defect_id).all()
    dnames = {d.id: d.name for d in db.query(DefectType).all()}
    cols, cells = OrderedDict(), defaultdict(dict)
    for r in sorted(q, key=lambda r: -int(r.q or 0)):
        cols.setdefault(r.defect_id, dnames.get(r.defect_id, "?"))
        cells[r.part_id][r.defect_id] = int(r.q or 0)
    return cols, cells


def submission_status(db, d):
    """Which lines have sent data for each shift on a given day."""
    got = defaultdict(dict)
    for r in db.query(InspRecord.line_id, InspRecord.shift, func.count(InspRecord.id).label("n"),
                      func.sum(InspRecord.qc_check).label("c")) \
            .filter(InspRecord.work_date == d).group_by(InspRecord.line_id, InspRecord.shift).all():
        got[r.line_id][r.shift] = dict(parts=int(r.n), check=int(r.c or 0))
    return [dict(id=l.id, name=l.name, require=l.require_daily, D=got[l.id].get("D"), N=got[l.id].get("N"))
            for l in db.query(Line).filter(Line.active == True).order_by(Line.line_no).all()]  # noqa: E712


# ---------------------------------------------------------------- quality issues
def issue_query(db, d1, d2, customer_id=None, source=None, category=None, status=None, line_id=None, text=None):
    q = db.query(Issue).filter(Issue.found_date >= d1, Issue.found_date <= d2)
    if customer_id:
        q = q.filter(Issue.customer_id == customer_id)
    if source in ("internal", "external"):
        q = q.filter(Issue.source == source)
    if category:
        q = q.filter(Issue.category == category)
    if status:
        q = q.filter(Issue.status == status)
    if line_id:
        q = q.filter(Issue.line_id == line_id)
    if text:
        like = f"%{text}%"
        q = q.filter((Issue.part_no.ilike(like)) | (Issue.part_name.ilike(like)) | (Issue.problem.ilike(like))
                     | (Issue.case_no.ilike(like)))
    return q


def issue_stats(db, d1, d2, group="auto", **f):
    rows = issue_query(db, d1, d2, **f).all()
    customers = db.query(Customer).order_by(Customer.sort, Customer.id).all()
    cmap = {c.id: c.code for c in customers}
    dnames = {d.id: d.name for d in db.query(DefectType).all()}
    lines = {l.id: l.name for l in db.query(Line).all()}
    used = [c for c in customers if c.active or any(r.customer_id == c.id for r in rows)]
    ccodes = [c.code for c in used]

    def cnt():
        return {c: 0 for c in ccodes}
    src = {"internal": cnt(), "external": cnt()}
    cat, status, found_at, by_line = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    defect = defaultdict(cnt)
    defect_qty = defaultdict(int)
    part = defaultdict(lambda: {"cases": 0, "qty": 0, "name": ""})
    mode = bucket_mode(d1, d2, group)
    if mode == "day" and (d2 - d1).days > 31:
        mode = "week"
    buckets = all_buckets(d1, d2, mode, clip=True)
    trend = {k: {"internal": 0, "external": 0, "qty": 0} for k in buckets}
    for r in rows:
        cc = cmap.get(r.customer_id, "-")
        if cc in src[r.source if r.source in src else "internal"]:
            src[r.source if r.source in src else "internal"][cc] += 1
        cat[r.category] += 1
        status[r.status] += 1
        found_at[r.found_at or "-"] += 1
        if r.line_id:
            by_line[lines.get(r.line_id, "?")] += 1
        dn = dnames.get(r.defect_id) or (r.problem or "-")[:40]
        if cc in defect[dn]:
            defect[dn][cc] += 1
        defect_qty[dn] += int(r.ng_qty or 0)
        p = part[r.part_no or "-"]
        p["cases"] += 1
        p["qty"] += int(r.ng_qty or 0)
        p["name"] = r.part_name
        k, lab = bucket_key(r.found_date, mode)
        if k not in buckets:
            buckets[k] = lab
            trend[k] = {"internal": 0, "external": 0, "qty": 0}
        trend[k]["internal" if r.source != "external" else "external"] += 1
        trend[k]["qty"] += int(r.ng_qty or 0)
    dlist = sorted(defect.items(), key=lambda kv: -sum(kv[1].values()))
    tot = sum(sum(v.values()) for _, v in dlist) or 1
    cum, pareto = 0, []
    for name, v in dlist:
        cum += sum(v.values())
        pareto.append(dict(name=name, count=sum(v.values()), qty=defect_qty[name], by_customer=v,
                           cum_pct=round(cum / tot * 100, 1)))
    return dict(
        total=len(rows), ng_qty=sum(int(r.ng_qty or 0) for r in rows), customers=ccodes,
        colors={c.code: c.color for c in used}, source=src, category=dict(cat), status=dict(status),
        found_at=sorted(found_at.items(), key=lambda kv: -kv[1]), by_line=sorted(by_line.items(), key=lambda kv: -kv[1]),
        pareto=pareto,
        parts=sorted([dict(part_no=k, **v) for k, v in part.items()], key=lambda x: (-x["cases"], -x["qty"]))[:12],
        trend=dict(labels=list(buckets.values()), internal=[trend[b]["internal"] for b in buckets],
                   external=[trend[b]["external"] for b in buckets], qty=[trend[b]["qty"] for b in buckets]),
        mode=mode,
    )
