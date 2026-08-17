# TerraByte Orange Pi telemetry bridge

이 프로그램은 Arduino의 USB serial JSON Lines를 검증하고 수신 시각을 UTC로
기록한 뒤 SQLite outbox에 먼저 저장합니다. 백엔드가 끊겨도 데이터가 남으며,
연결이 복구되면 순서대로 재전송합니다.

## 통신 계약

### Arduino → Orange Pi (serial JSON Lines)

`115200 baud`에서 한 줄에 JSON 객체 하나와 `\n`을 보냅니다. 필수 측정값 세 개가
모두 유효할 때만 `telemetry`를 내보냅니다.

```json
{"message_type":"telemetry","protocol_version":1,"node_id":"terrabyte-node-001","sequence":42,"uptime_ms":123456,"air_temperature_c":24.5,"relative_humidity_pct":61.2,"ppfd_umol_m2_s":382.0,"illuminance_lux":14000.0,"soil_temperature_c":18.5,"soil_moisture_pct":52.0}
```

`soil_temperature_c`와 `soil_moisture_pct`는 해당 프로브를 컴파일했을 때만
나옵니다. **없으면 필드가 통째로 빠지며 0으로 채우지 않습니다** — 관수 판단에
"확신에 찬 완전 건조"로 도달하면 안 되기 때문입니다.

`hello`와 `sensor_status`는 상태 확인용으로 기록만 하고 전송하지 않습니다.
범위를 벗어나면 클램프하지 않고 폐기합니다. `sequence`·`uptime_ms`는 재부팅 시
0부터 다시 시작하고 uint32로 wrap되므로 영구 식별자로 쓰지 않습니다. Orange Pi가
수신 건마다 UUID를 부여하며, 이 UUID와 수신 UTC 시각은 재전송 중에도 바뀌지
않습니다.

### Orange Pi → 백엔드 (MQTT, telemetry envelope v2)

운영 전송은 MQTT입니다. 계약 원본은
[`docs/design/device_model_and_telemetry_contract.md`](../../docs/design/device_model_and_telemetry_contract.md) §6입니다.

```text
tb/v2/{gatewayId}/up/telemetry    게이트웨이 → 서버   QoS 1, retain 안 함
tb/v2/{gatewayId}/up/status       온라인 상태, LWT     QoS 1, retain
tb/v2/{gatewayId}/up/ack          명령 수명주기 전체   QoS 1, retain 안 함
tb/v2/{gatewayId}/dn/command      서버 → 게이트웨이     QoS 1, retain 안 함
```

**인증은 브로커가 담당합니다.** 각 게이트웨이 계정은 자기 `gatewayId` 아래에만
발행할 수 있어(Mosquitto ACL) 토픽 위조가 불가능하고, 그래서 백엔드는 토픽에서
뽑은 `gatewayId`를 신뢰합니다. 공용 `X-Device-Key`는 이 구조로 대체되어
삭제됐습니다.

접속 시 `up/status`에 `{"online": true}`를 retain 발행하고, LWT로
`{"online": false}`를 등록합니다. 연결이 끊기면 브로커가 대신 발행하므로 서버는
오프라인 판정을 위해 폴링하지 않습니다. **명령(`dn/command`)은 절대 retain하지
않습니다** — retain하면 재접속 때마다 오래된 관수 명령이 재실행됩니다.

전달 판정은 PUBACK 기준입니다. MQTT에는 HTTP 4xx에 해당하는 응답이 없어
"영구히 잘못된 페이로드"와 "일시적 장애"를 구분할 수 없으므로, `dead` 격리는
브로커에 닿기 전 **로컬 스키마 검증 실패에만** 적용합니다. 나머지는 전부 재시도이며
outbox가 순서를 보존합니다.

MQTT v5를 씁니다. 3.1.1에서는 브로커가 ACL로 막은 발행에도 PUBACK을 돌려주기
때문에, 게이트웨이가 자기 네임스페이스 밖으로 발행하도록 잘못 설정되면 성공으로
보고되고 outbox에서 지워져 데이터가 조용히 사라집니다. v5의 PUBACK reason code로
이를 감지해 재시도로 처리합니다.

### 관수 명령 중계 (`dn/command` → serial → `up/ack`)

