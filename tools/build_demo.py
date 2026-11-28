"""Собрать интерактивное демо: static/index.html + запись сеанса (CF_RECORD=1) → один demo.html без сервера.

  python -m tools.build_demo                      # последняя запись из workspace/demo_rec/
  python -m tools.build_demo --rec путь/к/записи --out demo.html

В demo.html — настоящий интерфейс копилота; запросы к серверу подменены записью: кнопки и вопросы
проигрываются с теми же событиями (план, инструменты, графики, журнал), длинные паузы сжаты.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main(argv=None):
    from app import config
    config.load_dotenv(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", default=None, help="папка записи (по умолчанию — последняя)")
    ap.add_argument("--out", default=os.path.join(ROOT, "demo.html"))
    a = ap.parse_args(argv)
    rec = a.rec
    if not rec:
        dirs = sorted(glob.glob(os.path.join(config.workdir(), "demo_rec", "*")))
        if not dirs:
            sys.exit("Нет записей: запустите record_demo.command и пройдите сценарий в интерфейсе")
        rec = dirs[-1]
    recs = []
    for f in sorted(glob.glob(os.path.join(rec, "*.json"))):
        r = json.load(open(f, encoding="utf-8"))
        if r.get("status", 200) >= 400:
            continue
        recs.append(r)
    if not recs:
        sys.exit(f"В {rec} нет успешных запросов")
    model = None
    for r in recs:
        if r["path"] in ("/api/upload_path", "/api/upload") and r["chunks"]:
            try:
                model = json.loads("".join(c[1] for c in r["chunks"]))["card"]["file"]
            except Exception:
                pass
    meta = {"model": model, "date": time.strftime("%d.%m.%Y", time.localtime(os.path.getmtime(rec))), "n": len(recs)}
    data = json.dumps({"meta": meta, "recs": recs}, ensure_ascii=False).replace("</", "<\\/")
    shim = open(os.path.join(ROOT, "tools", "demo_shim.js"), encoding="utf-8").read()
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    script = '<script src="/credit-review.js"></script>'
    if script in html and any(r["path"].startswith("/api/credit-review/") for r in recs):
        workflow = open(os.path.join(ROOT, "static", "credit-review.js"), encoding="utf-8").read()
        html = html.replace(script, f"<script>\n{workflow}\n</script>", 1)
    else:
        html = html.replace(script, "", 1)
    inject = f"<script>window.__DEMO__={data};</script>\n<script>{shim}</script>\n"
    html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + "\n" + inject, html, count=1)
    html = re.sub(r"<title>(.*?)</title>", r"<title>\1 · демо</title>", html, count=1)
    open(a.out, "w", encoding="utf-8").write(html)
    chats = sum(r["path"] == "/api/chat" for r in recs)
    print(f"Готово: {a.out} · {os.path.getsize(a.out) / 1e6:.1f} МБ · запросов {len(recs)}, вопросов в чат {chats}, модель {model}")


if __name__ == "__main__":
    main()
