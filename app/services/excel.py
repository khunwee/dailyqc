"""Excel exports. Both reports keep the layout QC and Assembly already know from the old workbooks."""
import io
import math
from collections import defaultdict

import xlsxwriter
from sqlalchemy.orm import joinedload, undefer

from ..core import MONTHS_EN
from ..models import Customer, DefectType, InspDefect, InspRecord, Issue, IssuePhoto, Line, Part
from . import reports
from .images import image_size

FONT = "Tahoma"
C_INK, C_OK, C_NG, C_NC, C_HEAD, C_LINE = "#14213D", "#18794E", "#C4281C", "#B7791F", "#E8ECF2", "#B8C0CC"


def dmy(d):
    return f"{d.day} {MONTHS_EN[d.month]} {d.year}" if d else ""


def period_label(d1, d2):
    return dmy(d1) if d1 == d2 else f"{dmy(d1)} - {dmy(d2)}"


def _formats(wb):
    base = {"font_name": FONT, "font_size": 10, "valign": "vcenter"}
    mk = lambda **k: wb.add_format({**base, **k})  # noqa: E731
    return dict(
        title=mk(bold=True, font_size=16, font_color=C_INK),
        sub=mk(font_color="#5B6677"),
        head=mk(bold=True, bg_color=C_HEAD, border=1, border_color=C_LINE, align="center", text_wrap=True),
        cell=mk(border=1, border_color=C_LINE),
        cellc=mk(border=1, border_color=C_LINE, align="center"),
        wrap=mk(border=1, border_color=C_LINE, text_wrap=True, valign="top"),
        wrapc=mk(border=1, border_color=C_LINE, text_wrap=True, align="center"),
        num=mk(border=1, border_color=C_LINE, num_format="#,##0"),
        numb=mk(border=1, border_color=C_LINE, num_format="#,##0", bold=True, bg_color="#F4F6F9"),
        pct=mk(border=1, border_color=C_LINE, num_format="0.00%"),
        date=mk(border=1, border_color=C_LINE, num_format="dd-mmm-yy", align="center"),
        dhead=mk(bold=True, bg_color=C_HEAD, border=1, border_color=C_LINE, align="center", num_format="d-mmm"),
        st_prod=mk(border=1, border_color=C_LINE, bold=True),
        st_ok=mk(border=1, border_color=C_LINE, bold=True, font_color=C_OK),
        st_ng=mk(border=1, border_color=C_LINE, bold=True, font_color=C_NG),
        st_nc=mk(border=1, border_color=C_LINE, bold=True, font_color=C_NC),
        n_ok=mk(border=1, border_color=C_LINE, num_format="#,##0;-#,##0;", font_color=C_OK),
        n_ng=mk(border=1, border_color=C_LINE, num_format="#,##0;-#,##0;", font_color=C_NG, bold=True),
        n_nc=mk(border=1, border_color=C_LINE, num_format="#,##0;-#,##0;", font_color=C_NC, bold=True),
        n_blank=mk(border=1, border_color=C_LINE, num_format="#,##0;-#,##0;"),
        label=mk(bold=True, font_color=C_INK, valign="top", text_wrap=True),
        text=mk(text_wrap=True, valign="top"),
        casehead=mk(bold=True, font_size=11, font_color="#FFFFFF", bg_color=C_INK),
        section=mk(bold=True, font_size=12, font_color=C_INK, bottom=2, bottom_color=C_INK),
        note=mk(italic=True, font_color="#8A94A3"),
    )