게이트웨이는 **동결된 두 계약 사이의 번역기**입니다. 계약 원본은
[`docs/design/edge_ai_hardening.md`](../../docs/design/edge_ai_hardening.md)
개선 2·3이며, 구현은 `terrabyte_edge/command_relay.py`입니다.

```text
백엔드 ──dn/command (긴 키)──▶ 파이 ──{"t":"cmd"} (짧은 키)──▶ 아두이노
       ◀──up/ack (긴 키)────       ◀──{"t":"ack"}───────────
```

```json
{"schema_version":2,"message_type":"command","command_id":"01J8F3…","node_id":"terrabyte-node-01","pot_id":42,"actuator":"pump","action":"dose","params":{"volume_ml":120,"max_runtime_ms":18000},"expires_at":"2026-08-04T10:02:00Z"}
{"t":"cmd","id":"01J8F3…","act":"pump","ms":18000,"ml":120}
```

`max_runtime_ms` ↔ `ms` 개명에 주의하십시오. 짧은 키는 ATmega328P의 SRAM이
2KB이기 때문입니다.

**TTL은 파이만 판정합니다.** Spring은 발행 전에 거르고, 아두이노는 RTC가 없어
벽시계 비교 자체가 불가능하며 상대 시간 `ms`만 다룹니다(D19). 즉 동기화된 시계와
명령을 동시에 가진 계층은 파이뿐입니다. 만료된 명령은 **시리얼로 나가지 않고**
`phase:"rejected", reason:"EXPIRED"`로 응답합니다 — 2시간 오프라인 후 재접속해
큐잉된 6건을 한꺼번에 받아도 관수 횟수는 정확히 0이 됩니다(F3 지연폭탄).

**명령은 절대 retain하지 않습니다.** 구독 측에서도 마찬가지입니다: retain된
명령을 받으면 실행하지 않고 폐기하고 ERROR를 남깁니다. 재접속마다 오래된 관수가
재실행되는 경로이기 때문입니다.

`ms`는 **클램프하지 않고 그대로** 내려보냅니다. 파이가 30초로 깎으면 펌웨어가
`stop:"volume_reached"`로 답해 **절반만 나간 관수가 완주로 기록**됩니다. 원래
값을 보내야 펌웨어가 `stop:"max_runtime"`과 실제 구동시간으로 답합니다.

**데드맨 틱** — 구동 중 1초 주기로 `{"t":"ka"}`를 보냅니다. 3초 무수신 시 즉시
정지하는 펌웨어 G3의 송신 측입니다. `dn/heartbeat`(30초 QoS0, Spring 생존 신호)와
**다른 것**입니다. 주기가 30배 다릅니다.

**중복 방지는 SQLite에 남깁니다.** QoS1 중복이 두 홉 모두에 있고, 펌웨어
링버퍼는 8개까지만 기억하며, 재시작하면 메모리 집합은 사라집니다. 같은 outbox
DB의 `command_journal` 테이블이 이 창을 프로세스 수명보다 길게 유지합니다.

#### `reason` 3중 어휘

같은 이름이 계층마다 다른 개념입니다. `phase`(4값)가 상태를 정하고 `reason`은
거친 진단이며, **펌웨어의 원문 토큰은 `stop_cause`에 그대로 보존**됩니다.

| 아두이노 | MQTT `reason` | 비고 |
|---|---|---|
| `cooldown` | `INTERLOCK_COOLDOWN` | Java `DenyReason.COOLDOWN`과 **다름**(전자는 발행 전 서버 게이트 6시간, 후자는 발행 후 펌웨어 10분) |
| `busy` | `INTERLOCK_COOLDOWN` | 8개 어휘에 대응값이 없는 펌웨어 로컬 토큰. `NODE_OFFLINE`으로 뭉개지 않습니다 — 노드는 실제로 응답했습니다. 구분은 `stop_cause="busy"`가 보존 |
| `duplicate` | `DUPLICATE` | |
| `watchdog` | `WATCHDOG` | |
| `max_runtime` | `OK` | G1이 제대로 동작한 것이므로 실패가 아닙니다 |
| `volume_reached` | `OK` | |
| (미등록 토큰) | phase별 폴백 + WARN | 없던 사건을 발명하지 않는 방향으로만 |

