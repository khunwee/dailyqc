import calendar
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session, joinedload

from .. import config
from ..core import can, pdate, period_range, render, require, shift_now
from ..db import get_db
from ..models import Issue, User
from ..services import reports

router = APIRouter()


@router.get("/")
def overview(request: Request, m: str = "day", d: str = "", ym: str = "",
             user: User = Depends(require("overview.view", "insp.view", "issue.view")), db: Session = Depends(get_db)):
    today = config.today()
    q = request.query_params
    if m == "month":
        try:
            y, mo = [int(x) for x in (ym or today.strftime("%Y-%m")).split("-")]
            d1 = date(y, mo, 1)
        except ValueError:
            d1 = today.replace(day=1)
        d2 = d1.replace(day=calendar.monthrange(d1.year, d1.month)[1])
        prev, nxt = (d1 - timedelta(days=1)).strftime("%Y-%m"), (d2 + timedelta(days=1)).strftime("%Y-%m")
    elif m == "range":
        d1, d2 = pdate(q.get("from")), pdate(q.get("to"))
        if q.get("p"):
            d1, d2 = period_range(q.get("p"))
        if not (d1 and d2):
            d1, d2 = period_range("last7")
        if d2 < d1:
            d1, d2 = d2, d1
        prev = nxt = ""
    else:
        m = "day"
        d1 = d2 = pdate(d, today)
        prev, nxt = (d1 - timedelta(days=1)).isoformat(), (d1 + timedelta(days=1)).isoformat()

    stats = reports.insp_stats(db, d1, d2) if can(user, "insp.view") else None
    trend = None
    if stats:
        trend = reports.insp_stats(db, d1 - timedelta(days=13), d1, group="day")["trend"] if m == "day" else stats["trend"]
    istats, issues, open_issues = None, [], 0
    if can(user, "issue.view"):
        istats = reports.issue_stats(db, d1, d2)
        issues = reports.issue_query(db, d1, d2).options(joinedload(Issue.customer), joinedload(Issue.line)) \
            .order_by(Issue.found_date.desc(), Issue.id.desc()).limit(12).all()
        open_issues = db.query(Issue).filter(Issue.status != "closed").count()
    status = reports.submission_status(db, d1) if (m == "day" and stats) else []
    return render(request, "overview.html", db=db, m=m, d1=d1, d2=d2, prev=prev, nxt=nxt, stats=stats, trend=trend,
                  istats=istats, issues=issues, open_issues=open_issues, status=status, p=q.get("p", ""),
                  is_today=(d1 == today), night_pending=(d1 == today and shift_now(db) == (today, "D")))
