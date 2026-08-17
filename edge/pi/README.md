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
tb/v2/{gatewayId}/dn/command      서버 → 게이트웨이     QoS 1
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

프로필 두 가지: `EnvelopeLimits.supervised(substrate_volume_ml=...)`(기본, 건조
게이트 45%·최소 간격 6시간)와 클라우드 장애용 `EnvelopeLimits.autonomous()`
(`D16` 기준, 15%·12시간).

모델 아티팩트가 없거나 스키마가 어긋나면 `ModelError`가 발생하고, 판정은
`MODEL_UNAVAILABLE`로 **관수하지 않는 쪽**으로 떨어집니다.

판정은 `BridgeService`의 ingest 경로에서 매 측정마다 돌지만 **아무것도 구동하지
않습니다.** 파이에서 펌프로 가는 경로가 아직 없고, 결과는 `latest_decision(node_id)`로
남아 명령 중계와 자율 상태기계가 소비합니다. 판정 경로의 어떤 실패도 텔레메트리를
막지 않습니다 — 게이트웨이의 첫 번째 임무는 관측 전달이고, 관수 판정의 고장은
측정값이 아니라 판정 한 건을 잃는 것으로 끝나야 합니다.

### 일일 예산은 화분 용적에서 유도합니다

```
일일 예산 = min(max(기준 관수량 x 하루 허용 횟수, 200), 600)   # mL
기준 관수량 = 서버 폴백표(§3.2): ~1 L 40 / 1–3 L 80 / 3–6 L 120 / 6 L~ 160
             화분 용적 미설정이면 FIXED_VOLUME_ML(30)
하루 허용 횟수 = floor(24 / min_interval_hours)                # 6시간이면 4회
```

| 화분 용적 | 일일 예산 |
|---|---|
| 미설정 | 200 mL |
| 1 L | 200 mL |
| 3 L | 320 mL |
| 6 L | 480 mL |
| 12 L~ | 600 mL |

**규칙은 옛 상수를 만든 것과 같고, 틀렸던 입력만 바꿨습니다.** 120 mL는 고정 30 mL
관수량의 4회분이었고 `min_interval_hours=6`이 이미 하루를 4회로 제한하므로,
예산은 간격의 재진술이었을 뿐 **한 번도 발동할 수 없었습니다.** 변동량이 그 우연을
깨뜨립니다 — 3 L lettuce 화분은 토양수분 12%에서 **390 mL**를 요구합니다.

상·하한은 둘 다 서버 상수이고 각각 중간 항의 실패를 하나씩 막습니다.

- **하한 200 mL** = Governor의 1회 관수량 상한(`doseMaxMl`). **1회분보다 작은
  일일 예산은 예산이 아니라 금지입니다** — 화분은 영원히 관수되지 않고 로그에는
  "예산 소진"이 남습니다. 1 L 화분은 표에서 160 mL가 나오는데 자기 수식이 12%에서
  159 mL를 요구하므로, 측정 한 번이면 그 상태가 됩니다.
- **상한 600 mL** = Governor의 화분당 일일 예산(`dailyBudgetMl`). 엣지는 서버가
  허용한 범위를 넓힐 수 없습니다(`D16`/`D17`). 40 L 화분은 표대로면 640 mL이므로
  여기서 잘립니다.

유도가 사주는 것: **예산이 이제 횟수가 아니라 부피를 잰다.** 200 mL를 요구하는
화분은 하루 1회, 60 mL를 요구하는 화분은 여러 번 — 어느 쪽도 횟수를 세서 정하지
않습니다.

봉투에 다는 관수량은 **서버가 실제로 승인할 수 있는 양**입니다. 제안값 자체는
클램프하지 않고 그대로 백엔드로 보내지만(§3.2 — 깎아 보내면 수식과 시스템 한계의
불일치가 보이지 않게 됩니다), Governor가 모든 승인을 200 mL로 클램프하므로 390 mL를
예산에 달면 **평범하게 건조한 화분이 설정 오류 판정(`DOSE_EXCEEDS_DAILY_BUDGET`)으로
거절됩니다.** 게이트에 넘기는 값만 200 mL로 제한하고, 그 사실을 로그에 남깁니다.

