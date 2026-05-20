#!/usr/bin/env python3
"""Wi-Fi monitoring script for Windows 10/11.

Collects Wi-Fi and connectivity metrics every interval and writes CSV/JSONL logs.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
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
    "is_internet_available",
    "error_count",
    "event",
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
                writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
            self._csv_initialized = True
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] Failed to write CSV log: {exc}", file=sys.stderr)

    def _write_jsonl(self, row: dict[str, Any]) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as file_obj:
                file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] Failed to write JSONL log: {exc}", file=sys.stderr)


def run_command(command: list[str], timeout: int = 5) -> tuple[bool, str, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, shell=False)
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as exc:  # noqa: BLE001
        return False, "", str(exc)


def parse_windows_key_value_block(raw_text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in raw_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip().lower()] = value.strip()
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
        }, f"netsh_error: {err or 'unknown error'}"

    data = parse_windows_key_value_block(out)
    # netsh output may include localized field names; these are english defaults.
    state = data.get("state", "").strip().lower()
    is_connected = state == "connected"

    def get_field(*names: str) -> str:
        for name in names:
            value = data.get(name.lower())
            if value is not None:
                return value
        return ""

    return {
        "is_connected": is_connected,
        "connection_status": "CONNECTED" if is_connected else "DISCONNECTED",
        "ssid": get_field("SSID"),
        "bssid": get_field("BSSID"),
        "signal": get_field("Signal"),
        "radio_type": get_field("Radio type"),
        "channel": get_field("Channel"),
        "rx_rate": get_field("Receive rate (Mbps)", "Receive rate"),
        "tx_rate": get_field("Transmit rate (Mbps)", "Transmit rate"),
    }, None


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
            "latency_ms": "",
            "packet_loss": "100%",
            "error": f"ping_error: {err or 'unknown error'}",
        }

    loss_match = re.search(r"\((\d+)% loss\)", out)
    time_match = re.search(r"time[=<](\d+)ms", out)
    success = "TTL=" in out.upper() and "unreachable" not in out.lower()

    return {
        "target": target,
        "ping_status": "OK" if success else "FAIL",
        "latency_ms": int(time_match.group(1)) if time_match else "",
        "packet_loss": f"{loss_match.group(1)}%" if loss_match else "",
        "error": "" if success else (err.strip() or "ping_failed"),
    }


def choose_ping_result(targets: list[str]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for target in targets:
        result = ping_target(target)
        if result["ping_status"] == "OK":
            return result
        failures.append(result)
    return failures[0] if failures else {"target": "", "ping_status": "FAIL", "latency_ms": "", "packet_loss": "", "error": "no_targets"}


def detect_event(state: MonitorState, row: dict[str, Any], failures_before_outage: int, latency_threshold_ms: int) -> str:
    events: list[str] = []
    connected = bool(row["is_connected"])
    internet = bool(row["is_internet_available"])

    if state.previous_connected is False and connected:
        events.append("WIFI_CONNECTED")
    if state.previous_connected is True and not connected:
        events.append("WIFI_DISCONNECTED")

    if connected and state.previous_ssid and row["ssid"] and state.previous_ssid != row["ssid"]:
        events.append("SSID_CHANGED")
    if connected and state.previous_bssid and row["bssid"] and state.previous_bssid != row["bssid"]:
        events.append("BSSID_CHANGED")

    if row["ping_status"] == "FAIL":
        events.append("PING_FAIL")
    latency = row.get("latency_ms")
    if isinstance(latency, int) and latency > latency_threshold_ms:
        events.append("HIGH_LATENCY")

    if state.previous_internet is False and internet:
        events.append("INTERNET_RESTORED")

    if not state.outage_active and state.consecutive_failures >= failures_before_outage:
        state.outage_active = True
        events.append("OUTAGE_STARTED")
    elif state.outage_active and state.consecutive_failures == 0:
        state.outage_active = False
        events.append("OUTAGE_ENDED")

    state.previous_connected = connected
    state.previous_ssid = str(row["ssid"] or "")
    state.previous_bssid = str(row["bssid"] or "")
    state.previous_internet = internet

    return "|".join(events)


def load_config(path: Path | None) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    if path is None:
        return config
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as file_obj:
        loaded = json.load(file_obj)
    if not isinstance(loaded, dict):
        raise ValueError("Config must be a JSON object")
    config.update(loaded)
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows Wi-Fi monitor script")
    parser.add_argument("--config", type=Path, help="Path to config.json")
    parser.add_argument("--log", type=Path, help="Override log file path")
    parser.add_argument("--interval", type=float, help="Override check interval in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Failed to load config: {exc}", file=sys.stderr)
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
        print("[WARN] Unknown log_format, falling back to csv", file=sys.stderr)
        log_format = "csv"

    targets = list(config.get("ping_targets", []))
    gateway = parse_default_gateway()
    if gateway and gateway not in targets:
        targets.append(gateway)
    if not targets:
        targets = ["8.8.8.8"]

    logger = Logger(log_file, log_format)
    state = MonitorState()

    print(f"Starting Wi-Fi monitor. Interval={interval}s, log={log_file}, format={log_format}")

    while True:
        loop_started = time.time()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row: dict[str, Any] = {
            "timestamp": timestamp,
            "ssid": "",
            "bssid": "",
            "signal": "",
            "radio_type": "",
            "channel": "",
            "rx_rate": "",
            "tx_rate": "",
            "connection_status": "DISCONNECTED",
            "ping_status": "FAIL",
            "latency_ms": "",
            "packet_loss": "",
            "target": "",
            "is_connected": False,
            "is_internet_available": False,
            "error_count": 0,
            "event": "",
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
            row["is_internet_available"] = internet_ok

            failed = (not row["is_connected"]) or (not row["ssid"]) or (not internet_ok)
            latency = row.get("latency_ms")
            if isinstance(latency, int) and latency > latency_threshold_ms:
                failed = True

            state.consecutive_failures = state.consecutive_failures + 1 if failed else 0
            row["error_count"] = state.consecutive_failures

            row["event"] = detect_event(state, row, failures_before_outage, latency_threshold_ms)

        except Exception as exc:  # noqa: BLE001
            state.consecutive_failures += 1
            row["error_count"] = state.consecutive_failures
            row["event"] = "MONITOR_ERROR"
            row["error"] = f"monitor_loop_error: {exc}"

        logger.log(row)

        elapsed = time.time() - loop_started
        time.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped by user")
        raise SystemExit(0)
