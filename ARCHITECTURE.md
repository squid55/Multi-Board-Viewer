# Architecture

> 5종 이기종 임베디드 보드를 단일 PC 대시보드로 통합 운영하기 위한 분산 시스템의 기술 설계 문서.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Topology Diagram](#2-topology-diagram)
3. [Software Stack](#3-software-stack)
4. [Design Decisions (Why?)](#4-design-decisions-why)
5. [Audio Pipeline](#5-audio-pipeline)
6. [Video Pipeline](#6-video-pipeline)
7. [Control Plane (Status & Mode Switching)](#7-control-plane-status--mode-switching)
8. [Code Structure](#8-code-structure)
9. [Network & Port Plan](#9-network--port-plan)
10. [Comparison with Standard VoIP / IP-PBX](#10-comparison-with-standard-voip--ip-pbx)
11. [Validation Status](#11-validation-status)

---

## 1. System Overview

5종의 임베디드 보드(Jetson Orin Nano, Jetson Nano, Raspberry Pi 3B, Zybo Z7-20, Arty Z7-10)에 분산된 카메라/마이크/AI 추론 자원을 **단일 PC 대시보드**(`multi_board_viewer.py`)에서 통합 모니터링/제어한다.

핵심 가치:

- **N개 엣지 → 1 PC**: 한 화면에서 영상 그리드 + 메트릭 + 음성 인터콤
- **헤드리스 엣지의 완전 원격 운영**: 사용자 입력이 PC에만 있어도 모든 보드 제어
- **동일 코드 / LAN/외부 무관**: Tailscale + 자동 전환으로 어떤 네트워크에서도 동작

---

## 2. Topology Diagram

```
                     ┌────────────────────────────────────────┐
                     │       PC (X13, 사용자 컨트롤)            │
                     │                                         │
                     │  multi_board_viewer.py (Python)         │
                     │  ┌────────────────────────────────────┐ │
                     │  │ ThreadingHTTPServer :9090          │ │
                     │  │  ├── /          → 대시보드 HTML     │ │
                     │  │  ├── /stream    → 통합 그리드 MJPEG │ │
                     │  │  ├── /api/status                   │ │
                     │  │  ├── /api/listen, /api/mic         │ │
                     │  │  └── /api/switch (모드 전환)        │ │
                     │  └────────────────────────────────────┘ │
                     │  ┌────────────────────────────────────┐ │
                     │  │ Capture threads × N (MJPEG 파싱)    │ │
                     │  │ Status threads × N (SSH 5초 주기)   │ │
                     │  │ Audio supervisor (SSH 자동 복구)     │ │
                     │  │ GStreamer subprocess (Opus 송수신)  │ │
                     │  └────────────────────────────────────┘ │
                     └─────┬──────────────────────────────────┬┘
                           │ HTTP MJPEG / Opus RTP / SSH       │
              Tailscale (WireGuard P2P) 또는 LAN 자동 전환
                           │                                   │
       ┌───────────────────┼───────────┬──────────────┐        │
       │                   │           │              │        │
┌──────▼──────┐    ┌───────▼──────┐  ┌─▼──────┐  ┌───▼────┐    │
│ Jetson Orin │    │ Jetson Nano  │  │ RPi 3B │  │ Zybo   │    │
│  (사령관)    │    │  (보조 추론)  │  │ (감각)  │  │ (반사) │    │
│             │    │              │  │        │  │        │    │
│ YOLOv8n     │    │ MJPEG 송출   │  │ MJPEG  │  │ FPGA   │    │
│ TensorRT    │    │ + IMX219 CSI │  │ + USB  │  │ CNN    │    │
│ INT8        │    │              │  │ cam    │  │ (Pcam) │    │
│             │    │              │  │        │  │        │    │
│ + USB 헤드셋 │    │              │  │        │  │        │    │
│   alsasrc   │    │              │  │        │  │        │    │
│   alsasink  │    │              │  │        │  │        │    │
└─────────────┘    └──────────────┘  └────────┘  └────────┘    │
                                                                │
        ┌──── Arty Z7-10 ──── HDMI 캡처카드 ────────────────────┘
        │   (순수 FPGA, 네트워크 미지원, HDMI 직결)
```

---

## 3. Software Stack

| 계층 | 사용 기술 | 역할 |
|---|---|---|
| **AI 추론** | YOLOv8n (Ultralytics) + **TensorRT INT8** | Orin 객체 탐지 (~30 FPS) |
| | YOLOv8-Pose | Orin Fall Detection 모드 |
| | MNIST 8-bit 고정소수점 CNN (Verilog/HLS) | Zybo FPGA 초저지연 분류 |
| **영상 인코딩** | **MJPEG** over HTTP `multipart/x-mixed-replace` | 단순/범용/브라우저 직접 재생 |
| | (옵션) GStreamer H.264 NVENC | 고압축 시 |
| **음성 인코딩** | **Opus** 32 kbps over RTP/UDP | 음성 대역 거의 무손실, ~50 ms 지연 |
| **미디어 프레임워크** | **GStreamer 1.20** (`alsasrc/sink`, `opusenc/dec`, `rtpopuspay/depay`, `rtpjitterbuffer`, `tee`) | 모든 보드 공통 |
| **백엔드** | Python 3.10 + `ThreadingHTTPServer` + `subprocess` + `threading` | 단일 파일 서버 |
| **영상 처리** | OpenCV 4 (`cv2.imdecode`/`imencode`/`resize`/`putText`) | JPEG 직접 파싱 + 그리드 합성 |
| **프론트엔드** | Vanilla JS + HTML5 + CSS3 (단일 HTML 문자열 임베드) | 의존성 0, 즉시 동작 |
| **네트워크** | **Tailscale** (WireGuard 기반 P2P VPN) | 어디서든 접근, NAT 우회 |
| | 자체 LAN/Tailscale 자동전환 (`is_lan()`) | 동일 네트워크면 LAN 우선 |
| **원격 자동화** | SSH (OpenSSH, ServerAlive 옵션) | 보드 상태 수집, 모드 전환, 음성 파이프 |
| | `systemd` 서비스 | 영상 자동 시작 |
| | **SSH supervisor** 패턴 | 음성 자동 시작 (sudo 회피) |
| **OS** | Ubuntu 22.04 (PC) | |
| | JetPack 5.x / L4T R32.7.1 | Jetson Orin / Nano |
| | Raspberry Pi OS | RPi 3B |
| | PetaLinux 2023.x | Zybo Z7-20 |

---

## 4. Design Decisions (Why?)

### 4.1 영상 = MJPEG, 음성 = Opus/RTP — WebRTC를 *쓰지 않은* 이유

| 비교 항목 | WebRTC | 본 시스템 (MJPEG + Opus/RTP) |
|---|---|---|
| 시그널링 인프라 | SDP/ICE 협상 서버 필요 | 불요 (PC가 직접 GStreamer spawn) |
| FPGA/PetaLinux 호환 | 빌드 부담 큼 | GStreamer는 모든 보드에서 동작 |
| 브라우저 재생 | JS 스택 필요 | `<img>` 한 줄로 영상 재생 |
| 모바일 브라우저 음성 | 가능 | (현재 미지원) |
| 다보드 모니터링 | 복잡 (mesh) | 단순 (N→1 모델) |

본 시스템은 **N:1 모니터링** 모델이고 휴대폰에서의 음성 청취가 필수가 아니므로, 인프라 단순성을 택해 GStreamer 기반으로 갔다.

### 4.2 PulseAudio가 아닌 ALSA 직결

- 헤드리스 엣지는 PA가 사용자 세션과 함께 떠야 하지만, SSH 자식 프로세스에는 `XDG_RUNTIME_DIR`이 없어서 PA 접속이 자주 실패.
- `alsasrc device=plughw:0,0` / `alsasink device=plughw:0,0` 직결로 PA 의존을 완전 제거.
- USB 사운드카드는 simultaneous full-duplex 지원 → 같은 디바이스에서 mic/spk 동시.

### 4.3 엣지 always-on, PC만 토글하는 비대칭 모델

- 엣지는 **헤드리스** (사용자가 직접 끄거나 켤 수 없음)
- → 엣지의 TX(자기 mic 송출)와 RX(PC 말 수신)를 **항상 켜두고**
- PC 사용자만 "어느 보드 들을지" / "내 마이크 보낼지" 토글
- PC가 안 듣고 있으면 엣지 TX 패킷은 PC OS가 그냥 버린다 (UDP). 데이터 비용은 ~32 kbps × N → 무시 가능.

### 4.4 SSH supervisor 패턴 (sudo 없이 자동 시작)

- 일반 systemd 자동시작은 보드 sudo 권한이 필요 → 일부 보드는 비번 미확보
- 대안: PC 뷰어가 시작될 때 SSH로 엣지 GStreamer 파이프를 띄우고, 백그라운드 supervisor 스레드가 8초마다 자식 상태 확인 → 죽었으면 재시작
- 효과적으로 **`systemd Restart=always`와 동일**한 신뢰도
- 영상 쪽은 sudo가 있어 systemd 사용 (`yolo-stream.service`, `camera-stream.service`)

### 4.5 단일 파일 풀스택

- `multi_board_viewer.py` 하나에 백엔드 + HTML + JS + CSS 전부
- 보드 늘릴 때 `BOARDS` dict에 한 줄만 추가하면 그리드/대시보드/오디오 모두 자동 통합
- 의존성: `python3-opencv`, `gstreamer1.0-plugins-*`, OpenSSH 클라이언트

### 4.6 LAN/Tailscale 자동 전환

- `is_lan()`이 게이트웨이 ping → LAN이면 사설 IP, 아니면 Tailscale IP
- 같은 코드, 같은 보드 설정으로 사무실/외출/LTE/5G 어디서나 동작

---

## 5. Audio Pipeline

### 5.1 모델: 엣지 always-on, PC 토글

```
PC (사용자가 모든 컨트롤)              모든 엣지 (헤드리스, always-on)
┌─────────────────────────┐           ┌──────────────────────┐
│ 🎤 PC mic               │──RTP──┬──▶│ Orin: udpsrc 5010    │
│   [🎤 Mic ON/OFF]       │       ├──▶│ Nano: udpsrc 5010    │
│   글로벌 토글 (헤더)     │       ├──▶│ RPi:  udpsrc 5010    │
│                         │       └──▶│ Zybo: udpsrc 5010    │
│                         │           │     ↓ alsasink (헤드셋 spk) │
│                         │           │                      │
│ 🔊 PC spk               │           │ alsasrc (헤드셋 mic) │
│   [🎧 Listen / 🔇 Mute] │◀─5004─────│ Orin: opusenc + udpsink │
│   보드별 토글 (카드)     │◀─5005─────│ Nano                  │
│                         │◀─5006─────│ RPi                   │
│                         │◀─5007─────│ Zybo                  │
└─────────────────────────┘           └──────────────────────┘
```

- 엣지 TX/RX는 PC 뷰어가 시작될 때 **SSH로 자동 spawn**
- supervisor 스레드가 8초 간격으로 자식 헬스체크 → 죽으면 즉시 재시작
- PC 사용자만 보드별 Listen/Mute, 글로벌 PC Mic ON/OFF 토글

### 5.2 GStreamer Pipelines

**엣지 mic → PC spk (Phase 1, 검증 완료)**
```bash
# 엣지 측 송신 (SSH로 PC가 spawn)
gst-launch-1.0 -q alsasrc device=plughw:0,0 ! \
    audioconvert ! audioresample ! \
    opusenc bitrate=32000 inband-fec=true ! \
    rtpopuspay pt=96 ! \
    udpsink host=<PC_IP> port=5004

# PC 측 수신 (Listen 클릭 시)
gst-launch-1.0 -q udpsrc port=5004 \
    caps='application/x-rtp,media=audio,encoding-name=OPUS,payload=96,clock-rate=48000' ! \
    rtpjitterbuffer latency=60 ! \
    rtpopusdepay ! opusdec plc=true ! \
    audioconvert ! audioresample ! \
    autoaudiosink sync=false
```

**PC mic → 다중 엣지 spk 동시 송출 (Phase 3, `tee` 사용)**
```bash
gst-launch-1.0 -q \
    pulsesrc ! audioconvert ! audioresample ! \
    opusenc bitrate=32000 inband-fec=true ! tee name=t \
    t. ! queue ! rtpopuspay pt=96 ! udpsink host=<Orin_IP> port=5010 \
    t. ! queue ! rtpopuspay pt=96 ! udpsink host=<Nano_IP> port=5010 \
    t. ! queue ! rtpopuspay pt=96 ! udpsink host=<RPi_IP>  port=5010
```

**엣지 PC말 수신 → 헤드셋 spk**
```bash
gst-launch-1.0 -q udpsrc port=5010 \
    caps='application/x-rtp,media=audio,encoding-name=OPUS,payload=96,clock-rate=48000' ! \
    rtpjitterbuffer latency=60 ! \
    rtpopusdepay ! opusdec plc=true ! \
    audioconvert ! audioresample ! \
    alsasink device=plughw:0,0 sync=false
```

### 5.3 Codec / Network Choices

| 항목 | 선택 | 근거 |
|---|---|---|
| 코덱 | Opus 32 kbps | 음성 대역 거의 무손실, ~50 ms 지연 |
| 트랜스포트 | UDP/RTP | 음성은 재전송보다 최신성 중요 |
| jitter buffer | 60 ms | Tailscale에서 패킷 손실 흡수 |
| PLC | `opusdec plc=true` | 손실 패킷 보간 |
| 에코 캔슬링 | 양쪽 헤드셋 사용 | AEC 라이브러리 불요 |

---

## 6. Video Pipeline

```
엣지 카메라
  └→ GStreamer 송출 서버 (각 보드 :8080)
        └→ HTTP multipart/x-mixed-replace
              └→ PC capture_stream() — JPEG SOI/EOI 직접 파싱
                    └→ OpenCV resize(640×360) + 라벨/FPS 오버레이
                          └→ build_grid() — N×N hstack/vstack
                                └→ /stream HTTP 송출 (브라우저 <img>)
```

**구현 디테일**:
- HTTP body에서 `0xFFD8` (SOI) ~ `0xFFD9` (EOI)를 직접 찾아 JPEG 추출 → OpenCV `imdecode`
- 보드 disconnect 시 capture 스레드가 3초마다 재시도, 그 동안 카드에는 "Disconnected" 표시
- 모든 카메라 스트림이 동일 그리드에 합성되어 단일 MJPEG 스트림으로 재송출 → 브라우저는 단일 `<img>`만 받음

---

## 7. Control Plane (Status & Mode Switching)

### 7.1 보드 상태 수집 (5초 주기)

```
PC                SSH (timeout 8s)               엣지
  ──────────────────────────────────▶
       cat /proc/loadavg         (CPU)
       free -b                   (memory)
       cat /sys/class/thermal/.. (temperature)
       uptime -p                 (uptime)
       nproc                     (CPU 코어 수)
  ◀──────────────────────────────────
       파싱 → board_status[name] dict → /api/status JSON
                                              → 대시보드 카드 갱신 (5s polling)
```

### 7.2 AI 모드 전환 (Jetson Orin)

```
대시보드 클릭 [YOLOv8 / Fall Detect]
  └→ /api/switch?board=...&mode=...
        └→ switch_board_mode()
              └→ ssh "systemctl stop <old_service>; sleep 2; systemctl start <new_service>"
                    └→ BOARDS[name].current_mode 업데이트
                          └→ capture 스레드가 자동으로 새 포트 (8080↔8081) 재연결
```

---

## 8. Code Structure

```
multi_board_viewer.py (단일 파일)
│
├─ Configuration
│  ├─ BOARDS dict  — 보드별 stream URL, ssh, modes, audio 정의
│  ├─ is_lan()  / get_pc_ip_for_board()  — 네트워크 자동 감지
│  └─ USE_LAN, PC_IP_FOR_BOARDS
│
├─ Status Collection
│  └─ collect_status(name, ssh_target)  — SSH로 메트릭 수집
│
├─ Video Pipeline
│  ├─ capture_stream(name, url, color)  — MJPEG SOI/EOI 직접 파싱
│  ├─ build_grid(names)                  — N개 프레임 → 단일 이미지
│  └─ grid_update_loop(names)            — 30 FPS로 latest_grid_jpg 갱신
│
├─ Mode Switching
│  └─ switch_board_mode(board, mode)     — SSH로 service stop/start
│
├─ Audio Pipeline                       ★ v1.4.0 신규
│  ├─ _start_edge_tx_rx(board)           — SSH로 엣지 송수신 파이프 spawn
│  ├─ _audio_supervisor_loop()           — 8초 헬스체크 + 재시작
│  ├─ listen_start/stop(board)           — PC측 디코드+재생 토글
│  ├─ pc_mic_start/stop()                — `tee`로 다중 엣지 동시 송출
│  └─ shutdown_all_audio()               — atexit 핸들러
│
├─ HTTP API
│  └─ ViewerHandler (BaseHTTPRequestHandler)
│     ├─ /              → HTML_PAGE
│     ├─ /stream        → multipart MJPEG
│     ├─ /api/status    → JSON (보드별 + _pc_mic 글로벌)
│     ├─ /api/listen    → 보드별 재생 토글
│     ├─ /api/mic       → PC mic 글로벌 토글
│     └─ /api/switch    → 모드 전환
│
└─ Frontend (HTML_PAGE 문자열 내부)
   ├─ #micBtn (헤더 글로벌 토글, 펄스 애니메이션)
   ├─ #dashBtn (대시보드 토글)
   ├─ .board-card × N (보드별 CPU/MEM/온도/Listen/모드전환)
   └─ fetchStatus() — 5초마다 /api/status polling
```

---

## 9. Network & Port Plan

| Port | 프로토콜 | 방향 | 용도 |
|---|---|---|---|
| 8080 | HTTP MJPEG | 엣지 → PC | 카메라 영상 (기본) |
| 8081 | HTTP MJPEG | 엣지 → PC | (Orin) Fall Detection 모드 |
| 9090 | HTTP | PC | 통합 뷰어 + 대시보드 |
| 5004 | UDP/RTP | Orin → PC | 음성 (Orin mic) |
| 5005 | UDP/RTP | Nano → PC | 음성 (Nano mic, 예정) |
| 5006 | UDP/RTP | RPi → PC | 음성 (RPi mic, 예정) |
| 5007 | UDP/RTP | Zybo → PC | 음성 (Zybo mic, 예정) |
| **5010** | UDP/RTP | **PC → 모든 엣지** | PC mic 동시 송출 (공통) |
| 22 | TCP SSH | PC ↔ 엣지 | 상태 수집, 모드 전환, 음성 파이프 spawn |

---

## 10. Comparison with Standard VoIP / IP-PBX

본 시스템은 SIP 기반 IP-PBX와 **개념적으로 동일한 라우팅 결정자** 역할을 한다. 차이는 시그널링 레이어의 구현 방식뿐.

### 10.1 매핑 표

| 레이어 | 표준 VoIP (Asterisk/FreeSWITCH PBX) | 본 시스템 (`multi_board_viewer.py`) |
|---|---|---|
| **미디어 코덱** | G.711 / G.722 / Opus | **Opus** (동일!) |
| **미디어 트랜스포트** | RTP/UDP | **RTP/UDP** (동일!) |
| **시그널링** | SIP (INVITE/BYE/486 BUSY) | **GStreamer 파이프 직접 spawn (SSH)** |
| **라우팅 결정자** | PBX dialplan | **Python 함수 (listen_start/pc_mic_start)** |
| **단말 등록** | SIP REGISTER | **`BOARDS` dict (정적 등록)** |
| **NAT 우회** | STUN/TURN/ICE | **Tailscale (WireGuard P2P)** |
| **호 시작 트리거** | 사용자가 번호 입력 / 비상벨 누름 | **PC 사용자가 Listen/Mic 버튼 클릭** |
| **점유 상태 관리** | SIP dialog state machine | **`subprocess.poll()` + `audio_lock`** |
| **자동 폴백 (CFB)** | dialplan의 `GotoIf $["${DIALSTATUS}"="BUSY"]` | (현재 없음, dict 한 개로 추가 가능) |

### 10.2 PBX의 "Hunt Group + Call Forward on Busy" 시나리오

산업 안전 시스템 등에서 흔한 패턴:
- A구역 비상벨 → A담당 VoIP가 1순위로 울림
- A담당 통화중 → PBX가 dialplan에 따라 B담당으로 자동 전달

이를 본 시스템에 이식하면 ~10줄로 표현 가능:

```python
ESCALATION = {
    "Jetson Orin Nano":  ["Jetson Nano", "Raspberry Pi 3B"],
    "Jetson Nano":       ["Jetson Orin Nano", "Raspberry Pi 3B"],
    "Raspberry Pi 3B":   ["Jetson Orin Nano", "Jetson Nano"],
}

def emergency_call(source_board):
    """source_board에서 비상 트리거 (GPIO 버튼/AI 추락 감지/큰 소리 등)."""
    if pc_user_available():
        listen_start(source_board)
        return
    for fallback in ESCALATION[source_board]:
        if edge_audio[fallback]["rx_ssh"].poll() is None:
            relay_audio(source_board, fallback)
            return
```

→ 이게 곧 dialplan이고 곧 Hunt Group + CFB. 본 시스템은 **SIP 위에 안 올라가 있을 뿐, 동일한 라우팅 결정자 구조**를 이미 갖추고 있다.

### 10.3 진화 경로

```
[현재]                                     [1단계 진화]
GStreamer + SSH 직접 spawn          ─→    GStreamer + 자체 시그널링 REST API
(우리만의 mini-PBX)                        (호 시작/종료를 HTTP로 협상)
                                            │
                                            ▼
                                      [2단계 진화]
                                      PJSIP + Asterisk
                                      (표준 SIP/RTP, 시판 IP폰 호환)
                                            │
                                            ▼
                                      [3단계 진화]
                                      EN675X / NT98530 IP카메라 SoC
                                      (G.711 HW + NPU AI + 단일 칩)
```

각 단계에서 **RTP/Opus 미디어 레이어는 그대로 유지** — 현재 구현된 자산은 어느 단계로 가도 살아있다.

---

## 11. Validation Status

| 항목 | 상태 | 비고 |
|---|---|---|
| Orin YOLOv8 + Fall Detection 영상 스트리밍 | ✅ | TensorRT INT8, ~30 FPS |
| RPi MJPEG 영상 스트리밍 | ✅ | |
| Jetson Nano MJPEG 영상 스트리밍 | ✅ | IMX219 1280×720@30 |
| 4-board 2×2 그리드 통합 뷰 | ✅ | |
| LAN/Tailscale 자동 전환 | ✅ | |
| 보드별 실시간 대시보드 (CPU/MEM/온도) | ✅ | 5초 주기 SSH polling |
| Orin 모드 전환 (YOLO ↔ Fall) | ✅ | dashboard 버튼 → systemctl |
| **음성 Phase 1**: Orin mic → PC spk | ✅ | raw + 뷰어 통합 검증 |
| **음성 Phase 2**: PC mic → Orin spk | ✅ (raw) | 삑+음성 모두 들림 확인 |
| **음성 Phase 3**: 양방향 통합 코드 | 🔨 | supervisor SSH spawn 디버그 진행 중 |
| Zybo PetaLinux 영상 스트리밍 | ⏳ | SD 카드 굽기 대기 |
| Phase 4 systemd 자동시작 (음성) | ⏳ | Orin sudo 비번 확보 시 진행 |
| 휴대폰 브라우저 음성 듣기 | ⏳ | mediamtx + WebRTC 검토 단계 |

---

## References

- [GStreamer Documentation](https://gstreamer.freedesktop.org/documentation/)
- [Opus Codec RFC 6716](https://tools.ietf.org/html/rfc6716)
- [RTP RFC 3550](https://tools.ietf.org/html/rfc3550)
- [SIP RFC 3261](https://tools.ietf.org/html/rfc3261) — VoIP 비교의 기준
- [Tailscale Architecture](https://tailscale.com/blog/how-tailscale-works/) — WireGuard P2P
- [TensorRT Documentation](https://docs.nvidia.com/deeplearning/tensorrt/) — Orin AI 추론

---

*Last updated: 2026-05-01*
