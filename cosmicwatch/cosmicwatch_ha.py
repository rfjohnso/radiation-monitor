#!/usr/bin/env python3
"""CosmicWatch Muon Detector -> Home Assistant bridge with SQLite logging.

Reads muon detection events from a CosmicWatch Desktop Muon Detector v2
via serial (USB), pushes sensor data to Home Assistant via REST API,
and stores all events in a local SQLite database.

Serial format from detector (space-delimited):
  count timestamp_ms adc sipm_mV deadtime_ms temperature_C
"""

import logging
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import serial
import serial.tools.list_ports
import yaml

log = logging.getLogger("cosmicwatch_ha")

shutdown_requested = False

DB_PATH = Path(__file__).parent / "cosmicwatch_data.db"


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
    """Initialize SQLite database with events table."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS muon_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_num INTEGER,
            ardn_time_ms INTEGER,
            adc INTEGER,
            sipm_mv REAL,
            deadtime_ms REAL,
            temperature REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS muon_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            cpm REAL,
            total_count INTEGER,
            window_count INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON muon_events(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_timestamp ON muon_stats(timestamp)")
    conn.commit()
    conn.close()
    log.info("Database ready: %s", DB_PATH)


def find_cosmicwatch_port(configured_port=None):
    """Find the CosmicWatch serial port. Prefers configured port, falls back to CH340 detection."""
    if configured_port:
        return configured_port

    for p in serial.tools.list_ports.comports():
        if "CH340" in p.description or "CH341" in p.description:
            log.info("Auto-detected CosmicWatch on %s (%s)", p.device, p.description)
            return p.device

    return None


def push_sensor(session, base_url, token, entity_id, state, attributes):
    """Push a single sensor state to Home Assistant."""
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


def parse_event(line):
    """Parse a CosmicWatch event line. Returns dict or None."""
    parts = line.strip().split()
    if len(parts) != 6:
        return None
    try:
        return {
            "event_num": int(parts[0]),
            "ardn_time_ms": int(parts[1]),
            "adc": int(parts[2]),
            "sipm_mv": float(parts[3]),
            "deadtime_ms": float(parts[4]),
            "temperature": float(parts[5]),
        }
    except (ValueError, IndexError):
        return None


def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    cfg = load_config()
    setup_logging(cfg)
    init_db()

    ha_url = cfg["homeassistant"]["url"].rstrip("/")
    ha_token = cfg["homeassistant"]["token"]
    cw_cfg = cfg.get("cosmicwatch", {})
    port = cw_cfg.get("port")
    baud = cw_cfg.get("baud", 9600)
    ha_push_interval = cw_cfg.get("ha_push_interval_seconds", 10)

    http_session = requests.Session()

    # Load cumulative total from DB (persists across restarts)
    try:
        db_conn = sqlite3.connect(str(DB_PATH))
        row = db_conn.execute("SELECT COUNT(*) FROM muon_events").fetchone()
        cumulative_total = row[0] if row else 0
        db_conn.close()
    except Exception:
        cumulative_total = 0
    log.info("CosmicWatch-HA bridge starting (push to HA every %ds, %d events in DB)", ha_push_interval, cumulative_total)

    while not shutdown_requested:
        ser = None
        try:
            com_port = find_cosmicwatch_port(port)
            if not com_port:
                log.error("CosmicWatch not found. Is it plugged in? Retrying in 30s...")
                for _ in range(300):
                    if shutdown_requested:
                        return
                    time.sleep(0.1)
                continue

            log.info("Connecting to CosmicWatch on %s at %d baud...", com_port, baud)
            ser = serial.Serial(com_port, baud, timeout=10)
            time.sleep(2)  # Wait for Arduino reset

            # Skip header lines
            for _ in range(10):
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if line:
                    log.debug("Header: %s", line)
                if line and not line.startswith("#") and not line.startswith("Device") and not line.startswith("SD"):
                    # This might be an event already
                    break

            log.info("Connected, listening for muon events...")

            db_conn = sqlite3.connect(str(DB_PATH))
            last_ha_push = 0
            events_in_window = 0
            window_start = time.monotonic()

            try:
                while not shutdown_requested:
                    line = ser.readline().decode("utf-8", errors="replace").strip()

                    if not line:
                        # Timeout - no event. Still push stats periodically
                        now = time.monotonic()
                        if (now - last_ha_push) >= ha_push_interval:
                            window_secs = now - window_start
                            cpm = (events_in_window / window_secs * 60) if window_secs > 0 else 0

                            push_sensor(http_session, ha_url, ha_token, "cosmicwatch_cpm", round(cpm, 2), {
                                "unit_of_measurement": "CPM",
                                "friendly_name": "CosmicWatch Muon Rate",
                                "state_class": "measurement",
                                "icon": "mdi:atom",
                            })
                            push_sensor(http_session, ha_url, ha_token, "cosmicwatch_total", cumulative_total, {
                                "unit_of_measurement": "events",
                                "friendly_name": "CosmicWatch Total Muons",
                                "state_class": "total_increasing",
                                "icon": "mdi:counter",
                            })
                            last_ha_push = now
                        continue

                    # Skip any non-data lines
                    if line.startswith("#") or line.startswith("Device") or line.startswith("SD"):
                        continue

                    event = parse_event(line)
                    if not event:
                        log.debug("Unparseable line: %s", line)
                        continue

                    cumulative_total += 1
                    events_in_window += 1

                    # Store event to SQLite
                    ts = datetime.now(timezone.utc).isoformat()
                    db_conn.execute(
                        "INSERT INTO muon_events (timestamp, event_num, ardn_time_ms, adc, sipm_mv, deadtime_ms, temperature) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (ts, event["event_num"], event["ardn_time_ms"], event["adc"],
                         event["sipm_mv"], event["deadtime_ms"], event["temperature"]),
                    )
                    db_conn.commit()

                    # Push to HA periodically
                    now = time.monotonic()
                    if (now - last_ha_push) >= ha_push_interval:
                        window_secs = now - window_start
                        cpm = (events_in_window / window_secs * 60) if window_secs > 0 else 0

                        push_sensor(http_session, ha_url, ha_token, "cosmicwatch_cpm", round(cpm, 2), {
                            "unit_of_measurement": "CPM",
                            "friendly_name": "CosmicWatch Muon Rate",
                            "state_class": "measurement",
                            "icon": "mdi:atom",
                        })
                        push_sensor(http_session, ha_url, ha_token, "cosmicwatch_total", cumulative_total, {
                            "unit_of_measurement": "events",
                            "friendly_name": "CosmicWatch Total Muons",
                            "state_class": "total_increasing",
                            "icon": "mdi:counter",
                        })
                        push_sensor(http_session, ha_url, ha_token, "cosmicwatch_sipm", round(event["sipm_mv"], 2), {
                            "unit_of_measurement": "mV",
                            "friendly_name": "CosmicWatch Last SiPM Voltage",
                            "state_class": "measurement",
                            "icon": "mdi:flash",
                        })
                        push_sensor(http_session, ha_url, ha_token, "cosmicwatch_adc", event["adc"], {
                            "unit_of_measurement": "ADC",
                            "friendly_name": "CosmicWatch Last ADC",
                            "state_class": "measurement",
                            "icon": "mdi:sine-wave",
                        })

                        # Store CPM stat
                        db_conn.execute(
                            "INSERT INTO muon_stats (timestamp, cpm, total_count, window_count) VALUES (?, ?, ?, ?)",
                            (ts, round(cpm, 2), cumulative_total, events_in_window),
                        )
                        db_conn.commit()

                        log.info(
                            "muon #%d  CPM=%.1f  ADC=%d  SiPM=%.1f mV  [%d events in %.0fs]",
                            cumulative_total, cpm, event["adc"], event["sipm_mv"],
                            events_in_window, window_secs,
                        )
                        last_ha_push = now

            finally:
                db_conn.close()

        except serial.SerialException as e:
            log.error("Serial error: %s. Reconnecting in 10s...", e)
        except Exception:
            log.exception("Unexpected error. Reconnecting in 10s...")
        finally:
            if ser and ser.is_open:
                ser.close()

        if not shutdown_requested:
            for _ in range(100):
                if shutdown_requested:
                    break
                time.sleep(0.1)

    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