파이가 자체 거절할 때는 `stop_cause`에 `pi_` 접두사를 붙여 아두이노가 관여하지
않았음을 로그에서 바로 구분할 수 있게 합니다: `pi_expired`, `pi_duplicate`,
`pi_link_down`, `pi_unknown_node`, `pi_ambiguous_node`, `pi_bad_actuator`,
`pi_bad_params`, `pi_bad_schema`, `pi_frame_too_long`, `pi_no_expires_at`.

`estimated_ml`은 펌웨어가 `ml`을 실측해 보내면 그 값을, 아니면 실제 구동시간
대비로 환산한 값을 **올림**해서 씁니다. 과다 보고는 다음 관수를 조금 미루는
정도이지만, 과소 보고는 일일 예산이 못 본 물이 화분에 들어간다는 뜻입니다.

#### 스레드 모델

`mqtt_publisher.py`가 `loop_start()`를 쓰므로 `on_message`는 **paho 네트워크
스레드**에서 실행됩니다. 거기서 TTL 판정·시리얼 write·ack 대기를 하면 MQTT
keepalive를 굶기고 데드락이 가능합니다. **인라인으로 쓰면 단위 테스트는 통과하고
실기에서 죽습니다** — 테스트는 핸들러를 테스트 자기 스레드에서 부르기 때문입니다.

| 스레드 | 하는 일 |
|---|---|
| paho 네트워크 | `offer()` — 큐에 넣고 즉시 반환 |
| `command-relay` | 파싱 → TTL → 중복 판정 → 시리얼 write |
| `serial-ingest-N` | ack 파싱·번역 → outbox에 적재 |
| `command-deadman` | 구동 중 `{"t":"ka"}` |
| `ack-upload` | outbox의 `ack` kind만 별도 배출 |

ack가 텔레메트리와 **별도 스레드**인 이유는 outbox의 head-of-line blocking이
kind별이기 때문입니다. 스레드를 공유하면 백오프 중인 측정값 배치가 발행 타임아웃
동안 스레드를 잡고 있고, 그만큼 늦어진 ack는 서버가 이미 EXPIRED로 처리해
`granted_ml`을 예산에서 그대로 빼버립니다 — 가지 않은 물이 차감되는 **팬텀 예산
차감**입니다.

### HTTP 폴백

`TB_TRANSPORT=http`로 바꾸면 같은 envelope을 `POST /api/telemetry`(성공 `202`)로
보냅니다. 디버그·폴백 경로이며 백엔드에서 기본 비활성입니다
(`app.telemetry.http-ingest.enabled`).

## Orange Pi 설치

Python 3.10 이상을 권장합니다. 먼저 실제 USB 식별자를 확인합니다.

```bash
ls -l /dev/serial/by-id/
```

`/dev/ttyACM0`나 `/dev/ttyUSB0`는 재부팅 또는 재연결 때 번호가 바뀔 수
있으므로 환경 설정에는 `/dev/serial/by-id/...` 경로를 사용하세요.
`capturedAtUtc`는 Orange Pi의 수신 시각이므로 `timedatectl status`에서 NTP
동기화가 활성 상태인지도 확인해야 합니다.

프로젝트의 `edge/pi` 내용을 `/opt/terrabyte-edge`에 배치했다고 가정하면:

