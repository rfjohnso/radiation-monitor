#!/usr/bin/env python3
"""Simple web dashboard for Radiacode historical data.

Serves a web UI showing charts and tables of radiation readings
stored in the SQLite database by radiacode_ha.py.

Run: python dashboard.py
Then open http://localhost:5000 in your browser.
"""

import sqlite3
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

DB_PATH = Path(__file__).parent / "radiacode_data.db"


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/readings")
def api_readings():
    """Return readings as JSON. Query params: hours (default 24), limit (default 2000)."""
    hours = request.args.get("hours", 24, type=int)
    limit = request.args.get("limit", 2000, type=int)
    # Clamp values
    hours = max(1, min(hours, 8760))  # 1 hour to 1 year
    limit = max(10, min(limit, 50000))

    conn = get_db()
    rows = conn.execute(
        """
        SELECT timestamp, dose_rate_usv as dose_rate, cpm, temperature, battery
        FROM readings
        WHERE timestamp >= datetime('now', ? || ' hours')
        ORDER BY timestamp ASC
        LIMIT ?
        """,
        (f"-{hours}", limit),
    ).fetchall()
    conn.close()

    return jsonify([dict(r) for r in rows])


@app.route("/api/stats")
def api_stats():
    """Return summary statistics. Query params: hours (default 24)."""
    hours = request.args.get("hours", 24, type=int)
    hours = max(1, min(hours, 8760))

    conn = get_db()
    row = conn.execute(
        """
        SELECT
            COUNT(*) as count,
            MIN(dose_rate_usv) as min_dose_rate,
            MAX(dose_rate_usv) as max_dose_rate,
            AVG(dose_rate_usv) as avg_dose_rate,
            MIN(cpm) as min_cpm,
            MAX(cpm) as max_cpm,
            AVG(cpm) as avg_cpm,
            MIN(timestamp) as first_reading,
            MAX(timestamp) as last_reading
        FROM readings
        WHERE timestamp >= datetime('now', ? || ' hours')
        """,
        (f"-{hours}",),
    ).fetchone()
    conn.close()

    return jsonify(dict(row))


