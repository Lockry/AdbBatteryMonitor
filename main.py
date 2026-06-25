import subprocess
import time
import threading
import signal
import sqlite3
import logging
import re
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from queue import Queue, Empty as QueueEmpty

# ==================== CONFIG ====================
BASE_DIR = Path(__file__).resolve().parent
DEVICES_FILE = BASE_DIR / "devices.txt"
DB_FILE = BASE_DIR / "battery_log.db"
LOG_FILE = BASE_DIR / "battery_monitor.log"

POLL_INTERVAL_SEC = 5
ADB_TIMEOUT_SEC = 5
MAX_WORKERS = 5
DB_COMMIT_INTERVAL_SEC = 5
DB_QUEUE_MAXSIZE = 3000

ADB_PATH = BASE_DIR / "platform-tools" / "adb.exe"

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def log(message: str, level: str = "info") -> None:
    """Удобный логгер с меткой времени"""
    ts = datetime.now().strftime("%H:%M:%S")
    # "warn" — deprecated alias, используем "warning"
    if level == "warn":
        level = "warning"
    getattr(logger, level)(f"[{ts}] {message}")


# ==================== SQLITE HELPERS ====================
def sanitize_table_name(name: str) -> str:
    """Превращает IP:PORT в валидное имя таблицы SQLite"""
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    if sanitized and sanitized[0].isdigit():
        sanitized = '_' + sanitized
    return sanitized[:64]


def init_database(devices: list[str]) -> None:
    """Создаёт БД и таблицы для каждого устройства"""
    log(f"🗄️ Инициализация базы данных: {DB_FILE.name}")

    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.commit()

    cursor = conn.cursor()

    for device in devices:
        table = sanitize_table_name(device)

        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS "{table}" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                voltage_mv INTEGER,
                current_ma INTEGER,
                cpu_temp1 INTEGER,
                battery_temp1 INTEGER,
                battery_temp2 INTEGER,
                status TEXT NOT NULL,
                test_duration_sec REAL,
                capacity_mah REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute(f'CREATE INDEX IF NOT EXISTS "{table}_ts_idx" ON "{table}"(timestamp)')
        cursor.execute(f'CREATE INDEX IF NOT EXISTS "{table}_status_idx" ON "{table}"(status)')

    conn.commit()
    conn.close()
    log(f"✅ Создано таблиц: {len(devices)}")