```bash
cd /opt/terrabyte-edge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

다음 배포 파일을 복사해 값들을 수정합니다.

```bash
sudo cp deploy/terrabyte-edge.env.example /etc/terrabyte-edge.env
sudo cp deploy/terrabyte-edge.service /etc/systemd/system/
sudo useradd --system --home /var/lib/terrabyte-edge --shell /usr/sbin/nologin terrabyte-edge
sudo usermod -aG dialout terrabyte-edge
sudo install -o root -g terrabyte-edge -m 0640 /dev/null /etc/terrabyte-edge.token
sudoedit /etc/terrabyte-edge.token
sudoedit /etc/terrabyte-edge.env
sudo systemctl daemon-reload
sudo systemctl enable --now terrabyte-edge
```

보드 이미지에서 serial 장치의 그룹이 `dialout`이 아니면 `stat -c '%G'
/dev/serial/by-id/...`로 확인하고 unit의 `SupplementaryGroups`를 바꿉니다.

상태와 로그는 다음처럼 확인합니다. 토큰과 원문 센서 JSON은 로그에 남기지
않습니다.

```bash
systemctl status terrabyte-edge
journalctl -u terrabyte-edge -f
```

## 설정

필수 환경 변수는 `TB_SERIAL_PORTS`, `TB_BACKEND_BASE_URL`,
`TB_CROP_CONTEXT_ID`, `TB_DEVICE_ID`, `TB_EXPECTED_NODE_IDS`와 인증 토큰입니다.
serial의 `node_id`가 `TB_EXPECTED_NODE_IDS` 목록에 없으면 관측을 저장하지 않습니다.
단수형 `TB_SERIAL_PORT`·`TB_EXPECTED_NODE_ID`도 계속 인식하므로 기존 보드의 env는
그대로 두어도 됩니다.
토큰은
`TB_DEVICE_TOKEN_FILE`을 권장하며, 개발할 때만 `TB_DEVICE_TOKEN`을 직접
사용할 수 있습니다. 둘을 동시에 설정하면 시작을 거부합니다. 전체 기본값은
[`deploy/terrabyte-edge.env.example`](deploy/terrabyte-edge.env.example)에
있습니다.

SQLite 파일은 기본 `/var/lib/terrabyte-edge/outbox.sqlite3`입니다. 영구 실패
레코드는 현장 진단을 위해 삭제하지 않습니다. 각 이벤트가 수집 당시의 crop
context를 함께 저장하므로 context 변경 뒤 재전송해도 과거 관측의 귀속이
바뀌지 않습니다. `TB_OUTBOX_MAX_ROWS`에 도달하면 디스크를 무한히 채우는 대신
새 관측을 버리고 `CRITICAL` 로그를 남기므로, 운영 모니터링에서 이 로그와
pending/dead row 수를 경보로 연결해야 합니다.

운영에서는 HTTPS가 기본이며 개발용 HTTP는 `TB_ALLOW_INSECURE_HTTP=true`를
명시해야만 허용됩니다. Orange Pi 시각이 `TB_CLOCK_MINIMUM_UTC`보다 이르면 NTP가
동기화되지 않은 것으로 보고 관측을 폐기합니다.

명령 중계 설정(`TB_COMMAND_*`)은 전부 선택이며 기본값으로 동작합니다. 중계는
MQTT 전송에서 **기본 켜짐**입니다 — 명령 경로의 안전 게이트는 백엔드의 MQTT
디스패처(기본 비활성)이고, 상류에서 아무것도 발행하지 않으면 이 게이트웨이는
명령을 받을 일이 없습니다. 스위치 하나가 한 곳에 있는 편이 서로 어긋날 수 있는
두 곳보다 낫습니다. `TB_COMMAND_DEADMAN_INTERVAL_SECONDS`는 펌웨어 G3(3초)보다
반드시 짧아야 하므로 2초를 넘기면 시작을 거부합니다. 값과 근거는
[`deploy/terrabyte-edge.env.example`](deploy/terrabyte-edge.env.example) 참고.

## 관수 판정 (`terrabyte_edge/irrigation`)

지금 관수할지 말지를 판정합니다. **얼마나**는 `irrigation/volume.py`가 계산해
`IrrigationDecider.decide(features, volume_ml=...)`로 넘깁니다. 산출 불가일 때만
`FIXED_VOLUME_ML`(30 mL)로 폴백하고, 그 사실은 `decision.volume_source`에
남습니다 — "폴백이라 30 mL"와 "수식이 30 mL를 요구"는 화분에 대한 서로 다른
사실이고, 폴백으로 돌고 있는 배포는(`TB_POT_SUBSTRATE_ML` 미설정만으로 충분합니다)
그것 없이는 정상과 구분되지 않습니다.

관수량은 생성자가 아니라 `decide()`에서 주입합니다. **판정기는 오래 살고 관수량은
매 측정마다 바뀝니다.** 일일 예산 규칙도 자리표시자가 아니라 실제로 내보낼 양을
달아야 합니다.

판정은 두 단계이고 **순서가 안전성의 근거**입니다.

1. **안전 봉투(safety envelope)** — 결정론적 규칙. 센서 유효성, 측정 신선도(10분),
   건조 게이트, 최소 간격, 일일 예산. 하나라도 걸리면 즉시 거부하고 **모델은 호출되지
   않습니다.**
2. **랜덤 포레스트** — 봉투를 통과한 경우에만 판정. 봉투가 먼저·독립적으로 평가되므로
   모델은 관수를 **억제만 할 수 있고 결정론적 규칙이 허용한 범위를 넓힐 수 없습니다**
   (`D17`).

프로필 두 가지: `EnvelopeLimits.supervised()`(기본, 건조 게이트 45%·최소 간격 6시간)와
클라우드 장애용 `EnvelopeLimits.autonomous()`(`D16` 기준, 15%·12시간).

모델 아티팩트가 없거나 스키마가 어긋나면 `ModelError`가 발생하고, 판정은
`MODEL_UNAVAILABLE`로 **관수하지 않는 쪽**으로 떨어집니다.

> ⚠️ **`daily_budget_ml`(120 mL)은 배선 전에 재검토가 필요합니다.**
> 고정 30 mL 기준으로 120은 정확히 4회분이고, `min_interval_hours=6`이 이미
> 하루를 4회로 제한합니다. 즉 예산은 간격의 재진술이었고 **한 번도 발동하지
> 않았습니다.** 변동량이 그 우연을 깨서 예산이 먼저 걸리는데, 걸리는 기준이
> 물리적 근거가 없는 숫자입니다. 서버 폴백표는 3–6 L 화분에 120 mL,
> 6 L 초과에 160 mL를 씁니다(`irrigation_volume.md` §3.2) — 즉 큰 화분은
> **1회도 통과하지 못하고** 아무것도 주지 않은 상태로 "예산 소진"이 찍힙니다.
> 그 경우는 `DOSE_EXCEEDS_DAILY_BUDGET`으로 분리해 두었습니다. 안전한 상한은
> 화분 용적에 따라 달라지고 `EnvelopeLimits`는 용적을 모르므로
> (`Settings.pot_substrate_ml`이 압니다), 상수를 조용히 올리지 않았습니다 —
> 틀렸지만 감지되는 값을 틀렸는데 그럴듯한 값으로 바꾸는 셈입니다.
> **배선하는 사람이 화분 용적에서 유도한 예산을 넘겨야 하고,
> `DOSE_EXCEEDS_DAILY_BUDGET`은 정상 거부가 아니라 설정 오류로 다뤄야 합니다.**

### 런타임 의존성 없음

포레스트는 오프라인에서 scikit-learn으로 학습한 뒤 순수 배열 JSON으로 내보냅니다.
추론기(`forest.py`)는 numpy도 scikit-learn도 import하지 않으므로 Orange Pi의 의존성은
`pyserial` 하나로 유지됩니다. 25트리·깊이 7 기준 아티팩트 273 KiB, 로드 7.6 ms,
판정 0.013 ms(개발 PC 실측).

### 데이터 수집 (`edge/arduino` dataset_logger + `tools/capture_dataset.py`)

`src/dataset_logger.cpp`는 **배포 펌웨어가 아닙니다.** JSON Lines 계약 대신 CSV를
내보내고, 시리얼 명령을 받고, 액추에이터 인터록이 전혀 없습니다. PlatformIO의 기본
환경에서 제외되어 있어 명시적으로만 빌드됩니다.

```bash
cd edge/arduino && pio run -e dataset_logger -t upload

