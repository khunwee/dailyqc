"""Local launcher used by start.bat:  python run.py [--no-browser]

Starts the web server on every network interface, so other PCs and tablets on the same LAN can use it,
and opens the browser when the server is ready."""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

from app import config  # noqa: E402


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_when_ready(url: str):
    for _ in range(120):
        if port_open(config.PORT):
            webbrowser.open(url)
            return
        time.sleep(0.5)


if __name__ == "__main__":
    url = f"http://localhost:{config.PORT}"
    browser = "--no-browser" not in sys.argv
    if port_open(config.PORT):
        print(f"The program is already running at {url}")
        if browser:
            webbrowser.open(url)
        sys.exit(0)
    import uvicorn

    print("=" * 62)
    print("  Daily QC Operation Report")
    print(f"  This PC        : {url}")
    print(f"  Other devices  : http://{lan_ip()}:{config.PORT}   (same network)")
    print("  Keep this window open. Close it (or run stop.bat) to stop.")
    print("=" * 62)
    if browser:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    uvicorn.run("app.main:app", host=os.getenv("HOST", "0.0.0.0"), port=config.PORT, log_level="warning")
