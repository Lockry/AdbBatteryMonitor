#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convert SQLite battery_log.db to Excel with one sheet per device (IP:PORT)
Usage: python db_to_excel.py [input.db] [output.xlsx]
"""

import sys
import sqlite3
import re
from pathlib import Path
from datetime import datetime

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import LineChart, Reference

# ==================== CONFIG ====================
DEFAULT_DB = Path("battery_log_08_05_offfff.db")
DEFAULT_OUTPUT = Path("battery_report_off.xlsx")

# Лимиты для безопасности
MAX_ROWS_EXPORT = 100000  # не экспортировать больше этого на лист
MAX_SHEETS = 50  # лимит листов в одном файле


# ==================== HELPERS ====================
def sanitize_sheet_name(name: str) -> str:
    """Превращает имя таблицы в валидное имя листа Excel"""
    # Убираем запрещённые символы: \ / ? * [ ] :
    sanitized = re.sub(r'[\\/\?\*\[\]:]', '_', name)
    # Excel: макс 31 символ, не может начинаться/заканчиваться апострофом
    sanitized = sanitized.strip("'").strip('"')
    return sanitized[:31]


def format_cell_range(ws, range_str: str, **kwargs):
    """Применяет форматирование к диапазону ячеек"""
    for row in ws[range_str]:
        for cell in row:
            for key, value in kwargs.items():
                setattr(cell, key, value)


def auto_adjust_columns(ws, min_width: int = 10, max_width: int = 50):
    """Автоподбор ширины колонок"""
    for col in ws.columns:
        max_length = min_width
        col_letter = get_column_letter(col[0].column)

        for cell in col:
            try:
                if cell.value:
                    length = len(str(cell.value))
                    if length > max_length:
                        max_length = length
            except:
                pass

        ws.column_dimensions[col_letter].width = min(max_length + 2, max_width)


def add_summary_sheet(wb, stats: dict):
    """Добавляет лист с обзором по всем устройствам"""
    ws = wb.create_sheet(title="📊 Summary", index=0)

    # Заголовки
    headers = ["Device", "Total Rows", "Time Range", "Avg Voltage",
               "Avg Current", "Min Battery Temp", "Max Battery Temp", "OK %"]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")

    # Данные
    for row_idx, (device, data) in enumerate(stats.items(), 2):
        ws.cell(row=row_idx, column=1, value=device)
        ws.cell(row=row_idx, column=2, value=data["rows"])
        ws.cell(row=row_idx, column=3, value=data["time_range"])
        ws.cell(row=row_idx, column=4, value=f"{data['avg_voltage']:.1f}mV" if data["avg_voltage"] else "N/A")
        ws.cell(row=row_idx, column=5, value=f"{data['avg_current']:.1f}mA" if data["avg_current"] else "N/A")
        ws.cell(row=row_idx, column=6, value=f"{data['min_temp']}°C" if data["min_temp"] else "N/A")
        ws.cell(row=row_idx, column=7, value=f"{data['max_temp']}°C" if data["max_temp"] else "N/A")
        ws.cell(row=row_idx, column=8, value=f"{data['ok_percent']:.1f}%" if data["ok_percent"] is not None else "N/A")

    auto_adjust_columns(ws)


def create_chart(ws, data_rows: int, chart_type: str = "voltage"):
    """Добавляет простой график на лист"""
    if data_rows < 10:  # слишком мало данных для графика
        return

    chart = LineChart()
    chart.title = f"Battery {chart_type.replace('_', ' ').title()} Over Time"
    chart.style = 13
    chart.y_axis.title = chart_type.replace("_", " ").title()
    chart.x_axis.title = "Sample"

    # Определяем колонку для графика
    col_map = {
        "voltage": 3,  # Voltage (mV)
        "current": 4,  # Current (mA)
        "cpu_temp": 5,  # CPU temp
        "battery_temp": 6,  # Battery Temp #1
        "capacity": 10  # Capacity (mAh)
    }

    col = col_map.get(chart_type, 3)
    col_letter = get_column_letter(col)

    # Данные: пропускаем заголовок
    data = Reference(ws, min_col=col, min_row=2, max_row=min(data_rows + 1, 500))
    chart.add_data(data, titles_from_data=False)

    # Позиция графика
    ws.add_chart(chart, f"{get_column_letter(col + 2)}2")


# ==================== MAIN CONVERTER ====================
def convert_db_to_excel(
        db_path: str | Path,
        output_path: str | Path = None,
        add_charts: bool = True,
        add_summary: bool = True
) -> Path:
    """
    Конвертирует SQLite DB в Excel с листами по устройствам

    Args:
        db_path: Путь к .db файлу
        output_path: Путь для выходного .xlsx (по умолчанию: battery_report.xlsx)
        add_charts: Добавлять ли мини-графики на листы
        add_summary: Добавлять ли сводный лист

    Returns:
        Path к созданному файлу
    """
    db_path = Path(db_path)
    output_path = Path(output_path) if output_path else DEFAULT_OUTPUT

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    print(f"🔍 Reading database: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Получаем список таблиц (устройств)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    if not tables:
        conn.close()
        raise ValueError("No device tables found in database")

    print(f"📦 Found {len(tables)} device tables")

    # Создаём Excel файл
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # удаляем дефолтный лист

    stats = {}  # для сводного листа
    created_sheets = 0

    for table in tables:
        if created_sheets >= MAX_SHEETS:
            print(f"⚠️ Reached max sheets limit ({MAX_SHEETS}), stopping")
            break

        sheet_name = sanitize_sheet_name(table)
        print(f"📄 Processing: {table} → '{sheet_name}'")

        try:
            # Получаем данные
            cursor.execute(f'SELECT * FROM "{table}" ORDER BY timestamp LIMIT {MAX_ROWS_EXPORT}')
            rows = cursor.fetchall()

            if not rows:
                print(f"  ⚠️ No data in table {table}, skipping")
                continue

            # Создаём лист
            ws = wb.create_sheet(title=sheet_name)

            # Заголовки
            headers = [desc[0] for desc in cursor.description]
            for col, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=header)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = Border(
                    bottom=Side(style="thin"),
                    right=Side(style="thin")
                )

            # Данные
            for row_idx, row in enumerate(rows, 2):
                for col_idx, value in enumerate(row, 1):
                    cell = ws.cell(row=row_idx, column=col_idx, value=value)

                    # Цветовая индикация статуса
                    if headers[col_idx - 1] == "status" and value:
                        if value == "OK":
                            cell.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                        elif value == "OFFLINE":
                            cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                        elif value == "NO_DATA":
                            cell.fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")

            # Автоширина колонок
            auto_adjust_columns(ws)

            # График (опционально)
            if add_charts and len(rows) >= 20:
                create_chart(ws, len(rows), chart_type="voltage")

            # Сбор статистики
            voltage_vals = [r["voltage_mv"] for r in rows if r["voltage_mv"] is not None]
            current_vals = [r["current_ma"] for r in rows if r["current_ma"] is not None]
            temp_vals = [r["battery_temp1"] for r in rows if r["battery_temp1"] is not None]
            ok_count = sum(1 for r in rows if r["status"] == "OK")

            time_range = f"{rows[0]['timestamp']} → {rows[-1]['timestamp']}" if len(rows) > 1 else rows[0]["timestamp"]

            stats[table] = {
                "rows": len(rows),
                "time_range": time_range,
                "avg_voltage": sum(voltage_vals) / len(voltage_vals) if voltage_vals else None,
                "avg_current": sum(current_vals) / len(current_vals) if current_vals else None,
                "min_temp": min(temp_vals) if temp_vals else None,
                "max_temp": max(temp_vals) if temp_vals else None,
                "ok_percent": (ok_count / len(rows) * 100) if rows else None
            }

            created_sheets += 1
            print(f"  ✅ {len(rows)} rows exported")

        except Exception as e:
            print(f"  ❌ Error processing {table}: {e}")
            import traceback
            traceback.print_exc()
            continue

    # Сводный лист
    if add_summary and stats:
        add_summary_sheet(wb, stats)
        print(f"📋 Summary sheet added")

    # Сохранение
    print(f"\n💾 Saving to: {output_path}")
    wb.save(output_path)

    conn.close()

    # Итог
    total_rows = sum(s["rows"] for s in stats.values())
    print(f"\n🎉 Done!")
    print(f"   • Sheets created: {created_sheets}")
    print(f"   • Total rows exported: {total_rows:,}")
    print(f"   • Output file: {output_path.resolve()}")
    print(f"   • File size: {output_path.stat().st_size / 1024 / 1024:.2f} MB")

    return output_path


# ==================== CLI ====================
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert battery_log.db to Excel with one sheet per device"
    )
    parser.add_argument("db_path", nargs="?", default=str(DEFAULT_DB),
                        help=f"Path to SQLite database (default: {DEFAULT_DB})")
    parser.add_argument("output_path", nargs="?", default=str(DEFAULT_OUTPUT),
                        help=f"Path for output Excel file (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--no-charts", action="store_true",
                        help="Don't add mini-charts to sheets")
    parser.add_argument("--no-summary", action="store_true",
                        help="Don't add summary sheet")

    args = parser.parse_args()

    try:
        convert_db_to_excel(
            args.db_path,
            args.output_path,
            add_charts=not args.no_charts,
            add_summary=not args.no_summary
        )
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()