cd edge/pi
python tools/capture_dataset.py --port /dev/ttyUSB0 --output data/raw/pot-01.csv
```

물을 줄 때마다 stdin에 `w30`(30 mL)을 입력하십시오. **이 관수 이벤트가 라벨의
유일한 출처이며**, 없으면 캡처는 그냥 센서 기록일 뿐입니다. Arduino에 RTC가 없어
벽시계 시각은 호스트가 붙입니다.

### 가짜 캡처 (실측 데이터 확보 전까지)

```bash
python tools/make_bench_capture.py --pots 4 --days 45
```

`data/`는 git-ignored이고 생성 파일 첫 줄에 `# SYNTHETIC` 배너가 박힙니다.
**측정값이 아니므로 수집한 데이터로 보고하면 안 됩니다.** 화분마다 배지 건조 속도,
흡수율, 관수 임계값, 사용자 습관, 광주기를 다르게 뽑습니다.

### 재학습

```bash
python -m venv .venv
.venv/bin/pip install -r tools/requirements-train.txt
.venv/bin/python tools/train_irrigation_rf.py                    # CPU
.venv/bin/python tools/train_irrigation_rf.py --backend xgboost --device cuda
```

`data/raw/*.csv`를 자동으로 읽습니다. 라벨은 **"운영자가 6시간 안에 물을 줬는가"**로,
공식이 아니라 사람의 결정입니다. 캡처가 하나도 없으면 물수지 생성기로 폴백하는데,
그 경우 **라벨이 곧 공식이라 포레스트는 넘겨받은 방정식을 재발견할 뿐**이며 경고를
출력합니다.

