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
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ──── 보드 설정 ────
# lan: 같은 네트워크에서 접속 (빠름)
# tailscale: 외부에서 접속 (어디서든)
BOARDS = {
    "Jetson Orin Nano": {
        "stream_lan": "http://100.77.67.60:8080/stream",
        "stream_tailscale": "http://100.77.67.60:8080/stream",
        "ssh_lan": "jetson-nx@100.77.67.60",
        "ssh_tailscale": "jetson-nx@100.77.67.60",
        "modes": {
            "yolo": {"service": "yolo-stream", "port": 8080, "label": "YOLOv8 Detection"},
            "fall": {"service": "fall-detection", "port": 8081, "label": "Fall Detection"},
        },
        "current_mode": "yolo",
        "audio": {
            "tx_port": 5004,        # 보드 mic → PC: PC가 이 포트로 받음
            "rx_port": 5010,        # PC mic → 보드: 모든 엣지 공통 (옵시디언 노트 계획)
            "alsa_in": "plughw:0,0",
            "alsa_out": "plughw:0,0",
            "label": "USB Headset",
        },
    },
    "Raspberry Pi 3B": {
        "stream_lan": "http://192.168.0.13:8080/stream",
        "stream_tailscale": "http://100.123.127.114:8080/stream",
        "ssh_lan": "rbpi3b@192.168.0.13",
        "ssh_tailscale": "rbpi3b@100.123.127.114",
    },
    "Jetson Nano": {
        "stream_lan": "http://192.168.0.11:8080/stream",
        "stream_tailscale": "http://100.125.186.100:8080/stream",
        "ssh_lan": "hhj@192.168.0.11",
        "ssh_tailscale": "hhj@100.125.186.100",
    },
    "Zybo Z7-20": {
        "stream_lan": "http://192.168.0.14:8080/stream",
        "stream_tailscale": "http://192.168.0.14:8080/stream",
        "ssh_lan": "root@192.168.0.14",
        "ssh_tailscale": "root@192.168.0.14",
    },
}

def is_lan():
    """LAN에 있는지 자동 감지 (게이트웨이 ping)"""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", "192.168.0.1"],
            capture_output=True, timeout=3
        )
        return result.returncode == 0
    except Exception:
        return False

USE_LAN = is_lan()


def get_pc_ip_for_board():
    """Orin이 PC로 RTP 보낼 때 쓸 IP. LAN이면 LAN IP, 아니면 Tailscale IP."""
    try:
        out = subprocess.check_output(["tailscale", "ip", "-4"], timeout=2).decode().strip()
        ts_ip = out.splitlines()[0] if out else ""
    except Exception:
        ts_ip = "100.125.10.87"  # 노트의 PC Tailscale IP
    if USE_LAN:
        try:
            out = subprocess.check_output(
                ["ip", "-4", "-o", "addr", "show"], timeout=2
            ).decode()
            for line in out.splitlines():
                if "192.168.0." in line:
                    return line.split()[3].split("/")[0]
        except Exception:
            pass
    return ts_ip


PC_IP_FOR_BOARDS = get_pc_ip_for_board()

# 네트워크에 맞게 stream/ssh 필드 자동 설정
for name, cfg in BOARDS.items():
    suffix = "lan" if USE_LAN else "tailscale"
    cfg["stream"] = cfg.get(f"stream_{suffix}", "")
    cfg["ssh"] = cfg.get(f"ssh_{suffix}", "")

# 현재 모드에 맞게 스트림 URL 설정
def get_stream_url(board_name):
    cfg = BOARDS[board_name]
    if "modes" in cfg:
        mode = cfg["current_mode"]
        port = cfg["modes"][mode]["port"]
        host = cfg["ssh"].split("@")[1]
        return f"http://{host}:{port}/stream"
    return cfg["stream"]

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


