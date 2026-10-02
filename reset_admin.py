"""Emergency tool: gives the account "admin" the password admin1234 again (and re-enables it).
Run it on the PC that holds the data:   .venv\\Scripts\\python.exe reset_admin.py"""
import os
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

from app.core import hash_password  # noqa: E402
from app.db import SessionLocal, auto_migrate  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.seed import seed_initial  # noqa: E402

auto_migrate()
db = SessionLocal()
seed_initial(db)
role = db.query(Role).filter_by(code="admin").one()
u = db.query(User).filter_by(username="admin").first()
if not u:
    u = User(username="admin", full_name="Administrator")
    db.add(u)
u.role_id, u.active, u.must_change = role.id, True, True
u.password_hash = hash_password("admin1234")
db.commit()
db.close()
print('Done. Log in as "admin" with password "admin1234"; you will be asked for a new one.')
