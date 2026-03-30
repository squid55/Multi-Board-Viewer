# Multi Board Viewer

여러 개발 보드의 카메라 스트림을 PC 한 화면에서 동시에 모니터링하는 통합 뷰어.

실시간 대시보드로 각 보드의 CPU, 메모리, 온도, 업타임을 확인할 수 있습니다.

![Multi Board Viewer Dashboard](docs/dashboard_demo.png)
*Jetson Orin Nano (YOLOv8 실시간 객체 탐지) + Raspberry Pi 3B 동시 스트리밍 + 대시보드*

## Features

- 멀티 보드 카메라 스트림 2x2 (또는 NxN) 그리드 뷰
- 실시간 대시보드 (CPU / Memory / Temperature / Uptime)
- 멀티스레드 서버 — 여러 기기에서 동시 접속 가능
- 보드 연결/해제 자동 감지 및 재연결
- 브라우저 모드 / OpenCV 윈도우 모드 지원

## Supported Boards

| 보드 | 스트리밍 | AI 추론 | 상태 |
|------|---------|--------|------|
| Jetson Orin Nano 4GB | MJPEG over HTTP | YOLOv8n TensorRT INT8 | 완료 |
| Raspberry Pi 3B | MJPEG over HTTP | - | 완료 |
| Jetson Nano | MJPEG over HTTP | YOLOv8n | 예정 |
| Zybo Z7-20 | GStreamer RTSP | FPGA CNN | 예정 |
| Arty Z7-10 | HDMI 캡처카드 | - | 예정 |

## Usage

```bash
# 브라우저 모드 (권장)
python3 multi_board_viewer.py --web
# http://localhost:9090 에서 확인

# OpenCV 윈도우 모드
python3 multi_board_viewer.py
```

## Requirements

```bash
pip3 install opencv-python-headless numpy
```

## Configuration

`multi_board_viewer.py`의 `BOARDS` 딕셔너리에서 보드를 추가/수정:

```python
BOARDS = {
    "Board Name": {
        "stream": "http://<board-ip>:8080/stream",
        "ssh": "user@<board-ip>",
    },
}
```

## Network

### LAN (사설망)
```
[공유기 LAN]
    ├── Jetson Orin Nano (192.168.219.108)
    ├── Raspberry Pi 3B  (192.168.219.109)
    ├── Jetson Nano       (예정)
    └── Zybo Z7-20        (예정)

[PC] → WiFi로 공유기에 연결 → multi_board_viewer.py 실행
```

### Tailscale (외부 접속)

Tailscale VPN을 통해 외부 네트워크(LTE/5G, 카페, 학교 등)에서도 접속 가능.

```
[Tailscale Network]
    PC           100.125.10.87    → 통합 뷰어 :9090
    Jetson Orin  100.77.67.60     → YOLOv8 스트림 :8080
    RPi 3B       100.123.127.114  → 카메라 스트림 :8080
```

설치:
```bash
# 각 보드 + PC에서
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up
```

## Auto Start

각 보드는 전원 인가 시 자동으로 스트리밍이 시작됨 (systemd service).

| 보드 | 서비스명 | 상태 |
|------|---------|------|
| Jetson Orin | `yolo-stream.service` / `fall-detection.service` | 등록 완료 |
| RPi 3B | `camera-stream.service` | 등록 완료 |

## AI Mode Switching

대시보드에서 Jetson Orin Nano의 AI 모드를 실시간 전환 가능:

| 모드 | 설명 | 포트 |
|------|------|------|
| **YOLOv8 Detection** | 80종 객체 탐지 (person, car, bottle...) | 8080 |
| **Fall Detection** | YOLOv8-Pose 포즈 추정 + 추락/쓰러짐 감지 | 8081 |

Dashboard 버튼 클릭 → 모드 버튼 클릭 → SSH로 Jetson 서비스 자동 전환.
Tailscale 연결 시 원격에서도 모드 전환 가능.

## Related

- [Jetson-AI-Camera](https://github.com/squid55/Jetson-AI-Camera) — Jetson Orin Nano YOLOv8 실시간 추론
- [Construction-Safety-AI](https://github.com/squid55/Construction-Safety-AI) — 건설현장 추락사고 AI 감지

---

## Changelog

### v1.3.0 (2026-03-30)
- AI 모드 전환 기능 추가 (YOLOv8 Detection ↔ Fall Detection)
- 대시보드에서 모드 전환 버튼 UI
- SSH를 통한 원격 서비스 전환 (Tailscale 지원)
- Fall Detection systemd 서비스 등록

### v1.2.0 (2026-03-29)
- 멀티스레드 HTTP 서버 (동시 접속 지원)
- Tailscale VPN 외부 접속 설정
- systemd 자동 시작 서비스 등록 (Jetson + RPi)

### v1.1.0 (2026-03-29)
- 실시간 대시보드 추가 (CPU, 메모리, 온도, 업타임)
- Dashboard 토글 버튼
- 프로그레스 바 채워지기 수정

### v1.0.0 (2026-03-29)
- 멀티 보드 카메라 스트리밍 통합 뷰어
- 2x2 그리드 레이아웃
- 보드 자동 재연결
- 브라우저 모드 / OpenCV 모드 지원
