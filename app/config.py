"""Runtime configuration. Everything can be overridden with environment variables or a .env file."""
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APP_VERSION = "1.1.1"


def _load_dotenv():
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR = DATA_DIR / "backups"
SEED_DIR = BASE_DIR / "seed_data"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or f"sqlite:///{(DATA_DIR / 'dailyqc.db').as_posix()}"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len("postgresql://"):]
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Hosted = running on a cloud host (Render, Koyeb, Railway ...). Local PC otherwise.
IS_HOSTED = bool(os.getenv("RENDER") or os.getenv("KOYEB_APP_NAME") or os.getenv("RAILWAY_ENVIRONMENT")
                 or os.getenv("HOSTED", "").lower() in ("1", "true", "yes"))
_ON_PAAS = bool(os.getenv("RENDER") or os.getenv("KOYEB_APP_NAME") or os.getenv("RAILWAY_ENVIRONMENT"))
# "Secure" session cookie: on by default on the known HTTPS-only hosts; set COOKIE_SECURE=1 behind your own HTTPS proxy.
COOKIE_SECURE = (os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")) if os.getenv("COOKIE_SECURE") else _ON_PAAS
ALLOW_SHUTDOWN = os.getenv("ALLOW_SHUTDOWN", "").lower() in ("1", "true", "yes") if os.getenv("ALLOW_SHUTDOWN") else not IS_HOSTED

PORT = int(os.getenv("PORT", "8790"))
TZ_OFFSET_HOURS = float(os.getenv("TZ_OFFSET_HOURS", "7"))
TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
CRON_KEY = os.getenv("CRON_KEY", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
PHOTO_RETENTION_MAX = 45          # hard ceiling, days


def _secret() -> str:
    s = os.getenv("SECRET_KEY")
    if s:
        return s
    f = DATA_DIR / "secret.key"
    if f.exists():
        return f.read_text().strip()
    s = secrets.token_hex(32)
    try:
        f.write_text(s)
    except OSError:
        pass
    return s


SECRET_KEY = _secret()


def now() -> datetime:
    """Local (factory) time, naive."""
    return datetime.now(TZ).replace(tzinfo=None)


def today():
    return now().date()