# ==================== ADB FUNCTIONS ====================
def ensure_adb_server_running() -> None:
    """Перезапускает ADB сервер"""
    log("🔄 Перезапуск ADB сервера...")
    subprocess.run([str(ADB_PATH), "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    subprocess.run([str(ADB_PATH), "start-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    log("✅ ADB сервер запущен")


def connect_to_all_devices(devices: list[str]) -> dict[str, bool]:
    """Подключается к устройствам, возвращает статус"""
    log("\n--- 🔗 Подключение к устройствам ---")
    status = {}

    for device in devices:
        log(f"  Connecting to {device}...")
        try:
            result = subprocess.run(
                [str(ADB_PATH), "connect", device],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and "connected" in result.stdout.lower():
                log(f"  ✓ Connected to {device}")
                status[device] = True
            else:
                err = result.stderr.strip() or result.stdout.strip()
                log(f"  ✗ Failed to connect to {device}: {err}", "warning")
                status[device] = False
        except Exception as e:
            log(f"  ✗ Error connecting to {device}: {e}", "error")
            status[device] = False

    log(f"--- Подключено: {sum(status.values())}/{len(devices)} ---\n")
    return status


def load_devices() -> list[str]:
    """Загружает список устройств из файла"""
    if not DEVICES_FILE.exists():
        raise FileNotFoundError(f"Файл {DEVICES_FILE} не найден")

    devices = []
    with DEVICES_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            devices.append(line)
    return devices


def is_device_connected(device: str) -> bool:
    """Проверяет доступность устройства"""
    try:
        result = subprocess.run(
            [str(ADB_PATH), "-s", device, "shell", "echo", "ok"],
            capture_output=True, text=True, timeout=ADB_TIMEOUT_SEC
        )
        return result.returncode == 0 and result.stdout.strip() == "ok"
    except Exception:
        return False


def adb_read(device: str, path: str) -> int | None:
    """Читает число из системного файла на устройстве"""
    try:
        result = subprocess.run(
            [str(ADB_PATH), "-s", device, "shell", "cat", path],
            capture_output=True, text=True, timeout=ADB_TIMEOUT_SEC
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return int(value) if value else None
    except (subprocess.TimeoutExpired, ValueError, Exception):
        return None


def poll_device(device: str) -> dict:
    """Опрашивает устройство и возвращает метрики"""
    if not is_device_connected(device):
        return {
            "device": device,
            "voltage_mv": None, "current_ma": None,
            "cpu_temp1": None, "battery_temp1": None, "battery_temp2": None,
            "status": "OFFLINE"
        }

    voltage_raw = adb_read(device, "/sys/class/power_supply/battery/voltage_now")
    current_raw = adb_read(device, "/sys/class/power_supply/battery/current_now")
    cpu_temp1 = adb_read(device, "/sys/class/thermal/thermal_zone5/temp")
    battery_temp1 = adb_read(device, "/sys/class/thermal/thermal_zone27/temp")
    battery_temp2 = adb_read(device, "/sys/class/thermal/thermal_zone28/temp")

    if voltage_raw is None or current_raw is None:
        return {
            "device": device,
            "voltage_mv": None, "current_ma": None,
            "cpu_temp1": None, "battery_temp1": None, "battery_temp2": None,
            "status": "NO_DATA"
        }

    return {
        "device": device,
        "voltage_mv": voltage_raw // 1000,
        "current_ma": current_raw // 1000,
        "cpu_temp1": cpu_temp1 // 1000 if cpu_temp1 else None,
        "battery_temp1": battery_temp1 // 1000 if battery_temp1 else None,
        "battery_temp2": battery_temp2 // 1000 if battery_temp2 else None,
        "status": "OK"
    }


def collect_results(futures: dict[Future, str], db_writer: "DatabaseWriter",
                    test_start: datetime, iteration: int) -> None:
    """
    Собирает результаты futures без бросания TimeoutError наружу.

    Вместо as_completed(..., timeout=X) — который падает если хоть один future
    завис — используем явный поллинг с индивидуальным таймаутом на каждый future.
    Зависший future логируется как TIMEOUT и не роняет всю программу.
    """
    FUTURE_TIMEOUT = ADB_TIMEOUT_SEC + 5  # секунд на один future

    for future, device in futures.items():
        try:
            result = future.result(timeout=FUTURE_TIMEOUT)

            # Лог только для не-OK или раз в минуту
            if result["status"] != "OK" or iteration % 12 == 0:
                status_label = "🔴 OFFLINE" if result["status"] == "OFFLINE" else result["status"]
                log(
                    f"[{device}] {status_label:12s} "
                    f"V={result['voltage_mv']}mV I={result['current_ma']}mA "
                    f"CPU={result['cpu_temp1']}°C BAT={result['battery_temp1']}°C"
                )

            db_writer.enqueue(device, result, test_start)

        except TimeoutError:
            # future.result() тоже может кинуть TimeoutError — перехватываем здесь
            log(f"[{device}] ⏱ Future timeout, пропускаем итерацию", "warning")
            db_writer.enqueue(device, {
                "device": device, "status": "TIMEOUT",
                "voltage_mv": None, "current_ma": None,
                "cpu_temp1": None, "battery_temp1": None, "battery_temp2": None
            }, test_start)

        except Exception as e:
            log(f"[{device}] ❌ Error: {type(e).__name__}: {e}", "error")
            db_writer.enqueue(device, {
                "device": device, "status": "ERROR",
                "voltage_mv": None, "current_ma": None,
                "cpu_temp1": None, "battery_temp1": None, "battery_temp2": None
            }, test_start)


# ==================== DATABASE WRITER ====================
class DatabaseWriter:
    """Потокобезопасный писатель в SQLite с буферизацией"""

    def __init__(self, db_path: Path, devices: list[str], poll_interval: int):
        self.db_path = db_path
        self.poll_interval = poll_interval
        self.queue: Queue = Queue(maxsize=DB_QUEUE_MAXSIZE)
        self.shutdown_flag = False
        self.last_commit_time = time.time()
        self.pending_writes = 0
        self.capacity_cache: dict[str, float] = {
            sanitize_table_name(d): 0.0 for d in devices
        }

    def start(self) -> threading.Thread:
        """Запускает фоновый поток записи"""
        thread = threading.Thread(target=self._worker, daemon=True, name="DBWriter")
        thread.start()
        log("📝 Database writer thread started")
        return thread

    def enqueue(self, device: str, data: dict, test_start: datetime) -> bool:
        """Добавляет запись в очередь (не блокирует основной поток)"""
        if self.queue.full():
            log(f"⚠️ Queue full, dropping data for {device}", "warning")
            return False

        entry = {
            "device": device,
            "table": sanitize_table_name(device),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "data": data,
            "test_start": test_start
        }
        self.queue.put(entry, block=False)
        return True

    def _worker(self) -> None:
        """Фоновый поток: забирает из очереди и пишет в БД"""
        conn = None

        while not self.shutdown_flag or not self.queue.empty():
            try:
                entry = self.queue.get(timeout=1.0)
            except QueueEmpty:
                if conn and self.pending_writes > 0:
                    self._commit_if_needed(conn)
                continue

            try:
                if conn is None:
                    conn = self._get_connection()

                self._write_entry(conn, entry)
                self.pending_writes += 1
                self._commit_if_needed(conn)

            except Exception as e:
                log(f"[ERROR] Write failed for {entry['device']}: {e}", "error")
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = None
            finally:
                self.queue.task_done()

        # Финальный коммит
        if conn:
            try:
                if self.pending_writes > 0:
                    conn.commit()
                    log("✅ Final commit done")
            except Exception as e:
                log(f"[ERROR] Final commit failed: {e}", "error")
            finally:
                conn.close()

        log("📝 Database writer thread stopped")

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _write_entry(self, conn: sqlite3.Connection, entry: dict) -> None:
        table = entry["table"]
        data = entry["data"]
        test_start = entry["test_start"]
        timestamp = entry["timestamp"]

        duration = (datetime.now() - test_start).total_seconds()

        current_ma = data.get("current_ma")
        if current_ma is not None:
            delta = abs(current_ma) * self.poll_interval / 3600
            self.capacity_cache[table] = self.capacity_cache.get(table, 0.0) + delta
        capacity = round(self.capacity_cache.get(table, 0.0), 4)

        conn.execute(f"""
            INSERT INTO "{table}"
            (timestamp, voltage_mv, current_ma, cpu_temp1, battery_temp1,
             battery_temp2, status, test_duration_sec, capacity_mah)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            timestamp,
            data.get("voltage_mv"),
            data.get("current_ma"),
            data.get("cpu_temp1"),
            data.get("battery_temp1"),
            data.get("battery_temp2"),
            data.get("status"),
            duration,
            capacity
        ))

    def _commit_if_needed(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if (now - self.last_commit_time) >= DB_COMMIT_INTERVAL_SEC or self.pending_writes >= 100:
            try:
                conn.commit()
                self.last_commit_time = now
                self.pending_writes = 0
            except Exception as e:
                log(f"[ERROR] Commit failed: {e}", "error")

    def shutdown(self) -> None:
        self.shutdown_flag = True


# ==================== EXPORT FUNCTIONS ====================
def export_device_to_csv(db_path: Path, device: str, output_path: Path = None) -> Path:
    """Экспортирует данные устройства в CSV"""
    import csv
    table = sanitize_table_name(device)
    if output_path is None:
        output_path = db_path.parent / f"{device.replace(':', '_')}.csv"

    conn = sqlite3.connect(db_path, timeout=5.0)
    cursor = conn.cursor()

    try:
        cursor.execute(f'SELECT * FROM "{table}" ORDER BY timestamp')
        columns = [desc[0] for desc in cursor.description]

        with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(cursor.fetchall())

        log(f"✅ Экспортировано: {device} → {output_path.name}")
        return output_path

    finally:
        conn.close()


def export_all_to_csv(db_path: Path, output_dir: Path = None) -> list[Path]:
    """Экспортирует все таблицы в отдельные CSV-файлы"""
    if output_dir is None:
        output_dir = db_path.parent / "csv_export"
    output_dir.mkdir(exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=5.0)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    exported = []
    for table in tables:
        output_path = output_dir / f"{table}.csv"
        export_device_to_csv(db_path, table.replace('_', ':'), output_path)
        exported.append(output_path)

    log(f"✅ Экспортировано таблиц: {len(exported)} в {output_dir}")
    return exported


# ==================== MAIN ====================
def _graceful_shutdown(db_writer: DatabaseWriter) -> None:
    """Корректное завершение: ждём записи в БД"""
    log("\n⏹ Получен сигнал остановки...")
    db_writer.shutdown()
    log("⏳ Ожидание завершения записи...")
    db_writer.queue.join()
    time.sleep(1)
    log("✅ Все данные сохранены в базу")


def main() -> None:
    log(f"🔋 Battery Monitor v2.1 (SQLite)")
    log(f"📁 Directory: {BASE_DIR}")
    log(f"🗄️ Database: {DB_FILE.name}")
    log(f"🔧 ADB: {ADB_PATH} (exists: {ADB_PATH.exists()})")

    if not ADB_PATH.exists():
        log(f"❌ ADB not found: {ADB_PATH}", "error")
        return

    ensure_adb_server_running()

    try:
        devices = load_devices()
    except FileNotFoundError as e:
        log(f"❌ {e}", "error")
        return

    if not devices:
        log("❌ No devices listed", "error")
        return

    connection_status = connect_to_all_devices(devices)
    active_devices = [d for d in devices if connection_status.get(d)]

    if not active_devices:
        log("❌ No devices connected", "error")
        return

    if DB_FILE.exists():
        log(f"🗑️ Удаляем старую БД: {DB_FILE.name}")
        try:
            DB_FILE.unlink()
        except Exception as e:
            log(f"❌ Не удалось удалить БД: {e}", "error")
            return

    init_database(active_devices)
    db_writer = DatabaseWriter(DB_FILE, active_devices, POLL_INTERVAL_SEC)
    db_writer.start()

    # Сигналы завершения — не бросают исключений, просто выставляют флаг
    shutdown_requested = threading.Event()

    def _signal_handler(signum, frame):
        shutdown_requested.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    test_start = datetime.now()
    log(f"\n🚀 Start: {len(active_devices)} devices, {POLL_INTERVAL_SEC}s interval")
    log("💡 Press Ctrl+C to stop gracefully\n")

    iteration = 0
    start_time = time.time()

    try:
        while not shutdown_requested.is_set():
            iteration += 1

            # ThreadPoolExecutor пересоздаём каждую итерацию — зависшие воркеры
            # не накапливаются, каждый future живёт не дольше одного цикла
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(poll_device, d): d for d in active_devices}
                collect_results(futures, db_writer, test_start, iteration)
                # executor.__exit__ отменяет незавершённые futures и ждёт воркеров,
                # но не блокирует дольше естественного завершения потоков

            if iteration % 12 == 0:
                elapsed = (time.time() - start_time) / 60
                queue_size = db_writer.queue.qsize()
                log(f"⏱ {elapsed:.1f}min | iter:{iteration} | queue:{queue_size}")

            # sleep с возможностью прерваться по сигналу
            shutdown_requested.wait(timeout=POLL_INTERVAL_SEC)

    except Exception as e:
        log(f"[FATAL] {type(e).__name__}: {e}", "error")
        logger.exception("Stack trace")
    finally:
        _graceful_shutdown(db_writer)

        log(f"\n💡 Данные сохранены в: {DB_FILE}")
        log("💡 Для экспорта в CSV запустите:")
        log(f'   python -c "from main import export_all_to_csv; export_all_to_csv(\'{ DB_FILE }\')"')


if __name__ == "__main__":
    main()