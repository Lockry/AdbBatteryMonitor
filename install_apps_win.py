import subprocess
import logging
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future

# ==================== CONFIG ====================
BASE_DIR = Path(__file__).resolve().parent
DEVICES_FILE = BASE_DIR / "devices.txt"
APK_PATH = BASE_DIR / "apps" / "debug.apk"
LOG_FILE = BASE_DIR / "install_apps.log"

CONNECT_TIMEOUT_SEC = 30
INSTALL_TIMEOUT_SEC = 180   # apk может быть большим — даём запас
MAX_WORKERS = 5

ADB_PATH = BASE_DIR / "platform-tools" / "adb.exe"

# Флаги adb install:
#   -r  переустановить, сохранив данные приложения
#   -d  разрешить установку с меньшим versionCode (downgrade)
# Если нужна гарантированно чистая установка — можно сначала делать uninstall.
INSTALL_FLAGS = ["-r", "-d"]

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
    if level == "warn":
        level = "warning"
    getattr(logger, level)(f"[{ts}] {message}")


# ==================== DEVICES ====================
def load_devices() -> list[str]:
    """Загружает список устройств (IP:PORT) из файла"""
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


def connect_to_all_devices(devices: list[str]) -> dict[str, bool]:
    """Подключается к устройствам по adb connect, возвращает статус"""
    log("\n--- 🔗 Подключение к устройствам ---")
    status = {}

    for device in devices:
        log(f"  Connecting to {device}...")
        try:
            result = subprocess.run(
                [str(ADB_PATH), "connect", device],
                capture_output=True, text=True, timeout=CONNECT_TIMEOUT_SEC
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


# ==================== INSTALL ====================
def install_apk(device: str, apk_path: Path) -> dict:
    """Устанавливает APK на устройство, возвращает результат"""
    try:
        cmd = [str(ADB_PATH), "-s", device, "install", *INSTALL_FLAGS, str(apk_path)]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SEC
        )
        output = (result.stdout + result.stderr).strip()

        if result.returncode == 0 and "Success" in output:
            return {"device": device, "status": "OK", "message": output.splitlines()[-1] if output else "Success"}
        else:
            return {"device": device, "status": "FAILED", "message": output or "Unknown error"}

    except subprocess.TimeoutExpired:
        return {"device": device, "status": "TIMEOUT", "message": f"Install timed out after {INSTALL_TIMEOUT_SEC}s"}
    except Exception as e:
        return {"device": device, "status": "ERROR", "message": f"{type(e).__name__}: {e}"}


def collect_install_results(futures: dict[Future, str]) -> dict[str, dict]:
    """Собирает результаты установки, не роняя программу на зависшем future"""
    results = {}
    FUTURE_TIMEOUT = INSTALL_TIMEOUT_SEC + 10

    for future, device in futures.items():
        try:
            result = future.result(timeout=FUTURE_TIMEOUT)
        except TimeoutError:
            result = {"device": device, "status": "TIMEOUT", "message": "Future timeout"}
        except Exception as e:
            result = {"device": device, "status": "ERROR", "message": f"{type(e).__name__}: {e}"}

        status_icon = "✅" if result["status"] == "OK" else "❌"
        log(f"[{device}] {status_icon} {result['status']}: {result['message']}")
        results[device] = result

    return results


# ==================== MAIN ====================
def main() -> None:
    log(f"📦 APK Installer (Windows)")
    log(f"📁 Directory: {BASE_DIR}")
    log(f"📱 APK: {APK_PATH}")
    log(f"🔧 ADB: {ADB_PATH} (exists: {ADB_PATH.exists()})")

    if not ADB_PATH.exists():
        log(f"❌ ADB not found: {ADB_PATH}", "error")
        log("💡 Положи adb.exe в ./platform-tools/adb.exe", "error")
        return

    if not APK_PATH.exists():
        log(f"❌ APK не найден: {APK_PATH}", "error")
        return

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

    log(f"\n🚀 Установка на {len(active_devices)} устройств(о)...\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(install_apk, d, APK_PATH): d
            for d in active_devices
        }
        results = collect_install_results(futures)

    # ==================== SUMMARY ====================
    ok_count = sum(1 for r in results.values() if r["status"] == "OK")
    failed = [d for d, r in results.items() if r["status"] != "OK"]

    log(f"\n--- 📊 Итог ---")
    log(f"✅ Успешно: {ok_count}/{len(active_devices)}")
    if failed:
        log(f"❌ Неудачно: {len(failed)}", "warning")
        for d in failed:
            log(f"   - {d}: {results[d]['status']} — {results[d]['message']}", "warning")


if __name__ == "__main__":
    main()