# ====================================================================== QC Inspection Report
def export_inspection(db, d1, d2, customer_id=None, line_id=None, shift=None) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    F = _formats(wb)
    st = reports.insp_stats(db, d1, d2, customer_id, line_id, shift)
    flt = []
    if customer_id:
        flt.append("Customer: " + (db.get(Customer, customer_id).code))
    if line_id:
        flt.append("Line: " + db.get(Line, line_id).name)
    if shift:
        flt.append("Shift: " + ("Day" if shift == "D" else "Night"))

    # ---- Summary
    ws = wb.add_worksheet("Summary")
    ws.hide_gridlines(2)
    _print(ws)
    ws.set_column(0, 0, 20)
    ws.set_column(1, 8, 13)
    ws.write(0, 0, "QC Inspection Report (OK-NC-NG)", F["title"])
    ws.write(1, 0, period_label(d1, d2) + ("   |   " + ", ".join(flt) if flt else ""), F["sub"])
    heads = ["Customer", "Production", "QC Check", "OK", "NG", "NC", "% OK", "% NG", "% NC"]
    r0 = 3
    for i, h in enumerate(heads):
        ws.write(r0, i, h, F["head"])
    rows = [c for c in st["customers"] if (not customer_id or c["id"] == customer_id)]
    for j, c in enumerate(rows + [dict(code="Total", **st["total"])]):
        r = r0 + 1 + j
        last = j == len(rows)
        ws.write(r, 0, c["code"], F["numb"] if last else F["cell"])
        for i, k in enumerate(reports.KEYS):
            ws.write_number(r, 1 + i, c[k], F["numb"] if last else F["num"])
        for i, k in enumerate(("ok_pct", "ng_pct", "nc_pct")):
            if c.get(k) is None:
                ws.write(r, 6 + i, "-", F["cellc"])
            else:
                ws.write_number(r, 6 + i, c[k] / 100, F["pct"])
    if rows:
        ch = wb.add_chart({"type": "column", "subtype": "stacked"})
        for i, (k, col) in enumerate((("OK", C_OK), ("NG", C_NG), ("NC", "#E0A100"))):
            ch.add_series({"name": ["Summary", r0, 3 + i], "categories": ["Summary", r0 + 1, 0, r0 + len(rows), 0],
                           "values": ["Summary", r0 + 1, 3 + i, r0 + len(rows), 3 + i], "fill": {"color": col}})
        ch.set_title({"name": "OK / NG / NC by customer", "name_font": {"size": 11}})
        ch.set_legend({"position": "bottom"})
        ch.set_size({"width": 520, "height": 280})
        ws.insert_chart(r0, 10, ch)

    t = st["trend"]
    r1 = r0 + len(rows) + 4
    ws.write(r1 - 1, 0, f"Trend by {st['mode']}", F["section"])
    for i, h in enumerate(["Period", "Production", "QC Check", "OK", "NG", "NC", "% NG", "% NC"]):
        ws.write(r1, i, h, F["head"])
    for j, lab in enumerate(t["labels"]):
        ws.write(r1 + 1 + j, 0, lab, F["cellc"])
        for i, k in enumerate(reports.KEYS):
            ws.write_number(r1 + 1 + j, 1 + i, t[k][j], F["num"])
        for i, k in enumerate(("ng_pct", "nc_pct")):
            v = t[k][j]
            ws.write(r1 + 1 + j, 6 + i, (v / 100) if v is not None else "", F["pct"])
    n = len(t["labels"])
    if n:
        ch = wb.add_chart({"type": "column", "subtype": "stacked"})
        for i, (k, col) in enumerate((("OK", C_OK), ("NG", C_NG), ("NC", "#E0A100"))):
            ch.add_series({"name": k, "categories": ["Summary", r1 + 1, 0, r1 + n, 0],
                           "values": ["Summary", r1 + 1, 3 + i, r1 + n, 3 + i], "fill": {"color": col}})
        ln = wb.add_chart({"type": "line"})
        ln.add_series({"name": "% NG", "categories": ["Summary", r1 + 1, 0, r1 + n, 0],
                       "values": ["Summary", r1 + 1, 6, r1 + n, 6], "y2_axis": True,
                       "line": {"color": C_INK, "width": 1.75}, "marker": {"type": "circle", "size": 4}})
        ch.combine(ln)
        ch.set_title({"name": "Inspection result trend", "name_font": {"size": 11}})
        ch.set_legend({"position": "bottom"})
        ln.set_y2_axis({"num_format": "0.0%"})
        ch.set_size({"width": 760, "height": 300})
        ws.insert_chart(max(r0 + 16, r1), 10, ch)

    # ---- one pivot sheet per customer, like the original workbook
    q = reports._insp_filter(db.query(InspRecord), d1, d2, customer_id=customer_id, line_id=line_id, shift=shift)
    recs = q.options(joinedload(InspRecord.part), joinedload(InspRecord.line), joinedload(InspRecord.customer)).all()
    mode = "day" if (d2 - d1).days <= 92 else "month"
    buckets = reports.all_buckets(d1, d2, mode)
    cell = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))   # part -> (bucket, shift) -> key -> v
    for r in recs:
        k, _ = reports.bucket_key(r.work_date, mode)
        for key in reports.KEYS:
            cell[r.part_id][(k, r.shift)][key] += getattr(r, key) or 0
    customers = db.query(Customer).order_by(Customer.sort, Customer.id).all()
    STAT = [("Production", "production", "st_prod", "n_blank"), ("QC Check", "qc_check", "st_prod", "n_blank"),
            ("OK", "ok", "st_ok", "n_ok"), ("NG", "ng", "st_ng", "n_ng"), ("NC", "nc", "st_nc", "n_nc")]
    used_names = {"Summary", "Data", "Defect detail"}
    for c in customers:
        if customer_id and c.id != customer_id:
            continue
        pq = db.query(Part).options(undefer(Part.image)).filter(Part.customer_id == c.id).order_by(Part.sort, Part.id).all()
        parts = [p for p in pq if p.id in cell or (p.active and not line_id)]
        if not parts:
            continue
        name = _sheet_name(c.code, used_names)
        ws = wb.add_worksheet(name)
        _print(ws)
        ws.repeat_rows(2, 3)
        ws.freeze_panes(4, 6)
        ws.set_column(0, 0, 5)
        ws.set_column(1, 1, 15)
        ws.set_column(2, 2, 34)
        ws.set_column(3, 3, 24)
        ws.set_column(4, 4, 13)
        ws.set_column(5, 5, 11)
        ws.set_column(6, 6 + len(buckets) * 2, 6.5)
        ws.set_column(6 + len(buckets) * 2, 6 + len(buckets) * 2, 10)
        full = c.name if (c.name or "").lower().startswith(c.code.lower()) else f"{c.code}  {c.name or ''}"
        ws.write(0, 0, f"{full.strip()}  /  QC Inspection Report (OK-NC-NG)", F["title"])
        ws.write(1, 0, period_label(d1, d2) + ("   |   " + ", ".join(flt) if flt else ""), F["sub"])
        for i, h in enumerate(["No.", "Pic", "Part Name", "Part No.", "Model", "Status"]):
            ws.merge_range(2, i, 3, i, h, F["head"])
        for bi, (bk, lab) in enumerate(buckets.items()):
            col = 6 + bi * 2
            ws.merge_range(2, col, 2, col + 1, lab if mode == "month" else bk[8:10] + "-" + MONTHS_EN[int(bk[5:7])], F["head"])
            ws.write(3, col, "D", F["head"])
            ws.write(3, col + 1, "N", F["head"])
        tcol = 6 + len(buckets) * 2
        ws.merge_range(2, tcol, 3, tcol, "Total", F["head"])
        row, proc = 4, None
        for no, p in enumerate(parts, 1):
            if (p.process or "") != (proc or "") and p.process:
                ws.merge_range(row, 0, row, tcol, p.process, F["section"])
                row += 1
            proc = p.process
            for i, v in ((0, no), (1, ""), (2, p.part_name), (3, p.part_no), (4, " ".join(x for x in (p.model, p.variant) if x))):
                ws.merge_range(row, i, row + 4, i, v, F["wrapc"])
            if p.image:
                w, h = image_size(p.image)
                if w and h:
                    sc = min(100 / w, 92 / h)
                    ws.insert_image(row, 1, f"p{p.id}.jpg", {"image_data": io.BytesIO(p.image), "x_scale": sc, "y_scale": sc,
                                                             "x_offset": 4, "y_offset": 4, "object_position": 1})
            for si, (lab, key, fs, fn) in enumerate(STAT):
                ws.set_row(row + si, 20)
                ws.write(row + si, 5, lab, F[fs])
                tot = 0
                for bi, bk in enumerate(buckets):
                    for sj, sh in enumerate(("D", "N")):
                        v = cell[p.id][(bk, sh)][key] if p.id in cell else 0
                        tot += v
                        ws.write_number(row + si, 6 + bi * 2 + sj, v, F[fn])
                ws.write_number(row + si, tcol, tot, F["numb"])
            row += 5

        cols, cells = reports.defect_matrix(db, d1, d2, c.id, line_id, shift)
        if cols:
            wg = wb.add_worksheet(_sheet_name(c.code + " กราฟ", used_names))
            _print(wg)
            wg.set_column(0, 0, 6)
            wg.set_column(1, 1, 26)
            wg.set_column(2, 2 + len(cols), 16)
            wg.write(0, 0, f"{c.code}  /  NG-NC by defect", F["title"])
            wg.write(1, 0, period_label(d1, d2), F["sub"])
            wg.set_row(3, 48)
            wg.write(3, 0, "Item", F["head"])
            wg.write(3, 1, "Part no.", F["head"])
            for i, dn in enumerate(cols.values()):
                wg.write(3, 2 + i, dn, F["head"])
            wg.write(3, 2 + len(cols), "Total", F["head"])
            pr = [p for p in parts if p.id in cells]
            for j, p in enumerate(pr):
                wg.write(4 + j, 0, j + 1, F["cellc"])
                wg.write(4 + j, 1, p.part_no, F["cell"])
                for i, did in enumerate(cols):
                    wg.write(4 + j, 2 + i, cells[p.id].get(did, ""), F["n_blank"])
                wg.write_number(4 + j, 2 + len(cols), sum(cells[p.id].values()), F["numb"])
            tr = 4 + len(pr)
            wg.write(tr, 0, "Total", F["numb"])
            wg.write(tr, 1, "", F["numb"])
            for i, did in enumerate(cols):
                wg.write_number(tr, 2 + i, sum(cells[p.id].get(did, 0) for p in pr), F["numb"])
            wg.write_number(tr, 2 + len(cols), sum(sum(cells[p.id].values()) for p in pr), F["numb"])
            ch = wb.add_chart({"type": "column"})
            ch.add_series({"name": "NG + NC (pcs)", "categories": [wg.name, 3, 2, 3, 1 + len(cols)],
                           "values": [wg.name, tr, 2, tr, 1 + len(cols)], "fill": {"color": C_NG},
                           "data_labels": {"value": True}})
            ch.set_title({"name": f"{c.code} defect Pareto", "name_font": {"size": 11}})
            ch.set_legend({"none": True})
            ch.set_size({"width": 900, "height": 340})
            wg.insert_chart(tr + 2, 0, ch)

    # ---- flat database sheets
    ws = wb.add_worksheet("Data")
    heads = ["Date", "Shift", "Line", "Customer", "Process", "Part No.", "Part Name", "Model", "Production", "QC Check",
             "OK", "NG", "NC", "% NG", "% NC", "Remark", "Record ID"]
    widths = [12, 7, 14, 11, 20, 24, 34, 12, 11, 11, 9, 9, 9, 9, 9, 30, 10]
    for i, h in enumerate(heads):
        ws.write(0, i, h, F["head"])
        ws.set_column(i, i, widths[i])
    recs.sort(key=lambda r: (r.work_date, r.shift, r.line.line_no if r.line else 0, r.part_id))
    for j, r in enumerate(recs, 1):
        ws.write_datetime(j, 0, r.work_date, F["date"])
        ws.write(j, 1, "Day" if r.shift == "D" else "Night", F["cellc"])
        ws.write(j, 2, r.line.name if r.line else "", F["cell"])
        ws.write(j, 3, r.customer.code if r.customer else "", F["cell"])
        ws.write(j, 4, r.part.process if r.part else "", F["cell"])
        ws.write(j, 5, r.part.part_no if r.part else "", F["cell"])
        ws.write(j, 6, r.part.part_name if r.part else "", F["cell"])
        ws.write(j, 7, r.part.model if r.part else "", F["cell"])
        for i, k in enumerate(reports.KEYS):
            ws.write_number(j, 8 + i, getattr(r, k) or 0, F["num"])
        ws.write(j, 13, (r.ng / r.qc_check) if r.qc_check else "", F["pct"])
        ws.write(j, 14, (r.nc / r.qc_check) if r.qc_check else "", F["pct"])
        ws.write(j, 15, r.remark or "", F["cell"])
        ws.write(j, 16, r.id, F["cellc"])
    ws.autofilter(0, 0, max(1, len(recs)), len(heads) - 1)
    ws.freeze_panes(1, 0)

    ws = wb.add_worksheet("Defect detail")
    heads = ["Date", "Shift", "Line", "Customer", "Part No.", "Part Name", "Type", "Defect", "Qty"]
    for i, h in enumerate(heads):
        ws.write(0, i, h, F["head"])
        ws.set_column(i, i, [12, 7, 14, 11, 24, 34, 7, 36, 9][i])
    ids = [r.id for r in recs]
    rmap = {r.id: r for r in recs}
    dn = {d.id: d.name for d in db.query(DefectType).all()}
    j = 1
    for chunk in range(0, len(ids), 500):
        for d in db.query(InspDefect).filter(InspDefect.record_id.in_(ids[chunk:chunk + 500])).all():
            r = rmap[d.record_id]
            ws.write_datetime(j, 0, r.work_date, F["date"])
            ws.write(j, 1, "Day" if r.shift == "D" else "Night", F["cellc"])
            ws.write(j, 2, r.line.name if r.line else "", F["cell"])
            ws.write(j, 3, r.customer.code if r.customer else "", F["cell"])
            ws.write(j, 4, r.part.part_no if r.part else "", F["cell"])
            ws.write(j, 5, r.part.part_name if r.part else "", F["cell"])
            ws.write(j, 6, d.kind, F["cellc"])
            ws.write(j, 7, dn.get(d.defect_id, "?"), F["cell"])
            ws.write_number(j, 8, d.qty or 0, F["num"])
            j += 1
    ws.autofilter(0, 0, max(1, j - 1), len(heads) - 1)
    ws.freeze_panes(1, 0)
    wb.close()
    return buf.getvalue()


