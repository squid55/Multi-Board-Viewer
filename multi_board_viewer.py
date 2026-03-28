"""
Multi Board Camera Streaming Viewer
PC에서 여러 보드의 카메라 스트림을 동시에 모니터링 + 대시보드

Usage:
    python3 multi_board_viewer.py --web
"""

import cv2
import threading
import numpy as np
import time
import json
import argparse
import subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ──── 보드 설정 ────
BOARDS = {
    "Jetson Orin Nano (YOLOv8)": {
        "stream": "http://192.168.219.108:8080/stream",
        "ssh": "jetson-nx@192.168.219.108",
    },
    "Raspberry Pi 3B": {
        "stream": "http://192.168.219.109:8080/stream",
        "ssh": "rbpi3b@192.168.219.109",
    },
    # "Jetson Nano": {
    #     "stream": "http://192.168.219.xxx:8080/stream",
    #     "ssh": "user@192.168.219.xxx",
    # },
}

CELL_W, CELL_H = 640, 360
frames = {}
lock = threading.Lock()
board_status = {}
status_lock = threading.Lock()

COLORS = [
    (0, 255, 0),
    (0, 200, 255),
    (255, 100, 100),
    (0, 255, 255),
    (255, 0, 255),
]

# ──── SSH 상태 수집 ────
def collect_status(name, ssh_target):
    """SSH로 보드 상태를 주기적으로 수집."""
    while True:
        try:
            cmd = (
                f"ssh -o ConnectTimeout=3 -o StrictHostKeyChecking=no {ssh_target} "
                f"\"cat /proc/loadavg; echo '|||'; free -b; echo '|||'; "
                f"cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null || echo 0; echo '|||'; "
                f"uptime -p 2>/dev/null || uptime; echo '|||'; "
                f"nproc\""
            )
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=8)
            if result.returncode == 0:
                parts = result.stdout.split("|||")
                # CPU load
                load_parts = parts[0].strip().split()
                load_1m = float(load_parts[0])

                # Memory
                mem_lines = parts[1].strip().split("\n")
                mem_parts = mem_lines[1].split()
                mem_total = int(mem_parts[1])
                mem_used = int(mem_parts[2])

                # Temperature
                temp_raw = parts[2].strip().split("\n")[0]
                temp_c = int(temp_raw) / 1000.0 if int(temp_raw) > 1000 else float(temp_raw)

                # Uptime
                uptime_str = parts[3].strip()

                # CPU cores
                nproc = int(parts[4].strip())
                cpu_pct = (load_1m / nproc) * 100

                with status_lock:
                    board_status[name] = {
                        "online": True,
                        "cpu_pct": round(min(cpu_pct, 100), 1),
                        "mem_total_gb": round(mem_total / 1e9, 1),
                        "mem_used_gb": round(mem_used / 1e9, 1),
                        "mem_pct": round(mem_used / mem_total * 100, 1),
                        "temp_c": round(temp_c, 1),
                        "uptime": uptime_str,
                        "updated": time.strftime("%H:%M:%S"),
                    }
            else:
                with status_lock:
                    board_status[name] = {"online": False, "updated": time.strftime("%H:%M:%S")}
        except Exception:
            with status_lock:
                board_status[name] = {"online": False, "updated": time.strftime("%H:%M:%S")}
        time.sleep(5)