평가 분할은 절대 무작위가 아닙니다. 1분 간격 행은 서로 거의 같은 값이라 셔플하면
이웃 행이 반대편에 들어가 배포에서 재현 불가능한 정확도가 나옵니다. 화분이 여러 개면
**마지막 화분을 통째로 홀드아웃**하고, 하나뿐이면 시간순 75/25로 자릅니다.

백엔드 두 가지 모두 동일한 순수 파이썬 아티팩트를 내보내므로 Orange Pi 런타임은
어느 쪽으로 학습했든 달라지지 않습니다.

- `sklearn` (기본, CPU) — 리프가 확률, `aggregation: mean_probability`
- `xgboost` (`--device cuda`로 NVIDIA GPU) — 리프가 raw margin,
  `aggregation: sum_logit`. RAPIDS cuML 대신 고른 이유는 Windows 네이티브 pip로
  설치되기 때문입니다(cuML은 WSL2 필요).

내보내기 단계는 학습 표본을 런타임 추론기로 재채점해 학습기와 확률이 일치하는지
검사하고, 불일치하면 아티팩트를 쓰지 않습니다(train/serve skew 차단).

## 화분 여러 개 연결하기

게이트웨이 하나가 아두이노를 최대 4대까지 받습니다. 포트마다 읽기 스레드가 하나씩 돌고,
어느 화분의 측정값인지는 **아두이노가 보내는 `node_id`로 판별합니다.** 포트 순서가 아닙니다.
따라서 케이블을 다른 소켓으로 옮겨도 데이터 귀속은 바뀌지 않습니다.

아두이노마다 `TelemetryConfig.local.h`의 `TB_NODE_ID`를 **서로 다르게** 굽고,
그 값들을 `TB_EXPECTED_NODE_IDS`에 나열하세요.

### 포트 경로 고르기 — CH340이면 `by-path`를 쓰세요

`/dev/ttyUSB0` 같은 이름은 부팅 순서에 따라 바뀌므로 쓰지 않습니다. 안정적인 심볼릭 링크는
두 종류가 있고, **USB-시리얼 칩에 따라 선택이 갈립니다.**

| 경로 | 조건 | 주의 |
|---|---|---|
| `/dev/serial/by-id/` | 어댑터에 고유 시리얼 번호가 있을 때만 | CH340(`1a86`)에는 없습니다 |
| `/dev/serial/by-path/` | 항상 유일 (물리 USB 포트 기준) | 케이블을 다른 소켓에 꽂으면 경로가 바뀝니다 |

이름만 보면 구분됩니다. `usb-1a86_USB_Serial-if00-port0`에는 시리얼 번호가 없고,
`usb-Arduino_Uno_A1B2C3-if00`에는 있습니다. **시리얼 번호가 없는 어댑터를 두 개 이상 꽂으면
`by-id` 이름이 서로 겹쳐 한쪽만 살아남고 나머지 화분이 조용히 사라집니다.**

```bash
ls -l /dev/serial/by-id/ /dev/serial/by-path/
```

### 같은 펌웨어를 두 대에 구웠을 때

가장 흔한 실수입니다. 같은 `node_id`가 두 포트에서 보이면 두 번째 포트를 `중복 노드` 오류로
표시하고 그 측정값을 버립니다. 이걸 잡지 않으면 서로 다른 화분의 값이 한 이력에 섞여 들어가고도
그럴듯해 보입니다. 화면에서 바로 확인할 수 있습니다.

## 모니터 상태판

모니터를 연결하면 화분 4개의 상태와 6자리 등록 번호가 전체화면으로 보입니다.

