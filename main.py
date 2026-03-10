import subprocess
import time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

BASE_DIR = Path(__file__).resolve().parent
DEVICES_FILE = BASE_DIR / "devices.txt"  # файл со списком IP:PORT
EXCEL_FILE = BASE_DIR / "battery_log.xlsx"  # итоговый Excel файл

POLL_INTERVAL_SEC = 5
ADB_TIMEOUT_SEC = 60
MAX_WORKERS = 16

# Указываем локальный путь к adb.exe
ADB_PATH = BASE_DIR / "platform-tools" / "adb.exe"

print("ADB path:", ADB_PATH)
print("Exists:", ADB_PATH.exists())

# Глобальная переменная для отслеживания времени начала теста и ёмкости
TEST_START_TIME = None
CAPACITY_PER_DEVICE = {}  # словарь: {device: capacity_mah}

def ensure_adb_server_running():
    subprocess.run([str(ADB_PATH), "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(10)
    subprocess.run([str(ADB_PATH), "start-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(10)  # даем время серверу запуститься

def connect_to_all_devices(devices: list[str]):
    print("\n--- Connecting to all devices ---")
    for device in devices:
        print(f"Connecting to {device}...")
        result = subprocess.run(
            [str(ADB_PATH), "connect", device],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"✓ Connected to {device}")
        else:
            print(f"✗ Failed to connect to {device}: {result.stderr.strip()}")
    print("--- Connection process completed ---\n")

def load_devices() -> list[str]:
    if not DEVICES_FILE.exists():
        raise FileNotFoundError(f"Файл {DEVICES_FILE} не найден")

    devices: list[str] = []

    with DEVICES_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            devices.append(line)

    return devices

def is_device_connected(device: str) -> bool:
    try:
        result = subprocess.run(
            [str(ADB_PATH), "-s", device, "shell", "echo", "ok"],
            capture_output=True,
            text=True,
            timeout=ADB_TIMEOUT_SEC
        )
        if result.returncode != 0:
            print(f"[DEBUG] Device {device} error: {result.stderr.strip()}")
            return False
        output = result.stdout.strip()
        if output != "ok":
            print(f"[DEBUG] Device {device} unexpected output: '{output}'")
            return False
        return True
    except Exception as e:
        print(f"[DEBUG] Device {device} exception: {e}")
        return False

def adb_read(device: str, path: str) -> int | None:
    try:
        result = subprocess.run(
            [str(ADB_PATH), "-s", device, "shell", "cat", path],
            capture_output=True,
            text=True,
            timeout=ADB_TIMEOUT_SEC
        )

        if result.returncode != 0:
            print(f"[DEBUG] Read fail on {device}, stderr: {result.stderr.strip()}")
            return None

        value_str = result.stdout.strip()
        if not value_str:
            return None

        return int(value_str)

    except (subprocess.TimeoutExpired, ValueError) as e:
        print(f"[DEBUG] Read error on {device}: {e}")
        return None

def poll_device(device: str) -> dict:
    if not is_device_connected(device):
        return {
            "device": device,
            "voltage_mv": None,
            "current_ma": None,
            "cpu_temp1": None,
            "battery_temp1": None,
            "battery_temp2": None,
            "status": "OFFLINE"
        }

    voltage_raw = adb_read(device, "/sys/class/power_supply/battery/voltage_now")
    current_raw = adb_read(device, "/sys/class/power_supply/battery/current_now")
    cpu_temp1 = adb_read(device, "/sys/class/thermal/thermal_zone5/temp")
    battery_temp1 = adb_read(device, "/sys/class/thermal/thermal_zone27/temp")
    battery_temp2 = adb_read(device, "/sys/class/thermal/thermal_zone27/temp")

    if voltage_raw is None or current_raw is None:
        return {
            "device": device,
            "voltage_mv": None,
            "current_ma": None,
            "cpu_temp1": None,
            "battery_temp1": None,
            "battery_temp2": None,
            "status": "NO_DATA"
        }

    return {
        "device": device,
        "voltage_mv": voltage_raw // 1000,
        "current_ma": current_raw // 1000,
        "cpu_temp1": cpu_temp1 // 1000,
        "battery_temp1": battery_temp1 // 1000,
        "battery_temp2": battery_temp2 // 1000,
        "status": "OK"
    }

def init_excel(devices: list[str]) -> None:
    global TEST_START_TIME, CAPACITY_PER_DEVICE
    TEST_START_TIME = datetime.now()

    # Инициализируем ёмкость для каждого устройства
    for device in devices:
        CAPACITY_PER_DEVICE[device] = 0.0

    wb = openpyxl.Workbook()

    # Удаляем дефолтный лист
    default_sheet = wb.active
    wb.remove(default_sheet)

    for device in devices:
        ws = wb.create_sheet(title=device.replace(':', '_'))  # ':' нельзя в имени листа

        headers = [
            "Timestamp",
            "Device",
            "Voltage (mV)",
            "Current (mA)",
            "CPU temp(cpu_big1)",
            "Battery Temp #1",
            "Battery Temp #2",
            "Status",
            "Test Duration (sec)",
            "Capacity (mAh)"
        ]

        for col_num, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num, value=header)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")

    wb.save(EXCEL_FILE)

def append_to_excel_sheet(device: str, row_data: list) -> None:
    global CAPACITY_PER_DEVICE
    wb = openpyxl.load_workbook(EXCEL_FILE)

    sheet_name = device.replace(':', '_')
    ws = wb[sheet_name]

    # Добавляем время с начала теста
    test_duration = (datetime.now() - TEST_START_TIME).total_seconds()
    row_data.append(int(test_duration))

    # Рассчитываем накопленную ёмкость (mAh)
    current_ma = row_data[3]  # индекс 3 — Current (mA)
    if current_ma is not None:
        # Δt = POLL_INTERVAL_SEC (5 секунд)
        # Q = I * t / 3600 (в mAh)
        delta_capacity = abs(current_ma) * POLL_INTERVAL_SEC / 3600
        CAPACITY_PER_DEVICE[device] += delta_capacity
    else:
        delta_capacity = 0

    row_data.append(round(CAPACITY_PER_DEVICE[device], 4))  # округляем до 4 знаков

    ws.append(row_data)

    # Автоширина колонок
    for col in ws.columns:
        max_length = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[col_letter].width = adjusted_width

    # Цветовая индикация строки в зависимости от статуса
    status = row_data[4]  # индекс 4 — Status
    fill_color = None
    if status == "OK":
        fill_color = PatternFill(start_color="D5F5E3", end_color="D5F5E3", fill_type="solid")  # зелёный
    elif status == "OFFLINE":
        fill_color = PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid")  # красный
    elif status == "NO_DATA":
        fill_color = PatternFill(start_color="FEF9E7", end_color="FEF9E7", fill_type="solid")  # жёлтый

    if fill_color:
        for col_num in range(1, len(row_data) + 1):
            cell = ws.cell(row=ws.max_row, column=col_num)
            cell.fill = fill_color

    wb.save(EXCEL_FILE)

def main() -> None:
    global TEST_START_TIME
    ensure_adb_server_running()  # <-- запускаем сервер перед основным кодом
    devices = load_devices()

    if not devices:
        print("Список устройств пуст")
        return

    connect_to_all_devices(devices)  # <-- подключаемся ко всем устройствам

    init_excel(devices)

    print(f"Найдено устройств: {len(devices)}")

    while True:
        timestamp = datetime.now().isoformat(timespec="seconds")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(poll_device, d) for d in devices]

            for future in as_completed(futures):
                result = future.result()
                row = [
                    timestamp,
                    result["device"],
                    result["voltage_mv"],
                    result["current_ma"],
                    result["cpu_temp1"],
                    result["battery_temp1"],
                    result["battery_temp2"],
                    result["status"],
                ]

                print(
                    f"[{result['device']}] "
                    f"{result['status']} "
                    f"V={result['voltage_mv']}mV "
                    f"I={result['current_ma']}mA"
                    f"I={result['cpu_temp1']}C"
                    f"I={result['battery_temp1']}C"
                    f"I={result['battery_temp2']}C"
                )

                append_to_excel_sheet(result["device"], row)

        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()