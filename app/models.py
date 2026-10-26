from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import deferred, relationship

from .config import now
from .db import Base


class Role(Base):
    __tablename__ = "roles"
    id = Column(Integer, primary_key=True)
    code = Column(String(30), unique=True, nullable=False)
    name = Column(String(80), nullable=False)
    perms = Column(Text, default="[]")
    is_system = Column(Boolean, default=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    full_name = Column(String(120), default="")
    email = Column(String(160), default="")
    role_id = Column(Integer, ForeignKey("roles.id"))
    password_hash = Column(String(200), nullable=False)
    active = Column(Boolean, default=True)
    must_change = Column(Boolean, default=False)
    lang = Column(String(5), default="th")
    line_ids = Column(Text, default="")        # comma separated; empty = every line
    last_login = Column(DateTime)
    created_at = Column(DateTime, default=now)
    role = relationship("Role")

    def lines(self):
        return [int(x) for x in (self.line_ids or "").split(",") if x.strip().isdigit()]


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    code = Column(String(30), unique=True, nullable=False)
    name = Column(String(120), default="")
    color = Column(String(9), default="#1F4FD8")
    sort = Column(Integer, default=0)
    active = Column(Boolean, default=True)


class Line(Base):
    __tablename__ = "lines"
    id = Column(Integer, primary_key=True)
    line_no = Column(Integer, default=0)
    name = Column(String(80), nullable=False)
    company = Column(String(40), default="AHR")
    customer_id = Column(Integer, ForeignKey("customers.id"))
    require_daily = Column(Boolean, default=True)
    active = Column(Boolean, default=True)
    customer = relationship("Customer")


class Part(Base):
    __tablename__ = "parts"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    process = Column(String(80), default="")
    part_no = Column(String(80), nullable=False)
    part_name = Column(String(200), default="")
    model = Column(String(60), default="")
    variant = Column(String(60), default="")
    line_id = Column(Integer, ForeignKey("lines.id"))
    image = deferred(Column(LargeBinary))
    has_image = Column(Boolean, default=False)
    image_ver = Column(Integer, default=1)
    sort = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    customer = relationship("Customer")
    line = relationship("Line")


class DefectType(Base):
    __tablename__ = "defect_types"
    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    group_name = Column(String(60), default="")
    sort = Column(Integer, default=0)
    active = Column(Boolean, default=True)


class InspRecord(Base):
    __tablename__ = "insp_records"
    id = Column(Integer, primary_key=True)
    work_date = Column(Date, nullable=False, index=True)
    shift = Column(String(1), nullable=False, default="D")
    line_id = Column(Integer, ForeignKey("lines.id"), nullable=False)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    production = Column(Integer, default=0)
    qc_check = Column(Integer, default=0)
    ok = Column(Integer, default=0)
    ng = Column(Integer, default=0)
    nc = Column(Integer, default=0)
    remark = Column(Text, default="")
    is_demo = Column(Boolean, default=False)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=now)
    updated_by = Column(Integer, ForeignKey("users.id"))
    updated_at = Column(DateTime, default=now)
    line = relationship("Line")
    part = relationship("Part")
    customer = relationship("Customer")
    defects = relationship("InspDefect", cascade="all, delete-orphan", back_populates="record")
    __table_args__ = (UniqueConstraint("work_date", "shift", "line_id", "part_id", name="uq_insp_slot"),)


class InspDefect(Base):
    __tablename__ = "insp_defects"
    id = Column(Integer, primary_key=True)
    record_id = Column(Integer, ForeignKey("insp_records.id", ondelete="CASCADE"), nullable=False, index=True)
    defect_id = Column(Integer, ForeignKey("defect_types.id"), nullable=False)
    kind = Column(String(2), default="NG")
    qty = Column(Integer, default=0)
    record = relationship("InspRecord", back_populates="defects")
    defect = relationship("DefectType")