```bash
# 수동 실행
python -m terrabyte_edge dashboard
python -m terrabyte_edge dashboard --windowed     # 개발용 창 모드
```

부팅 시 자동 실행하려면 데스크톱 자동시작에 등록합니다.

```bash
sudo apt install python3-tk
sudo cp deploy/terrabyte-dashboard.desktop /etc/xdg/autostart/
```

브릿지가 `/run/terrabyte-edge/status.json`에 1초마다 상태를 쓰고 상태판이 그걸 읽습니다.
두 프로세스는 완전히 분리되어 있어 **상태판이 죽어도 텔레메트리는 영향받지 않습니다.**
스냅샷이 8초 이상 낡으면 상태판이 "브리지 서비스 응답 없음"을 띄웁니다 — 죽은 값을 살아 있는
것처럼 보여주지 않기 위해서입니다.

> 텍스트 콘솔(tty)이 아니라 데스크톱 세션 안에서 돕니다. 리눅스 콘솔 폰트는 글리프 512개가
> 상한이라 한글이 렌더링되지 않고, Orange Pi 이미지는 `graphical.target`으로 부팅해
> lightdm이 이미 tty1을 쓰고 있습니다.

## 최초 설정 마법사

처음 켠 게이트웨이는 상태판보다 먼저 설정 마법사를 전체화면으로 띄웁니다.
네 단계이며, 각 단계는 앞 단계가 끝나야 넘어갑니다.

1. **아두이노 확인** — 기대하는 `node_id`가 실제로 들어오는지 봅니다.
2. **와이파이** — 주변 SSID를 훑고 비밀번호를 받아 접속합니다.
3. **신원 확인** — 공장 매니페스트와 백엔드가 아는 값이 같은지 확인합니다.
4. **등록 번호** — 앱에서 입력할 6자리 번호를 보여줍니다.

```bash
# 수동 실행
python -m terrabyte_edge wizard
python -m terrabyte_edge wizard --windowed     # 개발용 창 모드
```

### 설치

```bash
sudo cp deploy/terrabyte-wizard.desktop /etc/xdg/autostart/
sudo cp deploy/50-terrabyte-network.rules /etc/polkit-1/rules.d/
sudo install -d -m 0755 /etc/terrabyte-edge
sudo install -o root -g root -m 0444 deploy/provisioning.json.example \
  /etc/terrabyte-edge/provisioning.json
sudoedit /etc/terrabyte-edge/provisioning.json
```

매니페스트는 **root 소유 0444**입니다. 게이트웨이 프로세스도 사용자도 고쳐 쓸 수
없어야 신원 확인이 의미가 있습니다 — 스스로 고칠 수 있는 값과 비교하는 검사는
검사가 아닙니다.

| 필드 | 뜻 |
|---|---|
| `device_id` | 게이트웨이 식별자. MQTT 토픽의 `{gatewayId}`와 같습니다 |
| `claim_code` | 앱에서 입력하는 6자리 등록 번호 |
| `mqtt_username` | 브로커 계정. ACL이 이 계정을 `device_id` 네임스페이스에 묶습니다 |
| `provisioned_at` | 공장에서 이미지를 구운 UTC 시각. 진단용이며 비교에는 쓰지 않습니다 |

polkit 규칙은 와이파이 단계에서 비밀번호 창이 뜨지 않게 합니다. NetworkManager의
`network-control`이 기본 `auth`라서, 규칙이 없으면 앞에 서 있는 사용자가 입력할 수
없는 관리자 비밀번호를 요구하며 설정이 그 자리에서 멈춥니다. 대신 **로컬
`netdev` 구성원 누구나 이 보드의 네트워크를 인증 없이 바꿀 수 있게 됩니다.**
단일 목적 기기라 받아들이는 위험이며, 공용 머신에는 이 파일을 넣지 마세요.

### 신원 확인이 필요한 이유

SD 카드 이미지를 복제해 두 번째 보드를 만들면 매니페스트의 등록 번호까지 그대로
따라옵니다. 확인이 없으면 새 보드가 **다른 게이트웨이의 등록 번호**를 화면에
띄우고, 사용자는 그 번호로 남의 기기를 자기 계정에 등록합니다. 이후 들어오는
측정값은 전부 엉뚱한 화분에 붙고, 값 자체는 정상으로 보이기 때문에 아무도
알아차리지 못합니다.