# ──── 스트림 캡처 ────
def capture_stream(name, url, color):
    blank = np.zeros((CELL_H, CELL_W, 3), dtype=np.uint8)
    cv2.putText(blank, f"{name}", (10, CELL_H // 2 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100, 100, 100), 2)
    cv2.putText(blank, "Connecting...", (10, CELL_H // 2 + 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 100, 100), 1)
    with lock:
        frames[name] = blank.copy()

    while True:
        try:
            cap = cv2.VideoCapture(url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            fps_time = time.monotonic()
            fps_count = 0
            current_fps = 0.0

            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                frame = cv2.resize(frame, (CELL_W, CELL_H))
                fps_count += 1
                elapsed = time.monotonic() - fps_time
                if elapsed >= 1.0:
                    current_fps = fps_count / elapsed
                    fps_count = 0
                    fps_time = time.monotonic()

                cv2.rectangle(frame, (0, 0), (CELL_W, 32), (0, 0, 0), -1)
                cv2.putText(frame, f"{name}", (8, 22),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                cv2.putText(frame, f"{current_fps:.0f}fps", (CELL_W - 70, 22),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
                cv2.rectangle(frame, (0, 0), (CELL_W - 1, CELL_H - 1), color, 2)

                with lock:
                    frames[name] = frame
            cap.release()
        except Exception:
            pass

        with lock:
            dc = blank.copy()
            cv2.putText(dc, "Disconnected", (10, CELL_H // 2 + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 1)
            frames[name] = dc
        time.sleep(3)


def build_grid(names):
    n = len(names)
    cols = 2 if n >= 2 else 1
    rows = (n + cols - 1) // cols

    with lock:
        cells = [frames.get(name, np.zeros((CELL_H, CELL_W, 3), dtype=np.uint8)) for name in names]
    while len(cells) < rows * cols:
        cells.append(np.zeros((CELL_H, CELL_W, 3), dtype=np.uint8))

    row_imgs = []
    for r in range(rows):
        row_imgs.append(np.hstack(cells[r * cols:(r + 1) * cols]))
    return np.vstack(row_imgs)


# ──── 웹 서버 (ThreadingHTTPServer = 멀티스레드) ────
latest_grid_jpg = None
grid_lock = threading.Lock()

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Multi Board Viewer</title>
<style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { background:#0a0a0a; color:#eee; font-family:'Segoe UI',sans-serif; }
    .header {
        display:flex; justify-content:space-between; align-items:center;
        padding:8px 16px; background:#1a1a1a; border-bottom:1px solid #333;
    }
    .header h2 { font-size:1.1em; }
    #dashBtn {
        background:#2563eb; color:white; border:none; padding:8px 16px;
        border-radius:6px; cursor:pointer; font-size:0.9em;
    }
    #dashBtn:hover { background:#1d4ed8; }
    #dashBtn.active { background:#dc2626; }
    .stream-container { text-align:center; padding:4px; }
    .stream-container img { max-width:100%; }
    #dashboard {
        display:none; padding:12px 16px;
        background:#111; border-top:1px solid #333;
    }
    .dash-grid {
        display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr));
        gap:12px;
    }
    .board-card {
        background:#1a1a1a; border:1px solid #333; border-radius:8px;
        padding:14px; position:relative;
    }
    .board-card h3 { font-size:0.95em; margin-bottom:10px; }
    .online-dot {
        display:inline-block; width:8px; height:8px; border-radius:50%;
        margin-right:6px; vertical-align:middle;
    }
    .online-dot.on { background:#22c55e; }
    .online-dot.off { background:#ef4444; }
    .stat-row {
        display:flex; justify-content:space-between; align-items:center;
        padding:4px 0; border-bottom:1px solid #222; font-size:0.85em;
    }
    .stat-row:last-child { border-bottom:none; }
    .stat-label { color:#999; }
    .stat-value { font-weight:600; }
    .bar-row { padding:2px 0 6px 0; }
    .bar-bg-full {
        width:100%; height:12px; background:#333; border-radius:6px;
        overflow:hidden;
    }
    .bar-fill {
        height:100%; border-radius:6px; transition:width 0.5s;
    }
    .bar-cpu { background:linear-gradient(90deg, #3b82f6, #60a5fa); }
    .bar-mem { background:linear-gradient(90deg, #f59e0b, #fbbf24); }
    .temp-normal { color:#22c55e; }
    .temp-warm { color:#f59e0b; }
    .temp-hot { color:#ef4444; }
    .updated { color:#555; font-size:0.75em; margin-top:8px; text-align:right; }
</style>
</head>
<body>
<div class="header">
    <h2>Multi Board Viewer (BOARD_COUNT boards)</h2>
    <button id="dashBtn" onclick="toggleDash()">Dashboard</button>
</div>
<div class="stream-container">
    <img src="/stream">
</div>
<div id="dashboard">
    <div class="dash-grid" id="dashGrid"></div>
</div>

<script>
let dashOpen = false;
let refreshTimer = null;

function toggleDash() {
    dashOpen = !dashOpen;
    const dash = document.getElementById('dashboard');
    const btn = document.getElementById('dashBtn');
    if (dashOpen) {
        dash.style.display = 'block';
        btn.textContent = 'Close Dashboard';
        btn.classList.add('active');
        fetchStatus();
        refreshTimer = setInterval(fetchStatus, 5000);
    } else {
        dash.style.display = 'none';
        btn.textContent = 'Dashboard';
        btn.classList.remove('active');
        if (refreshTimer) clearInterval(refreshTimer);
    }
}

function tempClass(t) {
    if (t < 50) return 'temp-normal';
    if (t < 70) return 'temp-warm';
    return 'temp-hot';
}

function fetchStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            const grid = document.getElementById('dashGrid');
            grid.innerHTML = '';
            for (const [name, s] of Object.entries(data)) {
                const online = s.online;
                const dot = `<span class="online-dot ${online?'on':'off'}"></span>`;
                let body = '';
                if (online) {
                    body = `
                        <div class="stat-row">
                            <span class="stat-label">CPU</span>
                            <span class="stat-value">${s.cpu_pct}%</span>
                        </div>
                        <div class="bar-row"><div class="bar-bg-full"><div class="bar-fill bar-cpu" style="width:${s.cpu_pct}%"></div></div></div>
                        <div class="stat-row">
                            <span class="stat-label">Memory</span>
                            <span class="stat-value">${s.mem_used_gb}/${s.mem_total_gb}GB (${s.mem_pct}%)</span>
                        </div>
                        <div class="bar-row"><div class="bar-bg-full"><div class="bar-fill bar-mem" style="width:${s.mem_pct}%"></div></div></div>
                        <div class="stat-row">
                            <span class="stat-label">Temperature</span>
                            <span class="stat-value ${tempClass(s.temp_c)}">${s.temp_c}&deg;C</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Uptime</span>
                            <span class="stat-value">${s.uptime}</span>
                        </div>
                        <div class="updated">Updated: ${s.updated}</div>
                    `;
                } else {
                    body = `<div style="color:#666;padding:20px;text-align:center">Offline</div>
                            <div class="updated">Checked: ${s.updated}</div>`;
                }
                const card = `<div class="board-card"><h3>${dot}${name}</h3>${body}</div>`;
                grid.innerHTML += card;
            }
        })
        .catch(() => {});
}
</script>
</body>
</html>"""


class ViewerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            html = HTML_PAGE.replace("BOARD_COUNT", str(len(BOARDS)))
            self.wfile.write(html.encode())
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with grid_lock:
                        jpg = latest_grid_jpg
                    if jpg:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(jpg)
                        self.wfile.write(b"\r\n")
                    time.sleep(0.05)
            except BrokenPipeError:
                pass
        elif self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with status_lock:
                data = dict(board_status)
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def grid_update_loop(names):
    global latest_grid_jpg
    while True:
        grid = build_grid(names)
        _, jpg = cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 75])
        with grid_lock:
            latest_grid_jpg = jpg.tobytes()
        time.sleep(0.033)


def main():
    parser = argparse.ArgumentParser(description="Multi Board Camera Viewer")
    parser.add_argument("--web", action="store_true", help="웹 브라우저 모드 (port 9090)")
    args = parser.parse_args()

    names = list(BOARDS.keys())
    print(f"Starting Multi Board Viewer ({len(names)} boards)")

    # 캡처 스레드
    for i, (name, cfg) in enumerate(BOARDS.items()):
        color = COLORS[i % len(COLORS)]
        t = threading.Thread(target=capture_stream, args=(name, cfg["stream"], color), daemon=True)
        t.start()
        print(f"  [{i+1}] {name} <- {cfg['stream']}")

    # 상태 수집 스레드
    for name, cfg in BOARDS.items():
        t = threading.Thread(target=collect_status, args=(name, cfg["ssh"]), daemon=True)
        t.start()

    if args.web:
        t = threading.Thread(target=grid_update_loop, args=(names,), daemon=True)
        t.start()
        print(f"\nWeb viewer at http://localhost:9090")
        print("Dashboard: click 'Dashboard' button on the page")
        server = ThreadingHTTPServer(("0.0.0.0", 9090), ViewerHandler)
        server.serve_forever()
    else:
        print("\nPress ESC to quit")
        while True:
            grid = build_grid(names)
            cv2.imshow("Multi Board Viewer", grid)
            if cv2.waitKey(1) == 27:
                break
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
