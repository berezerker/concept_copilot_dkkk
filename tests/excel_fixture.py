"""Заполнить кэш формул синтетической книги через выбранный Excel, без soffice."""
from app.workbook.engine import ExcelEngine


def calculate_fixture(path):
    engine = ExcelEngine()
    try:
        engine.open(path)
        def calculate_and_save():
            engine._converge([])
            engine.wb.save()  # только созданный тестом файл; без Save As на macOS
        engine._submit(calculate_and_save)
    finally:
        engine.close()
    return path
