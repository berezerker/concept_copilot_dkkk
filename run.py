"""Запуск: python run.py  → откроется браузер на http://127.0.0.1:8000"""
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import load_dotenv, env  # noqa: E402

load_dotenv()

if __name__ == "__main__":
    import uvicorn

    host = env("CF_HOST", "127.0.0.1")
    port = int(env("CF_PORT", "8000"))
    url = f"http://{host}:{port}"
    if env("CF_OPEN_BROWSER", "1") not in ("0", "false"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Копилот ДККК: {url}  (Ctrl+C — остановить)")
    uvicorn.run("app.server:app", host=host, port=port, log_level="warning")