불일치면 마법사는 빨간 화면에서 멈추고 **완료 표시 파일을 쓰지 않습니다.** 다음
부팅에서 다시 뜨므로, 잘못된 번호가 화면에 남는 상태로 넘어갈 수 없습니다.

### 오프라인일 때

백엔드에 닿지 못하면 `failed`가 아니라 `unverified`입니다. 노란 안내를 함께
띄우되 **등록 번호는 그대로 보여줍니다.** 복제 이미지의 위험보다 행사장 와이파이가
죽었다는 이유로 시연 자체가 막히는 쪽이 더 나쁘기 때문입니다. 이때도 완료 표시는
남으므로, 나중에 문제가 의심되면 아래 절차로 다시 돌립니다.

### 다시 실행하기

```bash
sudo rm /var/lib/terrabyte-edge/setup-complete
sudo reboot
```

재부팅 없이 확인만 할 때는 데스크톱 세션에서 직접 실행합니다.

```bash
python -m terrabyte_edge wizard --windowed
```

### 와이파이 단계 수동 확인

**SSH로는 검증할 수 없습니다.** 다른 네트워크에 붙는 순간 SSH 세션이 끊기고,
그러면 실패인지 성공했는데 연결만 바뀐 것인지 구분할 수 없습니다. 모니터와
키보드를 보드에 직접 연결하고 확인하세요.

1. 유선을 뽑고 부팅해 마법사가 와이파이 단계에서 멈추는지 본다.
2. 목록에 주변 SSID가 뜨는지 (스캔 권한 확인).
3. 비밀번호를 넣고 접속할 때 **polkit 비밀번호 창이 뜨지 않는지** — 뜨면 규칙이
   적용되지 않은 것이다. `pkaction --action-id
   org.freedesktop.NetworkManager.network-control --verbose`로 확인한다.
4. 틀린 비밀번호를 넣어 실패 메시지가 뜨고 같은 단계에 머무는지.
5. 접속 후 `nmcli connection show` 결과에 저장된 연결이 남는지 (`autoconnect`가
   yes여야 정전 후 사람 없이 복구된다).
6. 전원을 뺐다 켜서 자동으로 같은 SSID에 붙고, 마법사가 다시 뜨지 않는지.

## 테스트

테스트에는 외부 서버, serial 장치, pyserial, 화면이 필요하지 않습니다.
표시 로직은 순수 함수로 분리되어 있어 Tk 없이 검증합니다.

```bash
cd edge/pi
python -m unittest discover -s tests -v
```

### 소프트웨어 아두이노 (`terrabyte_edge/loopback.py`)

실물 펌프·릴레이·펌웨어 없이 명령 계약의 시리얼 쪽을 말합니다. `{"t":"cmd"}`를
받아 `{"t":"ack"}`를 돌려주므로 백엔드 → MQTT → 파이 → 시리얼 → ack → MQTT →
백엔드 **전 구간을 물 없이** 돌릴 수 있습니다.

```python
from terrabyte_edge.loopback import LoopbackArduino
from terrabyte_edge.serial_reader import SerialLineReader

arduino = LoopbackArduino(node_id="terrabyte-node-01")
reader = SerialLineReader(..., factory=arduino.as_factory())
```

**환경변수나 CLI 플래그로 켜는 스위치는 의도적으로 없습니다.** 루프백으로 설정될
수 있는 게이트웨이는 오타 하나에 **측정값과 구분되지 않는 가짜 센서값을 발행**하게
됩니다. 코드에서 명시적으로 조립할 때만 쓰입니다.

모델링하는 것: `accepted` → `completed` 2단(중간 상태가 실제로 관측되게), 명령 ID
링버퍼 8개(QoS1 중복이 두 홉 모두에 있으므로 필수), G1 절대 최대 구동시간 클램프
(`stop:"max_runtime"`). 그 외 쿨다운 타이밍·워치독 발동은 `respond` 훅으로
주입합니다 — 인터록을 여기서 두 번째로 구현하면 펌웨어와 어긋나고, **펌웨어와
불일치하는 테스트 더블은 없는 것보다 나쁩니다.**