def _print(ws, landscape=True):
    ws.set_paper(9)                       # A4
    if landscape:
        ws.set_landscape()
    ws.fit_to_pages(1, 0)                 # one page wide, as many pages tall as needed
    ws.set_margins(left=0.4, right=0.4, top=0.5, bottom=0.5)


def _sheet_name(name, used):
    base = "".join(ch for ch in name if ch not in '[]:*?/\\')[:28] or "Sheet"
    n, i = base, 2
    while n in used:
        n = f"{base[:25]} {i}"
        i += 1
    used.add(n)
    return n


# ====================================================================== QC Report Quality Issue
def _lines(text, width):
    return sum(max(1, math.ceil(len(l) / width)) for l in (text or "-").split("\n"))


def export_issues(db, d1, d2, with_photos=True, **flt) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    F = _formats(wb)
    st = reports.issue_stats(db, d1, d2, **flt)
    issues = reports.issue_query(db, d1, d2, **flt).options(joinedload(Issue.customer), joinedload(Issue.line),
                                                            joinedload(Issue.defect), joinedload(Issue.reporter)) \
        .order_by(Issue.found_date, Issue.id).all()
    cust = st["customers"]

    ws = wb.add_worksheet("Report")
    ws.hide_gridlines(2)
    _print(ws)
    ws.set_column(0, 0, 26)
    ws.set_column(1, 1, 64)
    ws.set_column(2, 2, 3)
    ws.set_column(3, 14, 11)
    ws.merge_range(0, 0, 0, 12, f"Quality Issue Report   {period_label(d1, d2)}", F["title"])
    ws.set_row(0, 28)

    ws.write(2, 0, "Quality Issue", F["head"])
    ws.write(2, 1, "Cases", F["head"])
    cats = list(dict.fromkeys(["QR", "CR", "Information"] + list(st["category"].keys())))
    for i, c in enumerate(cats):
        ws.write(3 + i, 0, c, F["cell"])
        ws.write_number(3 + i, 1, st["category"].get(c, 0), F["num"])
    r = 3 + len(cats)
    ws.write(r, 0, "Total", F["numb"])
    ws.write_number(r, 1, st["total"], F["numb"])
    ws.write(r + 1, 0, f"NG quantity {st['ng_qty']:,} pcs.   Open {st['status'].get('open', 0)}   "
                       f"In progress {st['status'].get('in_progress', 0)}   Closed {st['status'].get('closed', 0)}", F["sub"])

    # found-by table + chart (right side)
    tc = 3
    ws.write(2, tc, "Found by", F["head"])
    for i, c in enumerate(cust):
        ws.write(2, tc + 1 + i, c, F["head"])
    for j, (lab, key) in enumerate((("AAPICO (internal)", "internal"), ("Customer (external)", "external"))):
        ws.write(3 + j, tc, lab, F["cell"])
        for i, c in enumerate(cust):
            ws.write_number(3 + j, tc + 1 + i, st["source"][key][c], F["num"])
    ws.set_column(tc, tc, 20)
    row = max(r + 3, 7)
    if cust and st["total"]:
        ch = wb.add_chart({"type": "column"})
        for j, col in enumerate(("#1F4FD8", C_NG)):
            ch.add_series({"name": ["Report", 3 + j, tc], "categories": ["Report", 2, tc + 1, 2, tc + len(cust)],
                           "values": ["Report", 3 + j, tc + 1, 3 + j, tc + len(cust)], "fill": {"color": col},
                           "data_labels": {"value": True}})
        ch.set_title({"name": f"Quality Issue {period_label(d1, d2)}", "name_font": {"size": 10}})
        ch.set_legend({"position": "bottom"})
        ch.set_y_axis({"num_format": "0", "major_unit": 1, "min": 0})
        ch.set_size({"width": 600, "height": 250})
        ws.insert_chart(row, 0, ch, {"x_offset": 4})
        # defect x customer
        dr = row + 14
        ws.write(dr, 0, "Quality Issue (defect)", F["head"])
        ws.write(dr, 1, "Cases / NG pcs", F["head"])
        for i, c in enumerate(cust):
            ws.write(dr, tc + 1 + i, c, F["head"])
        ws.write(dr, tc, "Defect", F["head"])
        top = st["pareto"][:12]
        for j, p in enumerate(top):
            ws.write(dr + 1 + j, 0, p["name"], F["cell"])
            ws.write(dr + 1 + j, 1, f"{p['count']} case / {p['qty']} pcs", F["cell"])
            ws.write(dr + 1 + j, tc, p["name"], F["cell"])
            for i, c in enumerate(cust):
                ws.write_number(dr + 1 + j, tc + 1 + i, p["by_customer"].get(c, 0), F["n_blank"])
        ch2 = wb.add_chart({"type": "bar", "subtype": "stacked"})
        palette = ["#1F4FD8", "#C4281C", "#18794E", "#E0A100", "#6B4FBB", "#0E7490", "#9A3412", "#475569"]
        for i, c in enumerate(cust):
            ch2.add_series({"name": c, "categories": ["Report", dr + 1, tc, dr + len(top), tc],
                            "values": ["Report", dr + 1, tc + 1 + i, dr + len(top), tc + 1 + i],
                            "fill": {"color": st["colors"].get(c) or palette[i % len(palette)]}})
        ch2.set_title({"name": "Defect by customer (cases)", "name_font": {"size": 10}})
        ch2.set_y_axis({"reverse": True})
        ch2.set_x_axis({"num_format": "0", "major_unit": 1, "min": 0})
        ch2.set_legend({"position": "bottom"})
        ch2.set_size({"width": 560, "height": 250})
        ws.insert_chart(row, tc, ch2)
        row = dr + len(top) + 3

    # ---- case blocks
    for no, it in enumerate(issues, 1):
        ws.set_row(row, 22)
        ws.merge_range(row, 0, row, 12, f"{it.category} :  Case {no}     {it.case_no}     "
                                        f"[{'Customer' if it.source == 'external' else 'Internal'}]"
                                        f"     Status: {it.status.replace('_', ' ').title()}", F["casehead"])
        top_row = row + 1
        fields = [
            ("Customer", it.customer.code if it.customer else ""),
            ("Model", it.model),
            ("Part Number", it.part_no),
            ("Part Name", it.part_name),
            ("Problem", it.problem or (it.defect.name if it.defect else "")),
            ("Found Date", dmy(it.found_date) + (f"  ({'Day' if it.shift == 'D' else 'Night'} shift)" if it.shift else "")),
            ("Found At", " / ".join(x for x in (it.found_at, it.line.name if it.line else "") if x)),
            ("NG Q'ty", f"{it.ng_qty or 0} pc." + (f"   Lot: {it.lot_no}" if it.lot_no else "")),
            ("QA/QC-AHR Action", it.action),
            ("1. Root Cause", it.root_cause),
            ("2. Correction (Immediate Containment)", (it.correction or "")
             + (f"\nPlan: {dmy(it.plan_date)}" if it.plan_date else "")
             + (f"\nActual: {dmy(it.actual_date)}" if it.actual_date else "")),
            ("3. Prevention", it.prevention),
            ("Reported by", (it.reporter.full_name or it.reporter.username) if it.reporter else ""),
        ]
        rr, height_px = top_row, 0
        for lab, val in fields:
            if not val and lab in ("QA/QC-AHR Action", "Reported by", "Model"):
                continue
            h = max(18, 15 * max(_lines(val, 78), _lines(lab, 28)))
            ws.set_row(rr, h)
            ws.write(rr, 0, lab + ":", F["label"])
            ws.write(rr, 1, val or "-", F["text"])
            height_px += h * 4 / 3
            rr += 1
        photos = db.query(IssuePhoto).options(undefer(IssuePhoto.data), undefer(IssuePhoto.thumb)) \
            .filter(IssuePhoto.issue_id == it.id).order_by(IssuePhoto.id).all() if with_photos else []
        target_h = 230
        if photos:
            while height_px < target_h + 8:
                ws.set_row(rr, 18)
                height_px += 24
                rr += 1
            x = 6
            shown = 0
            for ph in photos:
                data = ph.data or ph.thumb
                if not data or shown >= 4:
                    continue
                w, h = image_size(data)
                if not w:
                    continue
                sc = min(target_h / h, 300 / w)
                ws.insert_image(top_row, 3, f"i{ph.id}.jpg", {"image_data": io.BytesIO(data), "x_scale": sc, "y_scale": sc,
                                                              "x_offset": x, "y_offset": 4, "object_position": 2})
                x += int(w * sc) + 10
                shown += 1
            if any(p.purged_at for p in photos):
                ws.write(rr, 1, "Full-size photos were removed by the retention policy; thumbnails shown where kept.", F["note"])
                rr += 1
        elif with_photos:
            ws.write(top_row, 3, "No photo", F["note"])
        row = rr + 1

    # ---- flat data
    wd = wb.add_worksheet("Data")
    heads = ["Case No.", "Category", "Source", "Status", "Found Date", "Shift", "Customer", "Line", "Model", "Part No.",
             "Part Name", "Defect", "Problem", "Found At", "NG Qty", "Lot", "QA/QC Action", "Root Cause", "Correction",
             "Plan Date", "Actual Date", "Prevention", "Responsible", "Reported By", "Created", "Closed", "Photos"]
    widths = [14, 12, 10, 12, 12, 7, 11, 14, 12, 22, 32, 24, 40, 12, 8, 12, 40, 40, 40, 12, 12, 30, 18, 16, 16, 16, 8]
    for i, h in enumerate(heads):
        wd.write(0, i, h, F["head"])
        wd.set_column(i, i, widths[i])
    pc = {}
    for iid, in db.query(IssuePhoto.issue_id).all():
        pc[iid] = pc.get(iid, 0) + 1
    dt = wb.add_format({"font_name": FONT, "font_size": 10, "border": 1, "border_color": C_LINE, "num_format": "dd-mmm-yy hh:mm"})
    for j, it in enumerate(issues, 1):
        vals = [it.case_no, it.category, "Customer" if it.source == "external" else "Internal",
                it.status.replace("_", " ").title(), it.found_date, {"D": "Day", "N": "Night"}.get(it.shift, ""),
                it.customer.code if it.customer else "", it.line.name if it.line else "", it.model, it.part_no, it.part_name,
                it.defect.name if it.defect else "", it.problem, it.found_at, it.ng_qty or 0, it.lot_no, it.action,
                it.root_cause, it.correction, it.plan_date, it.actual_date, it.prevention, it.responsible,
                (it.reporter.full_name or it.reporter.username) if it.reporter else "", it.created_at, it.closed_at,
                pc.get(it.id, 0)]
        for i, v in enumerate(vals):
            if v is None:
                wd.write(j, i, "", F["cell"])
            elif i in (4, 19, 20):
                wd.write_datetime(j, i, v, F["date"])
            elif i in (24, 25):
                wd.write_datetime(j, i, v, dt)
            elif isinstance(v, int):
                wd.write_number(j, i, v, F["num"])
            else:
                wd.write(j, i, v, F["cell"])
    wd.autofilter(0, 0, max(1, len(issues)), len(heads) - 1)
    wd.freeze_panes(1, 1)
    wb.close()
    return buf.getvalue()
