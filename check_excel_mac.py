"""Диагностика Excel для Mac: перебирает способы запуска Excel и открытия файла, печатает, что сработало.
    python check_excel_mac.py путь/к/модели.xlsx"""
import os, shutil, subprocess, sys, time, traceback
path = os.path.abspath(sys.argv[1])
name = os.path.basename(path)
import xlwings as xw
print("xlwings", xw.__version__)
try:
    print("Excel запущен:", xw.apps.count, "экз.")
except Exception as e:
    print("apps.count:", e)


def wait_book(app, nm, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        names = [b.name for b in app.books]
        if nm in names:
            return app.books[nm]
        time.sleep(0.5)
    raise RuntimeError(f"нет книги {nm}; открыты: {names}")


def try_(label, fn):
    t = time.time()
    try:
        r = fn()
        print(f"  ✓ {label} ({time.time() - t:.1f} с)")
        return r
    except Exception as e:
        print(f"  ✗ {label}: {type(e).__name__}: {str(e)[:400]}")


print("A) обычный экземпляр Excel")
if not xw.apps.count:
    subprocess.run(["open", "-g", "-a", "Microsoft Excel"])
    for _ in range(60):
        if xw.apps.count:
            break
        time.sleep(1)
    time.sleep(2)
app = xw.apps.active
print("   версия Excel:", try_("version", lambda: app.version))
wb = try_("A1 books.open(path)", lambda: app.books.open(path))
if not wb:
    import mactypes
    wb = try_("A2 open(alias)+ожидание", lambda: (app.api.open(mactypes.Alias(path)), wait_book(app, name))[1])
if not wb:
    box = os.path.expanduser("~/Library/Containers/com.microsoft.Excel/Data/cf_agent")
    dst = try_("A3 копия в контейнер Excel", lambda: (os.makedirs(box, exist_ok=True), shutil.copyfile(path, os.path.join(box, name)))[1])
    if dst:
        wb = try_("A3 books.open(контейнер)", lambda: app.books.open(dst))
        if not wb:
            import mactypes
            wb = try_("A4 open(alias контейнер)+ожидание", lambda: (app.api.open(mactypes.Alias(dst)), wait_book(app, name))[1])
if wb:
    sh = wb.sheets[0]
    print("   первый лист:", sh.name, "A1:C3 =", try_("чтение", lambda: sh.range("A1:C3").value))
    try_("пересчёт", lambda: app.calculate())
    try_("закрытие книги", lambda: wb.close())
    print("ГОТОВО: Excel открывает модель — пришлите этот вывод")
else:
    print("НЕ ПОЛУЧИЛОСЬ — пришлите этот вывод целиком")
