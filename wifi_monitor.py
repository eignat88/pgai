#!/usr/bin/env python3
"""Wi-Fi monitoring script for Windows 10/11.

Collects Wi-Fi and connectivity metrics every interval and writes CSV/JSONL logs.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import locale
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "check_interval_sec": 1,
    "ping_targets": ["8.8.8.8", "1.1.1.1"],
    "latency_threshold_ms": 1000,
    "failures_before_outage": 3,
    "log_format": "csv",
    "log_file": "wifi_monitor_log.csv",
}

CSV_FIELDS = [
    "timestamp",
    "ssid",
    "bssid",
    "signal_percent",
    "signal_dbm",
    "signal_quality",
    "internet_available",
    "network_status",
    "fail_count",
    "comment",
    "signal",
    "radio_type",
    "channel",
    "rx_rate",
    "tx_rate",
    "connection_status",
    "ping_status",
    "latency_ms",
    "packet_loss",
    "target",
    "is_connected",
    "error_count",
    "outage_duration_sec",
    "event",
    "description",
    "severity",
    "error",
]


@dataclass
class MonitorState:
    previous_connected: bool | None = None
    previous_ssid: str = ""
    previous_bssid: str = ""
    previous_internet: bool | None = None
    outage_active: bool = False
    consecutive_failures: int = 0
    outage_started_at: float | None = None


@dataclass
class Logger:
    log_path: Path
    log_format: str
    _csv_initialized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, row: dict[str, Any]) -> None:
        if self.log_format == "jsonl":
            self._write_jsonl(row)
        else:
            self._write_csv(row)

    def _write_csv(self, row: dict[str, Any]) -> None:
        try:
            exists = self.log_path.exists()
            with self.log_path.open("a", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
                if not exists:
                    writer.writeheader()
                writer.writerow(self._prepare_csv_row(row))
            self._csv_initialized = True
        except Exception as exc:  # noqa: BLE001
            print(f"[ОШИБКА] Не удалось записать CSV-лог: {exc}", file=sys.stderr)

    def _prepare_csv_row(self, row: dict[str, Any]) -> dict[str, Any]:
        csv_row = {k: row.get(k, "") for k in CSV_FIELDS}
        if isinstance(csv_row.get("internet_available"), bool):
            csv_row["internet_available"] = "Да" if csv_row["internet_available"] else "Нет"
        if isinstance(csv_row.get("is_connected"), bool):
            csv_row["is_connected"] = "Да" if csv_row["is_connected"] else "Нет"
        packet_loss = row.get("packet_loss")
        if isinstance(packet_loss, (int, float)):
            csv_row["packet_loss"] = f"{packet_loss:.0f}%"
        return csv_row

    def _write_jsonl(self, row: dict[str, Any]) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as file_obj:
                file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            print(f"[ОШИБКА] Не удалось записать JSONL-лог: {exc}", file=sys.stderr)


def _decode_output(raw: bytes) -> str:
    encodings = [
        locale.getpreferredencoding(False),
        "utf-8",
        "cp866",
        "cp1251",
    ]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run_command(command: list[str], timeout: int = 5) -> tuple[bool, str, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=False, timeout=timeout, shell=False)
        stdout = _decode_output(result.stdout)
        stderr = _decode_output(result.stderr)
        return result.returncode == 0, stdout, stderr
    except Exception as exc:  # noqa: BLE001
        return False, "", str(exc)


WINDOWS_WIFI_KEY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "state": ("state", "состояние"),
    "ssid": ("ssid",),
    "bssid": ("bssid",),
    "signal": ("signal", "сигнал"),
    "radio_type": ("radio type", "тип радиомодуля"),
    "channel": ("channel", "канал"),
    "rx_rate": (
        "receive rate (mbps)",
        "receive rate",
        "скорость приема (мбит/с)",
        "скорость приема",
    ),
    "tx_rate": (
        "transmit rate (mbps)",
        "transmit rate",
        "скорость передачи (мбит/с)",
        "скорость передачи",
    ),
}

WINDOWS_WIFI_STATE_CONNECTED = {"connected", "подключено"}
WINDOWS_WIFI_STATE_DISCONNECTED = {"disconnected", "отключено"}


def normalize_windows_key(key: str) -> str:
    normalized = re.sub(r"[\s\.]+", " ", key.strip().lower())
    return normalized.strip(" :")


def parse_windows_key_value_block(raw_text: str | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not raw_text:
        return parsed

    for line in raw_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[normalize_windows_key(key)] = value.strip()
    return parsed


def collect_wifi_metrics() -> tuple[dict[str, Any], str | None]:
    ok, out, err = run_command(["netsh", "wlan", "show", "interfaces"])
    if not ok:
        return {
            "is_connected": False,
            "connection_status": "DISCONNECTED",
            "ssid": "",
            "bssid": "",
            "signal": "",
            "radio_type": "",
            "channel": "",
            "rx_rate": "",
            "tx_rate": "",
        }, f"ошибка netsh: {err or 'неизвестная ошибка'}"

    data = parse_windows_key_value_block(out)

    def get_field(alias_name: str) -> str:
        for synonym in WINDOWS_WIFI_KEY_SYNONYMS.get(alias_name, (alias_name,)):
            value = data.get(normalize_windows_key(synonym))
            if value is not None:
                return value
        return ""

    state = get_field("state").strip().lower()
    if state in WINDOWS_WIFI_STATE_CONNECTED:
        is_connected = True
    elif state in WINDOWS_WIFI_STATE_DISCONNECTED:
        is_connected = False
    else:
        is_connected = False

    metrics = {
        "is_connected": is_connected,
        "connection_status": "CONNECTED" if is_connected else "DISCONNECTED",
        "ssid": get_field("ssid"),
        "bssid": get_field("bssid"),
        "signal": get_field("signal"),
        "radio_type": get_field("radio_type"),
        "channel": get_field("channel"),
        "rx_rate": get_field("rx_rate"),
        "tx_rate": get_field("tx_rate"),
    }

    missing_aliases = [alias for alias in ("state", "signal", "radio_type") if not get_field(alias)]
    diagnostics: list[str] = []
    if state and state not in WINDOWS_WIFI_STATE_CONNECTED | WINDOWS_WIFI_STATE_DISCONNECTED:
        diagnostics.append(f"неизвестное значение state='{state}'")
    if missing_aliases:
        diagnostics.append(f"не найдены ключи: {', '.join(missing_aliases)}")

    return metrics, ("; ".join(diagnostics) if diagnostics else None)


def parse_default_gateway() -> str | None:
    ok, out, _ = run_command(["ipconfig"])
    if not ok:
        return None
    match = re.search(r"Default Gateway[ .:]*([\d.]+)", out)
    return match.group(1) if match else None


def ping_target(target: str) -> dict[str, Any]:
    ok, out, err = run_command(["ping", "-n", "1", "-w", "900", target], timeout=3)
    if not ok and not out:
        return {
            "target": target,
            "ping_status": "FAIL",
            "latency_ms": None,
            "packet_loss": 100.0,
            "error": f"ошибка ping: {err or 'неизвестная ошибка'}",
        }

    loss_match = re.search(r"\((\d+)% loss\)", out)
    time_match = re.search(r"time[=<](\d+)ms", out)
    success = "TTL=" in out.upper() and "unreachable" not in out.lower()

    return {
        "target": target,
        "ping_status": "OK" if success else "FAIL",
        "latency_ms": int(time_match.group(1)) if time_match else None,
        "packet_loss": float(loss_match.group(1)) if loss_match else None,
        "error": "" if success else (err.strip() or "пинг не выполнен"),
    }


def choose_ping_result(targets: list[str]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for target in targets:
        result = ping_target(target)
        if result["ping_status"] == "OK":
            return result
        failures.append(result)
    return failures[0] if failures else {"target": "", "ping_status": "FAIL", "latency_ms": None, "packet_loss": None, "error": "нет целей для пинга"}


def parse_signal_percent(signal: str) -> int | None:
    if not signal:
        return None
    cleaned = signal.replace("%", "").strip()
    if not cleaned.isdigit():
        return None
    return int(cleaned)


def classify_signal_quality(signal_dbm: int | None) -> str:
    if signal_dbm is None:
        return ""
    if signal_dbm >= -60:
        return "Отличное"
    if -67 <= signal_dbm <= -61:
        return "Хорошее"
    if -75 <= signal_dbm <= -68:
        return "Слабое"
    return "Плохое"


def resolve_network_status(
    row: dict[str, Any],
    latency_threshold_ms: int,
    failures_before_outage: int,
    consecutive_failures: int,
) -> str:
    if not row.get("is_connected"):
        return "Wi‑Fi отключен"
    if row.get("ping_status") == "FAIL" or not row.get("internet_available"):
        return "Нет интернета"
    latency = row.get("latency_ms")
    if isinstance(latency, int) and latency > latency_threshold_ms:
        return "Высокая задержка"
    if consecutive_failures >= failures_before_outage:
        return "Нестабильная сеть"
    signal_dbm = row.get("signal_dbm")
    if isinstance(signal_dbm, int) and signal_dbm < -75:
        return "Слабый сигнал"
    return "Нормально"


def detect_event(state: MonitorState, row: dict[str, Any], failures_before_outage: int, latency_threshold_ms: int) -> str:
    events: list[str] = []
    connected = bool(row["is_connected"])
    internet = bool(row["internet_available"])

    if state.previous_connected is False and connected:
        events.append("Подключение к Wi‑Fi")
    if state.previous_connected is True and not connected:
        events.append("Отключение Wi‑Fi")

    if connected and state.previous_ssid and row["ssid"] and state.previous_ssid != row["ssid"]:
        events.append("Смена SSID")
    if connected and state.previous_bssid and row["bssid"] and state.previous_bssid != row["bssid"]:
        events.append("Смена BSSID")

    if row["ping_status"] == "FAIL":
        events.append("Потери пакетов")
    latency = row.get("latency_ms")
    if isinstance(latency, int) and latency > latency_threshold_ms:
        events.append("Высокая задержка")

    if state.previous_internet is False and internet:
        events.append("Восстановление сети")

    if not state.outage_active and state.consecutive_failures >= failures_before_outage:
        state.outage_active = True
        events.append("Начало обрыва")
    elif state.outage_active and state.consecutive_failures == 0:
        state.outage_active = False
        events.append("Восстановление сети")

    state.previous_connected = connected
    state.previous_ssid = str(row["ssid"] or "")
    state.previous_bssid = str(row["bssid"] or "")
    state.previous_internet = internet

    signal_dbm = row.get("signal_dbm")
    if isinstance(signal_dbm, int) and signal_dbm < -75:
        events.append("Слабый сигнал")

    return "|".join(dict.fromkeys(events))




def build_event_metadata(row: dict[str, Any]) -> tuple[str, str, str]:
    events = [event.strip() for event in str(row.get("event", "")).split("|") if event.strip()]

    if row.get("error"):
        return (
            "Зафиксирована ошибка выполнения диагностических команд.",
            f"Диагностика сети завершилась ошибкой: {row['error']}",
            "error",
        )

    if "Начало обрыва" in events or row.get("ping_status") == "FAIL":
        return (
            "Обнаружены потери доступности сети или пакетов.",
            "Началась деградация: отсутствует интернет-связность или потери пакетов.",
            "error",
        )

    if "Высокая задержка" in events:
        return (
            "Задержка превышает допустимый порог.",
            "Качество сети ухудшено из-за высокой задержки ответа.",
            "warn",
        )

    if "Слабый сигнал" in events:
        return (
            "Уровень сигнала Wi-Fi ниже рекомендуемого.",
            "Качество сети ухудшено из-за слабого сигнала Wi-Fi.",
            "warn",
        )

    if "Отключение Wi‑Fi" in events:
        return (
            "Wi-Fi соединение разорвано.",
            "Соединение с точкой доступа Wi-Fi потеряно.",
            "warn",
        )

    if "Подключение к Wi‑Fi" in events or "Восстановление сети" in events:
        return (
            "Сетевое подключение доступно.",
            "Подключение к сети активно и интернет доступен.",
            "info",
        )

    return ("Отклонений не обнаружено.", "Сеть работает в штатном режиме.", "info")

def load_config(path: Path | None) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    if path is None:
        return config
    if not path.exists():
        raise FileNotFoundError(f"Файл конфигурации не найден: {path}")
    with path.open(encoding="utf-8") as file_obj:
        loaded = json.load(file_obj)
    if not isinstance(loaded, dict):
        raise ValueError("Конфигурация должна быть JSON-объектом")
    config.update(loaded)
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Скрипт мониторинга Wi-Fi для Windows")
    parser.add_argument("--config", type=Path, help="Путь к config.json")
    parser.add_argument("--log", type=Path, help="Переопределить путь к лог-файлу")
    parser.add_argument("--interval", type=float, help="Переопределить интервал проверки в секундах")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"[ОШИБКА] Не удалось загрузить конфигурацию: {exc}", file=sys.stderr)
        return 1

    if args.log:
        config["log_file"] = str(args.log)
    if args.interval is not None:
        config["check_interval_sec"] = args.interval

    interval = float(config.get("check_interval_sec", 1))
    latency_threshold_ms = int(config.get("latency_threshold_ms", 1000))
    failures_before_outage = int(config.get("failures_before_outage", 3))
    log_format = str(config.get("log_format", "csv")).lower()
    log_file = Path(str(config.get("log_file", "wifi_monitor_log.csv")))

    if log_format not in {"csv", "jsonl"}:
        print("[ПРЕДУПРЕЖДЕНИЕ] Неизвестный log_format, используется csv", file=sys.stderr)
        log_format = "csv"

    targets = list(config.get("ping_targets", []))
    gateway = parse_default_gateway()
    if gateway and gateway not in targets:
        targets.append(gateway)
    if not targets:
        targets = ["8.8.8.8"]

    logger = Logger(log_file, log_format)
    state = MonitorState()

    print(f"Запуск монитора Wi-Fi. Интервал={interval}с, лог={log_file}, формат={log_format}")

    while True:
        loop_started = time.time()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row: dict[str, Any] = {
            "timestamp": timestamp,
            "ssid": "",
            "bssid": "",
            "signal": "",
            "signal_percent": "",
            "signal_dbm": "",
            "signal_quality": "",
            "internet_available": False,
            "network_status": "Нормально",
            "fail_count": 0,
            "comment": "",
            "radio_type": "",
            "channel": "",
            "rx_rate": "",
            "tx_rate": "",
            "connection_status": "DISCONNECTED",
            "ping_status": "FAIL",
            "latency_ms": None,
            "packet_loss": None,
            "target": "",
            "is_connected": False,
            "error_count": 0,
            "outage_duration_sec": 0,
            "event": "",
            "description": "",
            "severity": "info",
            "error": "",
        }

        try:
            wifi_metrics, wifi_error = collect_wifi_metrics()
            row.update(wifi_metrics)
            if wifi_error:
                row["error"] = wifi_error

            ping_result = choose_ping_result(targets)
            row.update(ping_result)

            internet_ok = row["ping_status"] == "OK"
            row["internet_available"] = internet_ok

            signal_percent = parse_signal_percent(str(row.get("signal", "")))
            if signal_percent is not None:
                row["signal_percent"] = signal_percent
                signal_dbm = int(round((signal_percent / 2) - 100))
                row["signal_dbm"] = signal_dbm
                row["signal_quality"] = classify_signal_quality(signal_dbm)

            failed = (not row["is_connected"]) or (not row["ssid"]) or (not internet_ok)
            latency = row.get("latency_ms")
            if isinstance(latency, int) and latency > latency_threshold_ms:
                failed = True

            state.consecutive_failures = state.consecutive_failures + 1 if failed else 0
            row["error_count"] = state.consecutive_failures
            row["fail_count"] = state.consecutive_failures

            row["network_status"] = resolve_network_status(
                row,
                latency_threshold_ms=latency_threshold_ms,
                failures_before_outage=failures_before_outage,
                consecutive_failures=state.consecutive_failures,
            )

            if state.consecutive_failures >= failures_before_outage:
                if state.outage_started_at is None:
                    state.outage_started_at = time.time()
                row["outage_duration_sec"] = int(time.time() - state.outage_started_at)
            else:
                state.outage_started_at = None
                row["outage_duration_sec"] = 0

            row["event"] = detect_event(state, row, failures_before_outage, latency_threshold_ms)
            row["comment"], row["description"], row["severity"] = build_event_metadata(row)

        except Exception as exc:  # noqa: BLE001
            state.consecutive_failures += 1
            row["error_count"] = state.consecutive_failures
            row["fail_count"] = state.consecutive_failures
            row["network_status"] = "Нестабильная сеть"
            if state.outage_started_at is None:
                state.outage_started_at = time.time()
            row["outage_duration_sec"] = int(time.time() - state.outage_started_at)
            row["event"] = "Ошибка мониторинга"
            row["comment"] = "Ошибка в цикле мониторинга."
            row["description"] = f"Ошибка обработки метрик: {exc}"
            row["severity"] = "error"
            row["error"] = f"ошибка цикла мониторинга: {exc}"

        logger.log(row)

        elapsed = time.time() - loop_started
        time.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Остановлено пользователем")
        raise SystemExit(0)
