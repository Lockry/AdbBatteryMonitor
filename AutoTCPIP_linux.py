import subprocess
import time
import logging
import os
import re
import stat
import shutil
from pathlib import Path
from datetime import datetime

# ==================== CONFIG ====================
BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "auto_tcpip.log"

POLL_INTERVAL_SEC = 2          # как часто проверяем adb devices
TCPIP_PORT = 5555
TCPIP_TIMEOUT_SEC = 15         # таймаут самой команды adb tcpip

# Регулярка для распознавания "устройство уже в сети" (формат IP:PORT),
# такие записи в adb devices пропускаем — они не USB
NETWORK_DEVICE_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")


def resolve_adb_path() -> Path:
    """
    Ищет adb в трёх местах (в порядке приоритета):
      1) ./platform-tools-linux/adb рядом со скриптом
      2) $ANDROID_HOME/platform-tools/adb или $ANDROID_SDK_ROOT/platform-tools/adb
      3) adb, найденный в PATH
    """
    binary_name = "adb"

    local_candidate = BASE_DIR / "platform-tools-linux" / binary_name
    if local_candidate.exists():
        return local_candidate

    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk_root = os.environ.get(env_var)
        if sdk_root:
            candidate = Path(sdk_root) / "platform-tools" / binary_name
            if candidate.exists():
                return candidate

    which_result = shutil.which(binary_name)
    if which_result:
        return Path(which_result)

    return local_candidate


def ensure_executable(path: Path) -> None:
    """На Linux бинарник adb должен иметь бит на исполнение."""
    try:
        if path.exists():
            current_mode = path.stat().st_mode
            if not (current_mode & stat.S_IXUSR):
                path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass


ADB_PATH = resolve_adb_path()
ensure_executable(ADB_PATH)

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


# ==================== ADB HELPERS ====================
def list_devices() -> dict[str, str]:
    """
    Возвращает {device_id: status} из `adb devices`.
    device_id может быть как USB-серийником, так и IP:PORT (для уже подключённых по сети).
    status обычно 'device', 'unauthorized', 'offline'.
    """
    try:
        result = subprocess.run(
            [str(ADB_PATH), "devices"],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        log(f"❌ Ошибка вызова adb devices: {e}", "error")
        return {}

    devices = {}
    for line in result.stdout.strip().splitlines()[1:]:  # первая строка — заголовок
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 2:
            device_id, status = parts[0], parts[1]
            devices[device_id] = status
    return devices


def is_usb_device(device_id: str) -> bool:
    """USB-устройство отличаем от сетевого по формату id (сетевые — вида IP:PORT)"""
    return not NETWORK_DEVICE_RE.match(device_id)


def enable_tcpip(device_id: str, port: int) -> bool:
    """Переводит adbd на устройстве в TCP-режим"""
    try:
        result = subprocess.run(
            [str(ADB_PATH), "-s", device_id, "tcpip", str(port)],
            capture_output=True, text=True, timeout=TCPIP_TIMEOUT_SEC
        )
        return result.returncode == 0
    except Exception as e:
        log(f"❌ Ошибка adb tcpip для {device_id}: {e}", "error")
        return False


# ==================== MAIN LOGIC ====================
def handle_new_usb_device(device_id: str) -> bool:
    """Переводит устройство в TCP-режим на порту TCPIP_PORT. Без connect и без ожидания IP."""
    log(f"🔌 Новое USB-устройство: {device_id} — переводим в TCP-режим...")

    if enable_tcpip(device_id, TCPIP_PORT):
        log(f"  ✅ {device_id} переведён в tcpip:{TCPIP_PORT}")
        return True
    else:
        log(f"  ✗ Не удалось выполнить adb tcpip для {device_id}", "warning")
        return False


def main() -> None:
    log("📡 Auto TCPIP Watcher (Linux, быстрый режим — без connect)")
    log(f"📁 Directory: {BASE_DIR}")
    log(f"🔧 ADB: {ADB_PATH} (exists: {ADB_PATH.exists()})")
    log(f"🔌 Порт для tcpip: {TCPIP_PORT}")
    log(f"⏱ Интервал опроса: {POLL_INTERVAL_SEC}s")

    if not ADB_PATH.exists():
        log(f"❌ ADB not found: {ADB_PATH}", "error")
        log("💡 Установи Android platform-tools:", "error")
        log("   sudo apt install android-tools-adb", "error")
        log("   ...или положи бинарник adb в ./platform-tools-linux/adb", "error")
        return

    log("👀 Слежу за подключением USB-устройств. Ctrl+C для остановки.\n")

    # USB-устройства, которые уже обработаны (успешно или окончательно неудачно в этом запуске)
    processed: set[str] = set()

    try:
        while True:
            devices = list_devices()

            for device_id, status in devices.items():
                if not is_usb_device(device_id):
                    continue  # это уже сетевое подключение (IP:PORT) — не трогаем

                if device_id in processed:
                    continue  # уже обработали в этом запуске скрипта

                if status == "unauthorized":
                    log(f"⚠️ {device_id} подключено, но не авторизовано — "
                        f"подтвердите запрос 'Разрешить USB-debugging' на экране устройства", "warning")
                    continue  # не помечаем как processed — попробуем снова на следующем цикле

                if status != "device":
                    continue  # offline и прочие промежуточные статусы — ждём

                # Новое авторизованное USB-устройство — обрабатываем
                handle_new_usb_device(device_id)
                processed.add(device_id)  # не повторяем попытку до перезапуска скрипта

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        log("\n⏹ Остановлено пользователем (Ctrl+C)")


if __name__ == "__main__":
    main()