@app.route("/api/export")
def api_export():
    """Export readings as CSV. Query params: hours (default 24)."""
    hours = request.args.get("hours", 24, type=int)
    hours = max(1, min(hours, 8760))

    conn = get_db()
    rows = conn.execute(
        """
        SELECT timestamp, dose_rate_usv, cpm, temperature, battery
        FROM readings
        WHERE timestamp >= datetime('now', ? || ' hours')
        ORDER BY timestamp ASC
        """,
        (f"-{hours}",),
    ).fetchall()
    conn.close()

    lines = ["timestamp,dose_rate_uSv_h,cpm,temperature_C,battery_pct"]
    for r in rows:
        lines.append(f"{r['timestamp']},{r['dose_rate_usv']},{r['cpm']},{r['temperature']},{r['battery']}")

    from flask import Response

    return Response(
        "\n".join(lines),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=radiacode_export.csv"},
    )


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Radiacode Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a2e; color: #e0e0e0; padding: 20px; }
  h1 { color: #00d4aa; margin-bottom: 20px; font-size: 1.5rem; }
  .controls { display: flex; gap: 10px; margin-bottom: 20px; align-items: center; flex-wrap: wrap; }
  .controls button { background: #16213e; border: 1px solid #0f3460; color: #e0e0e0; padding: 8px 16px; cursor: pointer; border-radius: 4px; }
  .controls button:hover { background: #0f3460; }
  .controls button.active { background: #00d4aa; color: #1a1a2e; font-weight: bold; }
  .controls a { color: #00d4aa; text-decoration: none; margin-left: auto; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
  .stat-card { background: #16213e; border-radius: 8px; padding: 15px; border: 1px solid #0f3460; }
  .stat-card .label { font-size: 0.85rem; color: #888; margin-bottom: 4px; }
  .stat-card .value { font-size: 1.4rem; font-weight: bold; color: #00d4aa; }
  .stat-card .range { font-size: 0.8rem; color: #666; margin-top: 4px; }
  .charts { display: grid; grid-template-columns: 1fr; gap: 20px; }
  .chart-container { background: #16213e; border-radius: 8px; padding: 15px; border: 1px solid #0f3460; }
  .chart-container h2 { font-size: 1rem; color: #ccc; margin-bottom: 10px; }
  canvas { width: 100% !important; }
  .status { font-size: 0.8rem; color: #666; margin-top: 15px; text-align: center; }
</style>
</head>
<body>
<h1>&#9762; Radiacode Monitor</h1>
<div class="controls">
  <button onclick="setHours(1)">1H</button>
  <button onclick="setHours(6)">6H</button>
  <button onclick="setHours(24)" class="active">24H</button>
  <button onclick="setHours(168)">7D</button>
  <button onclick="setHours(720)">30D</button>
  <a href="#" onclick="exportCSV(); return false;">Export CSV</a>
</div>
<div class="stats">
  <div class="stat-card">
    <div class="label">Current Dose Rate</div>
    <div class="value" id="current-dose">--</div>
    <div class="range" id="dose-range"></div>
  </div>
  <div class="stat-card">
    <div class="label">Current CPM</div>
    <div class="value" id="current-cpm">--</div>
    <div class="range" id="cpm-range"></div>
  </div>
  <div class="stat-card">
    <div class="label">Readings</div>
    <div class="value" id="reading-count">--</div>
    <div class="range" id="time-range"></div>
  </div>
  <div class="stat-card">
    <div class="label">Battery / Temp</div>
    <div class="value" id="battery-temp">--</div>
  </div>
</div>
<div class="charts">
  <div class="chart-container">
    <h2>Dose Rate (&micro;Sv/h)</h2>
    <canvas id="doseChart" height="200"></canvas>
  </div>
  <div class="chart-container">
    <h2>Counts Per Minute (CPM)</h2>
    <canvas id="cpmChart" height="200"></canvas>
  </div>
</div>
<div class="status" id="status">Loading...</div>

<script>
let currentHours = 24;
let doseChart, cpmChart;

function createChart(ctx, label, color) {
  return new Chart(ctx, {
    type: 'line',
    data: { datasets: [{ label, data: [], borderColor: color, backgroundColor: color + '20', borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.2 }] },
    options: {
      responsive: true,
      scales: {
        x: { type: 'time', time: { tooltipFormat: 'yyyy-MM-dd HH:mm:ss' }, ticks: { color: '#666' }, grid: { color: '#222' } },
        y: { beginAtZero: true, ticks: { color: '#666' }, grid: { color: '#222' } }
      },
      plugins: { legend: { display: false } },
      interaction: { intersect: false, mode: 'index' }
    }
  });
}

function init() {
  doseChart = createChart(document.getElementById('doseChart').getContext('2d'), 'Dose Rate', '#ff6b6b');
  cpmChart = createChart(document.getElementById('cpmChart').getContext('2d'), 'CPM', '#4ecdc4');
  refresh();
  setInterval(refresh, 30000);
}

async function refresh() {
  try {
    const [readings, stats] = await Promise.all([
      fetch('/api/readings?hours=' + currentHours).then(r => r.json()),
      fetch('/api/stats?hours=' + currentHours).then(r => r.json())
    ]);

    doseChart.data.datasets[0].data = readings.map(r => ({ x: r.timestamp, y: r.dose_rate }));
    cpmChart.data.datasets[0].data = readings.map(r => ({ x: r.timestamp, y: r.cpm }));
    doseChart.update('none');
    cpmChart.update('none');

    if (readings.length > 0) {
      const last = readings[readings.length - 1];
      document.getElementById('current-dose').textContent = (last.dose_rate || 0).toFixed(4) + ' \\u00b5Sv/h';
      document.getElementById('current-cpm').textContent = (last.cpm || 0).toFixed(1);
      document.getElementById('battery-temp').textContent =
        (last.battery != null ? last.battery + '%' : '--') + ' / ' +
        (last.temperature != null ? last.temperature + '\\u00b0C' : '--');
    }

    document.getElementById('reading-count').textContent = stats.count || 0;
    if (stats.min_dose_rate != null) {
      document.getElementById('dose-range').textContent = 'Min: ' + stats.min_dose_rate.toFixed(4) + ' / Max: ' + stats.max_dose_rate.toFixed(4);
      document.getElementById('cpm-range').textContent = 'Min: ' + stats.min_cpm.toFixed(1) + ' / Max: ' + stats.max_cpm.toFixed(1);
    }
    if (stats.first_reading) {
      document.getElementById('time-range').textContent = stats.first_reading.slice(0, 19) + ' to ' + stats.last_reading.slice(0, 19);
    }

    document.getElementById('status').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('status').textContent = 'Error loading data: ' + e.message;
  }
}

function setHours(h) {
  currentHours = h;
  document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  refresh();
}

function exportCSV() {
  window.location.href = '/api/export?hours=' + currentHours;
}

init();
</script>
</body>
</html>"""


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        print("Run radiacode_ha.py first to start collecting data.")
        sys.exit(1)
    print(f"Dashboard: http://localhost:5000")
    print(f"Database: {DB_PATH}")
    app.run(host="127.0.0.1", port=5000, debug=False)
