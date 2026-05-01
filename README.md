# Multi Board Viewer

5종 이기종 임베디드 보드(Jetson Orin / Nano / RPi / Zybo / Arty)의 카메라 영상과 USB 헤드셋 음성을 PC 한 곳에서 통합 모니터링하고 양방향 인터콤으로 협업하는 분산 시스템.

![Multi Board Viewer Dashboard](docs/dashboard_demo.png)
*Jetson Orin Nano (YOLOv8 실시간 객체 탐지) + Raspberry Pi 3B 동시 스트리밍 + 대시보드*

> 🏗️ **자세한 소프트웨어 스택과 아키텍처는 [ARCHITECTURE.md](ARCHITECTURE.md) 참고**

---

## Features

- **멀티 보드 카메라 스트림** 2×2 (또는 N×N) 그리드 뷰
- **실시간 대시보드** — CPU / Memory / Temperature / Uptime
- **음성 양방향 인터콤** (v1.4.0+) — 보드별 `🎧 Listen` + 글로벌 `🎤 Mic`
- **AI 모드 전환** — Jetson Orin YOLOv8 ↔ Fall Detection 원격 전환
- **LAN/Tailscale 자동 전환** — 같은 네트워크면 LAN 우선, 외부면 Tailscale
- **멀티스레드 서버** — 여러 기기에서 동시 접속, 보드 자동 재연결
- **헤드리스 엣지 always-on 모델** — 사용자 입력이 없는 보드는 PC가 SSH supervisor로 음성 파이프 자동 유지

---

## Quick Start

```bash
# 의존성 설치 (PC)
sudo apt install python3-opencv gstreamer1.0-tools \
                 gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
                 gstreamer1.0-plugins-ugly openssh-client

# 브라우저 모드로 실행
python3 multi_board_viewer.py --web
# → http://localhost:9090 에서 확인
```

---

## Supported Boards

| 보드 | 영상 | 음성 (USB 헤드셋) | AI 추론 | 상태 |
|------|------|------|--------|------|
| Jetson Orin Nano 4GB | MJPEG/HTTP | Opus/RTP 양방향 | YOLOv8n TensorRT INT8 | ✅ 완료 |
| Raspberry Pi 3B | MJPEG/HTTP | (헤드셋 추가 시 자동 활성) | — | ✅ 완료 |
| Jetson Nano | MJPEG/HTTP | (헤드셋 추가 시 자동 활성) | YOLOv8n | ✅ 완료 |
| Zybo Z7-20 | GStreamer RTSP | (PetaLinux 검토 필요) | FPGA CNN (MNIST) | ⏳ SD 굽기 대기 |
| Arty Z7-10 | HDMI 캡처카드 | — | (FPGA 신호처리) | ⏳ 예정 |

---

## Audio Intercom (v1.4.0)

**모델**: 엣지 보드는 헤드리스(모니터/키보드 없음)이므로 **항상 송수신 ON**, PC 사용자만 토글.

| 컨트롤 | 위치 | 동작 |
|---|---|---|
| `🎧 Listen` / `🔇 Mute` | 보드 카드 | 해당 보드 음성을 PC 스피커로 디코드/재생 토글 |
| `🎤 Mic ON` / `🎤 Mic OFF` | 헤더 (글로벌) | PC 마이크를 모든 audio 보드에 동시 송출 (`tee`) |

**시그널 흐름**:
```
엣지 헤드셋 mic ──Opus/RTP──▶ PC port 5004/5/6/7 (보드별)
PC 마이크 ──Opus/RTP──▶ 모든 엣지 port 5010 (공통)
```

**검증된 코덱/네트워크**: GStreamer 1.20 + Opus 32 kbps + RTP/UDP + jitter buffer 60 ms + PLC.