그 결과 `DOSE_EXCEEDS_DAILY_BUDGET`은 유도된 예산에서는 발동하지 않습니다. 그것이
의도입니다 — 이 판정은 설정 오류를 뜻하고, 유도된 예산은 설정 오류가 아닙니다.
손으로 만든 `EnvelopeLimits`에서는 여전히 발동합니다.

`autonomous()`의 120 mL는 **유도하지 않습니다.** `D16`이 긴급 관수량을 60 mL,
간격을 12시간으로 못박았으므로 120은 2회분이고, 설정으로 위조할 수 없게 하드코딩된
설계값입니다. 유도하면 아무도 보지 않는 동안 봉투가 **넓어집니다.** 대신 이 프로필은
**고정 60 mL와 함께 써야 합니다** — 변동 관수량은 120 mL를 예사로 넘고, 그러면 긴급
상황에서 `DOSE_EXCEEDS_DAILY_BUDGET`으로 물을 주지 않습니다. 자율 상태기계를 쓰는
쪽이 이 짝을 책임집니다.

### 관수 이력 (`terrabyte_edge/irrigation_history.py`)

봉투의 최소 간격·일일 예산 게이트와 수식의 재분배 항이 모두 여기서 답을 받습니다.
그 전까지 `service.py`가 `hours_since_last_irrigation=None`으로 고정하고 있어서
**두 게이트가 성립하지 않았습니다.**

outbox와 **같은 SQLite 파일의 별도 테이블**(`irrigation_events`)입니다. 저장·전달
큐와 관수 기록이 한 fsync 도메인에 있어야 정전이 "ack는 남고 관수량은 없는" 상태를
만들지 못합니다. 마이그레이션 프레임워크가 없으므로 새 테이블에는
`CREATE TABLE IF NOT EXISTS`가 전부이고, 나중에 **컬럼**을 더할 때는
`Outbox._migrate`(`PRAGMA table_info` → `ALTER TABLE`)를 따라야 합니다.

질의 두 가지 — `hours_since_last_irrigation(node_id)`(기록 없으면 `None`),
`dispensed_today_ml(node_id)`. "오늘"은 **롤링 24시간**이며 로컬 자정 기준이
아닙니다. 서버 `budgetWindow()`가 롤링 24시간이라 맞춰야 하고(자정 리셋은 23:55와
00:05에 각각 만액을 허용해 서버라면 거절한 2일치를 10분 안에 내보냅니다), 자정
경계는 정확한 타임존을 요구하는데 게이트웨이는 그것 없이 부팅합니다.

**relay의 private 필드가 아니라 조회 가능한 이벤트 로그입니다.** 기록하는 쪽이
셋입니다 — 클라우드 명령의 ack, 엣지 자율 긴급 관수, 벤치 수동 시험. 셋 다 실제로
물을 옮기고 다음 판정에 보여야 합니다. `source` 컬럼에 출처를 남기지만 예산에는
**모두 합산**됩니다.

**기록하는 것은 전달이지 의도가 아닙니다.** IRRIGATE 판정은 이력이 아니고, 하류가
실제로 보고한 배출만 이력입니다. 판정을 기록하는 쪽이 쉽고 "안전 측"으로도 보이지만
(예산을 과다 계상하면 물을 덜 주니까), `hours_since_last_irrigation`이 오염되고
**한 시간 전에 물을 받았다고 들은 모델은 바싹 마른 화분에 계속 억제를 겁니다.**
지어낸 이력은 첫 판정만 안전하고 그 뒤로는 계속 위험합니다.

같은 `command_id`는 두 번 계상되지 않습니다(부분 unique 인덱스). QoS 1 중복이 두 홉
모두에 있고 아두이노 링버퍼는 8개만 기억하므로 같은 ack가 다시 올 수 있는데,
이중 계상은 **받지 않은 물을 받을 수 있는 양에서 빼는 것**입니다.

이력을 읽지 못하면(`sqlite3.Error`) **판정하지 않습니다.** 빈 로그와 못 읽은 로그는
구분해야 합니다 — 빈 로그는 최소 간격을 통과하고 예산에 0을 더하므로, DB 오류를
"기록 없음"으로 읽으면 게이트 둘이 동시에 열립니다.

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