class Issue(Base):
    __tablename__ = "issues"
    id = Column(Integer, primary_key=True)
    case_no = Column(String(30), unique=True, nullable=False)
    category = Column(String(30), default="Information")
    source = Column(String(10), default="internal")       # internal | external
    customer_id = Column(Integer, ForeignKey("customers.id"))
    line_id = Column(Integer, ForeignKey("lines.id"))
    part_id = Column(Integer, ForeignKey("parts.id"))
    part_no = Column(String(80), default="")
    part_name = Column(String(200), default="")
    model = Column(String(60), default="")
    defect_id = Column(Integer, ForeignKey("defect_types.id"))
    problem = Column(Text, default="")
    found_date = Column(Date, nullable=False, index=True)
    shift = Column(String(1), default="")
    found_at = Column(String(60), default="")
    ng_qty = Column(Integer, default=0)
    lot_no = Column(String(60), default="")
    action = Column(Text, default="")
    root_cause = Column(Text, default="")
    correction = Column(Text, default="")
    plan_date = Column(Date)
    actual_date = Column(Date)
    prevention = Column(Text, default="")
    responsible = Column(String(120), default="")
    status = Column(String(15), default="open")           # open | in_progress | closed
    is_demo = Column(Boolean, default=False)
    reported_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now)
    closed_at = Column(DateTime)
    customer = relationship("Customer")
    line = relationship("Line")
    part = relationship("Part")
    defect = relationship("DefectType")
    reporter = relationship("User")
    photos = relationship("IssuePhoto", cascade="all, delete-orphan", back_populates="issue",
                          order_by="IssuePhoto.id")


class IssuePhoto(Base):
    __tablename__ = "issue_photos"
    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, index=True)
    tag = Column(String(4), default="")                   # NG | OK | ""
    caption = Column(String(200), default="")
    data = deferred(Column(LargeBinary))
    thumb = deferred(Column(LargeBinary))
    size = Column(Integer, default=0)
    width = Column(Integer, default=0)
    height = Column(Integer, default=0)
    created_at = Column(DateTime, default=now, index=True)
    purged_at = Column(DateTime)
    issue = relationship("Issue", back_populates="photos")


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(60), primary_key=True)
    value = Column(Text, default="")


class Schedule(Base):
    __tablename__ = "schedules"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    kind = Column(String(20), default="report")           # report | missing
    include_insp = Column(Boolean, default=True)
    include_issue = Column(Boolean, default=True)
    period = Column(String(20), default="yesterday")
    freq = Column(String(10), default="daily")            # daily | weekly | monthly
    weekdays = Column(String(20), default="0,1,2,3,4,5")  # Monday = 0
    monthday = Column(Integer, default=1)
    at_time = Column(String(5), default="08:00")
    shift = Column(String(1), default="")                 # for "missing" reminders
    to_addr = Column(Text, default="")
    cc_addr = Column(Text, default="")
    to_roles = Column(String(200), default="")      # comma separated role codes: e-mail every user of them
    channels = Column(String(120), default="email")
    attach_excel = Column(Boolean, default=True)
    with_photos = Column(Boolean, default=True)
    enabled = Column(Boolean, default=True)
    last_slot = Column(String(20), default="")
    last_result = Column(String(250), default="")


class Notice(Base):
    __tablename__ = "notices"
    id = Column(Integer, primary_key=True)
    level = Column(String(10), default="info")
    title = Column(String(200), default="")
    body = Column(Text, default="")
    link = Column(String(200), default="")
    perm = Column(String(40), default="")
    created_at = Column(DateTime, default=now, index=True)


class NoticeRead(Base):
    __tablename__ = "notice_reads"
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_id = Column(Integer, default=0)


class SendLog(Base):
    __tablename__ = "send_logs"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=now, index=True)
    channel = Column(String(20))
    event = Column(String(60))
    target = Column(String(250), default="")
    ok = Column(Boolean, default=True)
    detail = Column(String(400), default="")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=now, index=True)
    username = Column(String(50), default="")
    action = Column(String(60))
    detail = Column(String(400), default="")


Index("ix_insp_date_cust", InspRecord.work_date, InspRecord.customer_id)