자세한 설계는 [ARCHITECTURE.md § Audio System](ARCHITECTURE.md#5-audio-pipeline)을 참고하세요.

---

## Configuration

`multi_board_viewer.py`의 `BOARDS` 딕셔너리에서 보드 추가:

```python
BOARDS = {
    "Jetson Orin Nano": {
        "stream_lan":       "http://192.168.0.X:8080/stream",
        "stream_tailscale": "http://100.77.67.60:8080/stream",
        "ssh_lan":          "jetson-nx@192.168.0.X",
        "ssh_tailscale":    "jetson-nx@100.77.67.60",
        "modes": {
            "yolo": {"service": "yolo-stream",     "port": 8080, "label": "YOLOv8"},
            "fall": {"service": "fall-detection",  "port": 8081, "label": "Fall Detect"},
        },
        "current_mode": "yolo",
        "audio": {                       # 헤드셋 추가 시만 작성
            "tx_port": 5004,             # 보드 mic → PC
            "rx_port": 5010,             # PC mic → 보드 (모든 엣지 공통)
            "alsa_in":  "plughw:0,0",
            "alsa_out": "plughw:0,0",
            "label": "USB Headset",
        },
    },
    # 다른 보드들...
}
```

---

## Network

### LAN (사설망)
```
[공유기 LAN]
    ├── Jetson Orin Nano  (192.168.0.X)
    ├── Jetson Nano        (192.168.0.11)
    ├── Raspberry Pi 3B    (192.168.0.13)
    ├── Zybo Z7-20         (192.168.0.14, 예정)
    └── PC (뷰어)          → 동일 네트워크 자동 감지
```

### Tailscale (외부 접속)

WireGuard 기반 P2P VPN. 외부 네트워크(LTE/5G/카페)에서도 동일 코드로 동작.

```
[Tailscale Network]
    PC           100.125.10.87    → 통합 뷰어 :9090
    Jetson Orin  100.77.67.60     → YOLOv8 스트림 :8080
    RPi 3B       100.123.127.114  → 카메라 스트림 :8080
    Jetson Nano  100.125.186.100  → 카메라 스트림 :8080
```

설치 (각 보드 + PC):
```bash
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up
```

뷰어가 `is_lan()`으로 자동 판별 — 같은 LAN이면 사설 IP, 아니면 Tailscale IP를 사용합니다.

---

## Auto Start

| 보드 | 서비스 | 트리거 | 상태 |
|---|---|---|---|
| Jetson Orin | `yolo-stream.service` / `fall-detection.service` | systemd | 등록 완료 |
| RPi 3B | `camera-stream.service` | systemd | 등록 완료 |
| Jetson Nano | `camera-stream.service` | systemd | 등록 완료 |
| (음성) 모든 audio 보드 | PC 뷰어의 SSH supervisor | 뷰어 기동 시 자동 | 코드 통합됨 |

음성 쪽은 일반 systemd가 아니라 **SSH supervisor 패턴**을 사용합니다. PC 뷰어가 시작될 때 SSH로 엣지 GStreamer 파이프를 띄우고, supervisor 스레드가 8초마다 자식 상태를 점검해 죽으면 재시작 (sudo 권한 없이도 systemd Restart=always와 동일한 효과).

---

## AI Mode Switching

대시보드에서 Jetson Orin Nano의 AI 모드를 실시간 전환:

| 모드 | 설명 | 포트 |
|------|------|------|
| **YOLOv8 Detection** | 80종 객체 탐지 (person, car, bottle...) | 8080 |
| **Fall Detection** | YOLOv8-Pose 포즈 추정 + 추락/쓰러짐 감지 | 8081 |

대시보드 → 모드 버튼 → SSH로 Jetson `systemctl stop/start` → 캡처 스레드가 새 포트에 자동 재연결.

---

## Software Stack (요약)

| 계층 | 기술 |
|---|---|
| AI 추론 | YOLOv8n + TensorRT INT8 (Orin), FPGA CNN MNIST (Zybo) |
| 영상 인코딩 | MJPEG over HTTP `multipart/x-mixed-replace` |
| 음성 인코딩 | Opus 32 kbps over RTP/UDP |
| 미디어 프레임워크 | GStreamer 1.20 (alsasrc, opusenc, rtpopuspay, tee, rtpjitterbuffer, ...) |
| 백엔드 | Python 3.10 + `ThreadingHTTPServer` + `subprocess` + `threading` |
| 영상 처리 | OpenCV 4 (JPEG SOI/EOI 직접 파싱, 그리드 합성) |
| 프론트엔드 | Vanilla JS + HTML5 + CSS3 (단일 파일 임베드) |
| 네트워크 | Tailscale (WireGuard) + 자동 LAN/VPN 전환 |
| 원격 자동화 | SSH (영상 = systemd, 음성 = SSH supervisor) |

깊은 설명: **[ARCHITECTURE.md](ARCHITECTURE.md)**

---

## Related

- [Jetson-AI-Camera](https://github.com/squid55/Jetson-AI-Camera) — Jetson Orin Nano YOLOv8 실시간 추론
- [Construction-Safety-AI](https://github.com/squid55/Construction-Safety-AI) — 건설현장 추락사고 AI 감지

---

## Changelog

### v1.4.0 (2026-05-01) — Audio Intercom
- 양방향 음성 통신 (Opus/RTP) — Phase 1/2 검증 + Phase 3 통합
- USB 헤드셋 → PC 스피커 (포트별 보드 분리)
- PC 마이크 → 모든 엣지 동시 송출 (GStreamer `tee`)
- 헤더 글로벌 `🎤 Mic ON/OFF` + 보드 카드별 `🎧 Listen / 🔇 Mute`
- 헤드리스 엣지 always-on 모델 + SSH supervisor 자동 재시작
- LAN/Tailscale 자동 PC IP 선택 (`get_pc_ip_for_board()`)
- API: `/api/listen`, `/api/mic`, `/api/status`에 `audio.edge`/`audio.listen`/`_pc_mic` 추가
- 신규 문서: [ARCHITECTURE.md](ARCHITECTURE.md) — 소프트웨어 스택, 데이터 흐름, VoIP 비교

### v1.3.0 (2026-03-30)
- AI 모드 전환 (YOLOv8 ↔ Fall Detection)
- 대시보드 모드 전환 버튼 UI, SSH 원격 서비스 전환

### v1.2.0 (2026-03-29)
- 멀티스레드 HTTP 서버 (동시 접속 지원)
- Tailscale VPN 외부 접속 설정
- systemd 자동 시작 서비스 등록 (Jetson + RPi)

### v1.1.0 (2026-03-29)
- 실시간 대시보드 (CPU, 메모리, 온도, 업타임)

### v1.0.0 (2026-03-29)
- 멀티 보드 카메라 스트리밍 통합 뷰어
- 2×2 그리드, 보드 자동 재연결, 브라우저/OpenCV 모드