# ──── 스트림 캡처 (HTTP MJPEG 직접 파싱) ────
def capture_stream(name, initial_url, color):
    print(f"[CAP {name}] thread started, initial_url={initial_url}", flush=True)
    blank = np.zeros((CELL_H, CELL_W, 3), dtype=np.uint8)
    cv2.putText(blank, f"{name}", (10, CELL_H // 2 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100, 100, 100), 2)
    cv2.putText(blank, "Connecting...", (10, CELL_H // 2 + 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 100, 100), 1)
    with lock:
        frames[name] = blank.copy()

    while True:
        try:
            url = get_stream_url(name) if name in BOARDS else initial_url
            print(f"[CAP {name}] connecting to {url}", flush=True)
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=5)
            print(f"[CAP {name}] connected, status={resp.status}", flush=True)
            buf = b""
            fps_time = time.monotonic()
            fps_count = 0
            current_fps = 0.0

            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                # JPEG SOI/EOI 마커로 프레임 추출
                while True:
                    soi = buf.find(b"\xff\xd8")
                    eoi = buf.find(b"\xff\xd9", soi + 2) if soi >= 0 else -1
                    if soi >= 0 and eoi >= 0:
                        jpg = buf[soi:eoi + 2]
                        buf = buf[eoi + 2:]
                        arr = np.frombuffer(jpg, dtype=np.uint8)
                        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if frame is not None:
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
                    else:
                        break
            resp.close()
            print(f"[CAP {name}] resp closed cleanly", flush=True)
        except Exception as e:
            import traceback
            print(f"[CAP {name}] EXCEPTION: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()

        with lock:
            dc = blank.copy()
            cv2.putText(dc, "Disconnected", (10, CELL_H // 2 + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 1)
            frames[name] = dc
        time.sleep(3)


# ──── 모드 전환 ────
capture_threads = {}

def switch_board_mode(board_name, mode):
    """SSH로 Jetson의 서비스를 전환하고 캡처 스레드를 재시작."""
    cfg = BOARDS.get(board_name)
    if not cfg or "modes" not in cfg:
        return {"ok": False, "error": "Board not found or no modes"}
    if mode not in cfg["modes"]:
        return {"ok": False, "error": f"Unknown mode: {mode}"}
    if mode == cfg["current_mode"]:
        return {"ok": True, "message": "Already in this mode"}

    ssh_target = cfg["ssh"]
    old_mode = cfg["current_mode"]
    old_service = cfg["modes"][old_mode]["service"]
    new_service = cfg["modes"][mode]["service"]

    try:
        # 이전 서비스 중지 + 새 서비스 시작
        cmd = (
            f"ssh -o ConnectTimeout=5 {ssh_target} "
            f"\"echo hong1003 | sudo -S bash -c '"
            f"systemctl stop {old_service}; "
            f"sleep 2; "
            f"systemctl start {new_service}"
            f"'\""
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)

        # 현재 모드 업데이트
        cfg["current_mode"] = mode
        new_port = cfg["modes"][mode]["port"]
        host = ssh_target.split("@")[1]
        cfg["stream"] = f"http://{host}:{new_port}/stream"

        # 캡처 스레드가 자동으로 새 URL에 재연결됨 (disconnect → reconnect)
        return {"ok": True, "message": f"Switched to {mode}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ──── 오디오 ────
# 모델:
#   엣지 (헤드리스): TX(자기말 송출) + RX(PC말 받기) 모두 뷰어 시작 시 SSH로 always-on
#   PC: 보드별 Listen 토글 (디코드+재생) + 글로벌 Mic 토글 (모든 엣지로 동시 송출)
audio_lock = threading.Lock()
edge_audio = {}      # name -> {"tx_ssh": Popen, "rx_ssh": Popen, "started": str}
pc_listen = {}       # name -> {"proc": Popen, "since": str}
pc_mic_state = {"proc": None, "since": None}
audio_supervisor_running = False


def _start_edge_tx_rx(board_name):
    """엣지에 TX/RX 파이프를 SSH로 띄움. always-on."""
    cfg = BOARDS.get(board_name) or {}
    a = cfg.get("audio") or {}
    if not a:
        return
    ssh_target = cfg["ssh"]
    alsa_in = a.get("alsa_in", "plughw:0,0")
    alsa_out = a.get("alsa_out", "plughw:0,0")
    tx_port = a["tx_port"]
    rx_port = a["rx_port"]

    # 엣지 TX: 자기 mic → PC tx_port
    tx_remote = (
        f"gst-launch-1.0 -q alsasrc device={alsa_in} ! "
        f"audioconvert ! audioresample ! "
        f"opusenc bitrate=32000 inband-fec=true ! "
        f"rtpopuspay pt=96 ! "
        f"udpsink host={PC_IP_FOR_BOARDS} port={tx_port}"
    )
    # 엣지 RX: PC mic 받기 → 자기 spk
    rx_remote = (
        f"gst-launch-1.0 -q udpsrc port={rx_port} "
        f"caps='application/x-rtp,media=audio,encoding-name=OPUS,payload=96,clock-rate=48000' ! "
        f"rtpjitterbuffer latency=60 ! rtpopusdepay ! opusdec plc=true ! "
        f"audioconvert ! audioresample ! "
        f"alsasink device={alsa_out} sync=false"
    )

    def ssh_popen(remote):
        return subprocess.Popen(
            [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=20",
                "-o", "ServerAliveCountMax=2",
                ssh_target, remote,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    tx = ssh_popen(tx_remote)
    rx = ssh_popen(rx_remote)
    edge_audio[board_name] = {
        "tx_ssh": tx, "rx_ssh": rx, "started": time.strftime("%H:%M:%S"),
    }


def _audio_supervisor_loop():
    """엣지 TX/RX 죽으면 재시작. 보드 재부팅/네트워크 끊김 자동 복구."""
    while True:
        try:
            with audio_lock:
                for name, cfg in BOARDS.items():
                    if "audio" not in cfg:
                        continue
                    st = edge_audio.get(name)
                    needs_restart = (
                        st is None
                        or st["tx_ssh"].poll() is not None
                        or st["rx_ssh"].poll() is not None
                    )
                    if needs_restart:
                        # 죽은 거 청소
                        if st:
                            for key in ("tx_ssh", "rx_ssh"):
                                p = st.get(key)
                                if p and p.poll() is None:
                                    try:
                                        p.terminate()
                                    except Exception:
                                        pass
                        _start_edge_tx_rx(name)
        except Exception as e:
            print(f"[audio supervisor] {e}", flush=True)
        time.sleep(8)


def start_audio_supervisor():
    global audio_supervisor_running
    if audio_supervisor_running:
        return
    audio_supervisor_running = True
    t = threading.Thread(target=_audio_supervisor_loop, daemon=True)
    t.start()


def listen_start(board_name):
    """PC측 디코드+재생 ON (해당 보드 음성을 PC 스피커로)."""
    cfg = BOARDS.get(board_name) or {}
    a = cfg.get("audio")
    if not a:
        return {"ok": False, "error": "No audio config"}
    with audio_lock:
        st = pc_listen.get(board_name)
        if st and st["proc"].poll() is None:
            return {"ok": True, "active": True, "message": "Already listening"}
        port = a["tx_port"]
        proc = subprocess.Popen(
            [
                "gst-launch-1.0", "-q",
                "udpsrc", f"port={port}",
                "caps=application/x-rtp,media=audio,encoding-name=OPUS,payload=96,clock-rate=48000",
                "!", "rtpjitterbuffer", "latency=60",
                "!", "rtpopusdepay", "!", "opusdec", "plc=true",
                "!", "audioconvert", "!", "audioresample",
                "!", "autoaudiosink", "sync=false",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pc_listen[board_name] = {"proc": proc, "since": time.strftime("%H:%M:%S")}
    return {"ok": True, "active": True}


def listen_stop(board_name):
    with audio_lock:
        st = pc_listen.pop(board_name, None)
        if st:
            p = st["proc"]
            if p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
    return {"ok": True, "active": False}


def listen_status(board_name):
    st = pc_listen.get(board_name)
    if not st or st["proc"].poll() is not None:
        return {"active": False}
    return {"active": True, "since": st.get("since")}


def pc_mic_start():
    """PC mic → opusenc → tee → 모든 audio 보드의 rx_port로 동시 송출."""
    with audio_lock:
        if pc_mic_state["proc"] and pc_mic_state["proc"].poll() is None:
            return {"ok": True, "active": True, "message": "Already on"}
        targets = []
        for name, cfg in BOARDS.items():
            a = cfg.get("audio")
            if not a:
                continue
            host = cfg["ssh"].split("@")[1]
            targets.append((host, a["rx_port"], name))
        if not targets:
            return {"ok": False, "error": "No audio-capable boards"}

        # 단일 GStreamer 파이프라인: pulsesrc ! ... ! tee name=t  t. ! queue ! rtpopuspay ! udpsink ...
        pipeline = (
            "pulsesrc ! audioconvert ! audioresample ! "
            "opusenc bitrate=32000 inband-fec=true ! tee name=t"
        )
        for host, port, _ in targets:
            pipeline += f" t. ! queue ! rtpopuspay pt=96 ! udpsink host={host} port={port}"

        proc = subprocess.Popen(
            ["gst-launch-1.0", "-q"] + pipeline.split(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pc_mic_state["proc"] = proc
        pc_mic_state["since"] = time.strftime("%H:%M:%S")
        pc_mic_state["targets"] = [n for _, _, n in targets]
    return {"ok": True, "active": True, "targets": pc_mic_state["targets"]}


def pc_mic_stop():
    with audio_lock:
        p = pc_mic_state.get("proc")
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        pc_mic_state["proc"] = None
        pc_mic_state["since"] = None
    return {"ok": True, "active": False}


def pc_mic_status():
    p = pc_mic_state.get("proc")
    if not p or p.poll() is not None:
        return {"active": False}
    return {
        "active": True,
        "since": pc_mic_state.get("since"),
        "targets": pc_mic_state.get("targets", []),
    }


def edge_audio_status(board_name):
    """엣지 TX/RX 상태 (디버깅용)."""
    st = edge_audio.get(board_name)
    if not st:
        return {"tx_alive": False, "rx_alive": False}
    return {
        "tx_alive": st["tx_ssh"].poll() is None,
        "rx_alive": st["rx_ssh"].poll() is None,
        "started": st.get("started"),
    }


def shutdown_all_audio():
    """뷰어 종료 시 호출."""
    with audio_lock:
        for name, st in list(edge_audio.items()):
            for key in ("tx_ssh", "rx_ssh"):
                p = st.get(key)
                if p and p.poll() is None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
        for name, st in list(pc_listen.items()):
            p = st.get("proc")
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        p = pc_mic_state.get("proc")
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass


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
    #micBtn {
        background:#16a34a; color:white; border:none; padding:8px 16px;
        border-radius:6px; cursor:pointer; font-size:0.9em; margin-right:8px;
    }
    #micBtn.active { background:#dc2626; animation:pulse 1.5s infinite; }
    @keyframes pulse {
        0%,100% { box-shadow:0 0 0 0 rgba(220,38,38,0.6); }
        50%     { box-shadow:0 0 0 8px rgba(220,38,38,0); }
    }
    .header-actions { display:flex; align-items:center; }
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
    <div class="header-actions">
        <button id="micBtn" onclick="togglePcMic()">🎤 Mic OFF</button>
        <button id="dashBtn" onclick="toggleDash()">Dashboard</button>
    </div>
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
            // 글로벌 PC Mic 상태 반영
            const micBtn = document.getElementById('micBtn');
            const micState = data._pc_mic || {};
            if (micState.active) {
                micBtn.textContent = '🎤 Mic ON → ' + (micState.targets || []).length + ' boards';
                micBtn.classList.add('active');
            } else {
                micBtn.textContent = '🎤 Mic OFF';
                micBtn.classList.remove('active');
            }
            delete data._pc_mic;

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
                // 오디오 (audio가 있는 보드만)
                if (s.audio) {
                    const a = s.audio;
                    const listening = !!(a.listen && a.listen.active);
                    const txAlive = !!(a.edge && a.edge.tx_alive);
                    const rxAlive = !!(a.edge && a.edge.rx_alive);
                    const edgeDot = (txAlive && rxAlive) ? '🟢' : '🔴';
                    const btnLabel = listening ? '🔇 Mute' : '🎧 Listen';
                    const btnAction = listening ? 'stop' : 'start';
                    const btnStyle = listening
                        ? 'background:#dc2626;color:white;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:0.85em'
                        : 'background:#16a34a;color:white;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:0.85em';
                    const sinceText = listening && a.listen.since ? ` <span style="color:#888;font-size:0.8em">since ${a.listen.since}</span>` : '';
                    body += `
                        <div class="stat-row" style="margin-top:8px">
                            <span class="stat-label">${edgeDot} ${a.label}${sinceText}</span>
                            <button style="${btnStyle}" onclick="toggleListen('${name}','${btnAction}')">${btnLabel}</button>
                        </div>
                    `;
                }

                // 모드 전환 버튼 (modes가 있는 보드만)
                if (s.modes) {
                    body += `<div style="margin-top:10px;display:flex;gap:6px">`;
                    for (const [modeKey, modeInfo] of Object.entries(s.modes)) {
                        const active = modeKey === s.current_mode;
                        const btnStyle = active
                            ? 'background:#2563eb;color:white;border:none;padding:6px 12px;border-radius:4px;cursor:default;font-size:0.85em'
                            : 'background:#333;color:#ccc;border:1px solid #555;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:0.85em';
                        body += `<button style="${btnStyle}" onclick="${active ? '' : `switchMode('${name}','${modeKey}')`}">${modeInfo.label}${active ? ' ●' : ''}</button>`;
                    }
                    body += `</div>`;
                }

                const card = `<div class="board-card"><h3>${dot}${name}</h3>${body}</div>`;
                grid.innerHTML += card;
            }
        })
        .catch(() => {});
}

function toggleListen(boardName, action) {
    fetch(`/api/listen?board=${encodeURIComponent(boardName)}&action=${action}`)
        .then(r => r.json())
        .then(data => {
            if (!data.ok) alert('Listen ' + action + ' failed: ' + (data.error || ''));
            setTimeout(fetchStatus, 400);
        })
        .catch(e => alert('Error: ' + e));
}

function togglePcMic() {
    const btn = document.getElementById('micBtn');
    const action = btn.classList.contains('active') ? 'stop' : 'start';
    fetch(`/api/mic?action=${action}`)
        .then(r => r.json())
        .then(data => {
            if (!data.ok) alert('Mic ' + action + ' failed: ' + (data.error || ''));
            // 대시보드 닫혀있어도 즉시 갱신 위해 한 번 호출
            setTimeout(fetchStatus, 400);
        })
        .catch(e => alert('Error: ' + e));
}

function refreshMicState() {
    // 대시보드 닫혀 있어도 헤더 버튼 상태는 항상 갱신
    if (!dashOpen) fetchStatus();
}
setInterval(refreshMicState, 5000);

function switchMode(boardName, mode) {
    if (!confirm(`Switch ${boardName} to ${mode} mode?`)) return;
    fetch(`/api/switch?board=${encodeURIComponent(boardName)}&mode=${mode}`)
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                alert(`Switching to ${mode}... Stream will reconnect in ~30 seconds.`);
                setTimeout(fetchStatus, 5000);
            } else {
                alert('Switch failed: ' + data.error);
            }
        })
        .catch(e => alert('Error: ' + e));
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
                data = {}
                for name, s in board_status.items():
                    data[name] = dict(s)
                    cfg = BOARDS.get(name, {})
                    if "modes" in cfg:
                        data[name]["modes"] = cfg["modes"]
                        data[name]["current_mode"] = cfg["current_mode"]
                    if "audio" in cfg:
                        data[name]["audio"] = {
                            "label": cfg["audio"].get("label", "Audio"),
                            "edge": edge_audio_status(name),
                            "listen": listen_status(name),
                        }
                data["_pc_mic"] = pc_mic_status()
            self.wfile.write(json.dumps(data).encode())
        elif self.path.startswith("/api/switch"):
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(self.path).query)
            board_name = params.get("board", [""])[0]
            mode = params.get("mode", [""])[0]
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            result = switch_board_mode(board_name, mode)
            self.wfile.write(json.dumps(result).encode())
        elif self.path.startswith("/api/listen"):
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(self.path).query)
            board_name = params.get("board", [""])[0]
            action = params.get("action", [""])[0]
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            if action == "start":
                result = listen_start(board_name)
            elif action == "stop":
                result = listen_stop(board_name)
            else:
                result = {"ok": False, "error": "action must be start|stop"}
            self.wfile.write(json.dumps(result).encode())
        elif self.path.startswith("/api/mic"):
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(self.path).query)
            action = params.get("action", [""])[0]
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            if action == "start":
                result = pc_mic_start()
            elif action == "stop":
                result = pc_mic_stop()
            else:
                result = {"ok": False, "error": "action must be start|stop"}
            self.wfile.write(json.dumps(result).encode())
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
    mode = "LAN" if USE_LAN else "Tailscale"
    print(f"Starting Multi Board Viewer ({len(names)} boards, {mode} mode)")

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

    # 오디오: audio 있는 보드의 TX/RX 항상 ON (supervisor가 죽으면 재시작)
    audio_boards = [n for n, c in BOARDS.items() if "audio" in c]
    if audio_boards:
        print(f"  audio always-on: {audio_boards}")
        start_audio_supervisor()

    import atexit
    atexit.register(shutdown_all_audio)

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
