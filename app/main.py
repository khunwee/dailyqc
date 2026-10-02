"""Daily QC Operation Report - FastAPI application."""
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .core import Forbidden, NeedLogin, render, sync_notice_state
from .db import SessionLocal, auto_migrate
from .routers import admin, auth, inspection, issues, overview
from .seed import seed_initial
from .services import jobs

@asynccontextmanager
async def lifespan(_app):
    auto_migrate()
    db = SessionLocal()
    try:
        seed_initial(db)
        sync_notice_state(db)
    finally:
        db.close()
    jobs.start_ticker()
    print(f"\n  Daily QC Operation Report v{config.APP_VERSION}")
    print(f"  database : {'SQLite (data/dailyqc.db)' if config.IS_SQLITE else 'PostgreSQL'}")
    print(f"  open     : http://localhost:{config.PORT}\n")
    yield


app = FastAPI(title="Daily QC Operation Report", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    # same-origin check for anything that changes data
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-site request blocked", status_code=403)
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=86400")
    else:
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="dailyqc", max_age=7 * 24 * 3600,
                   same_site="lax", https_only=config.COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login"}, status_code=401)
    nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
    return RedirectResponse("/login" + (f"?next={quote(nxt)}" if nxt not in ("/", "") else ""), status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "ไม่มีสิทธิ์ใช้งานส่วนนี้"}, status_code=403)
    db = SessionLocal()
    try:
        resp = render(request, "message.html", db=db, title="ไม่มีสิทธิ์เข้าถึงหน้านี้",
                      text="บัญชีของคุณไม่ได้รับสิทธิ์สำหรับหน้านี้ ติดต่อ Admin ถ้าจำเป็นต้องใช้งาน", back="/")
        resp.status_code = 403
        return resp
    finally:
        db.close()


for r in (auth.router, overview.router, inspection.router, issues.router, admin.router):
    app.include_router(r)
