#!/usr/bin/env python3
"""Radiacode 103 -> Home Assistant bridge with SQLite logging.

Reads CPM and dose rate from a Radiacode 103 via USB, pushes sensor data
to Home Assistant via REST API, and stores all readings in a local SQLite
database for historical reporting.

Polls the device every second, pushes to HA every 5 seconds, and stores
every reading in SQLite.

Note on units: The radiacode library returns dose_rate in R/h (Roentgens/hour).
We convert to uSv/h by multiplying by 10,000 (1 R ≈ 0.01 Sv).
"""

# Patch pyusb to use the bundled libusb DLL on Windows (must be before radiacode import)
import libusb_package
import usb.backend.libusb1

_original_get_backend = usb.backend.libusb1.get_backend


def _patched_get_backend(**kwargs):
    kwargs.setdefault("find_library", libusb_package.find_library)
    return _original_get_backend(**kwargs)


usb.backend.libusb1.get_backend = _patched_get_backend

import logging
import signal
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import usb.core

import requests
import yaml
from radiacode import RadiaCode, RealTimeData
from radiacode.transports.usb import DeviceNotFound, MultipleUSBReadFailure
from radiacode.types import DoseRateDB, RareData

log = logging.getLogger("radiacode_ha")

shutdown_requested = False

DB_PATH = Path(__file__).parent / "radiacode_data.db"

# Conversion factor: radiacode library returns dose_rate in R/h
# 1 R/h = 10,000 uSv/h (using 1 R ≈ 0.01 Sv simplified conversion)
DOSE_RATE_TO_USV = 10_000


def handle_signal(signum, frame):
    global shutdown_requested
    log.info("Shutdown requested (signal %d)", signum)
    shutdown_requested = True


def load_config():
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    token = cfg["homeassistant"]["token"]
    if not token or token == "YOUR_LONG_LIVED_ACCESS_TOKEN":
        print("ERROR: Set your Home Assistant token in config.yaml")
        sys.exit(1)
    return cfg


def setup_logging(cfg):
    level = getattr(logging, cfg.get("logging", {}).get("level", "INFO").upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    log.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)

    log_file = cfg.get("logging", {}).get("file")
    if log_file:
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        fh.setFormatter(fmt)
        log.addHandler(fh)


def init_db():
    """Initialize SQLite database with readings table."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            dose_rate_usv REAL,
            cpm REAL,
            temperature REAL,
            battery REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp)
    """)
    conn.commit()
    conn.close()
    log.info("Database ready: %s", DB_PATH)


def store_reading(conn, dose_rate_usv, cpm, temperature=None, battery=None):
    """Store a reading in the SQLite database."""
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO readings (timestamp, dose_rate_usv, cpm, temperature, battery) VALUES (?, ?, ?, ?, ?)",
        (ts, dose_rate_usv, cpm, temperature, battery),
    )


def push_sensor(session, base_url, token, entity_id, state, attributes):
    """Push a single sensor state to Home Assistant. Returns True on success."""
    url = f"{base_url}/api/states/sensor.{entity_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"state": str(state), "attributes": attributes}
    try:
        resp = session.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.warning("Failed to push %s: %s", entity_id, e)
        return False


def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    cfg = load_config()
    setup_logging(cfg)
    init_db()

    ha_url = cfg["homeassistant"]["url"].rstrip("/")
    ha_token = cfg["homeassistant"]["token"]
    serial = cfg["radiacode"].get("serial_number")
    ha_push_interval = cfg["radiacode"].get("ha_push_interval_seconds", 5)

    session = requests.Session()

    log.info("Radiacode-HA bridge starting (poll every 1s, push to HA every %ds)", ha_push_interval)

    # Track latest rare data values across poll cycles
    last_temperature = None
    last_battery = None

    while not shutdown_requested:
        try:
            log.info("Connecting to Radiacode via USB...")
            # Reset USB device first to recover from any stuck state
            try:
                be = libusb_package.get_libusb1_backend()
                usb_dev = usb.core.find(idVendor=0x0483, idProduct=0xF123, backend=be)
                if usb_dev:
                    usb_dev.reset()
                    time.sleep(1)
            except Exception:
                pass
            device = RadiaCode(serial_number=serial)
            log.info("Connected to Radiacode")

            # Drain stale buffer — device may have hours of backlog
            log.info("Draining device buffer...")
            for i in range(10):
                try:
                    records = device.data_buf()
                    log.debug("Drain pass %d: %d records", i + 1, len(records))
                    if len(records) == 0:
                        break
                except (ValueError, Exception) as e:
                    log.debug("Drain pass %d: error %s", i + 1, e)
                time.sleep(0.3)
            log.info("Buffer drained, starting data collection")

            # Open a persistent DB connection for the inner loop
            db_conn = sqlite3.connect(str(DB_PATH))
            last_ha_push = 0
            readings_since_push = 0

            try:
                while not shutdown_requested:
                    time.sleep(1)
                    if shutdown_requested:
                        break

                    try:
                        records = device.data_buf()
                    except ValueError as e:
                        log.warning("Buffer decode error (skipping): %s", e)
                        continue

                    # Extract latest RareData (battery, temperature)
                    rare_records = [r for r in records if isinstance(r, RareData)]
                    if rare_records:
                        rare = rare_records[-1]
                        last_battery = round(rare.charge_level)
                        last_temperature = round(rare.temperature, 1)

                    # Extract dose rate records (DoseRateDB or RealTimeData)
                    dose_records = [r for r in records if isinstance(r, (DoseRateDB, RealTimeData))]
                    for dr in dose_records:
                        dose_rate_usv = round(dr.dose_rate * DOSE_RATE_TO_USV, 4)
                        cpm = round(dr.count_rate * 60, 1)

                        # Store every reading to SQLite
                        store_reading(db_conn, dose_rate_usv, cpm, last_temperature, last_battery)
                        readings_since_push += 1

                    # Commit batch to SQLite
                    if dose_records:
                        db_conn.commit()

                    # Push to HA at configured interval
                    now = time.monotonic()
                    if dose_records and (now - last_ha_push) >= ha_push_interval:
                        latest = dose_records[-1]
                        dose_rate_usv = round(latest.dose_rate * DOSE_RATE_TO_USV, 4)
                        cpm = round(latest.count_rate * 60, 1)

                        push_sensor(session, ha_url, ha_token, "radiacode_dose_rate", dose_rate_usv, {
                            "unit_of_measurement": "\u00b5Sv/h",
                            "friendly_name": "Radiacode Dose Rate",
                            "state_class": "measurement",
                            "icon": "mdi:radioactive",
                        })
                        push_sensor(session, ha_url, ha_token, "radiacode_cpm", cpm, {
                            "unit_of_measurement": "CPM",
                            "friendly_name": "Radiacode CPM",
                            "state_class": "measurement",
                            "icon": "mdi:counter",
                        })

                        if last_battery is not None:
                            push_sensor(session, ha_url, ha_token, "radiacode_battery", last_battery, {
                                "unit_of_measurement": "%",
                                "friendly_name": "Radiacode Battery",
                                "device_class": "battery",
                                "state_class": "measurement",
                            })
                        if last_temperature is not None:
                            push_sensor(session, ha_url, ha_token, "radiacode_temperature", last_temperature, {
                                "unit_of_measurement": "\u00b0C",
                                "friendly_name": "Radiacode Temperature",
                                "device_class": "temperature",
                                "state_class": "measurement",
                            })

                        log.info(
                            "dose=%.4f \u00b5Sv/h  CPM=%.1f  batt=%s%%  temp=%s\u00b0C  [%d rows stored]",
                            dose_rate_usv, cpm,
                            last_battery if last_battery is not None else "?",
                            last_temperature if last_temperature is not None else "?",
                            readings_since_push,
                        )
                        last_ha_push = now
                        readings_since_push = 0

            finally:
                db_conn.close()

        except DeviceNotFound:
            log.error("Radiacode not found on USB. Is it plugged in? Retrying in 30s...")
        except MultipleUSBReadFailure as e:
            log.error("USB read failure: %s. Reconnecting in 30s...", e)
        except Exception:
            log.exception("Unexpected error. Reconnecting in 30s...")

        if not shutdown_requested:
            for _ in range(300):  # 30 seconds, interruptible
                if shutdown_requested:
                    break
                time.sleep(0.1)

    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
