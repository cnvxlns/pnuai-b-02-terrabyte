# TerraByte Orange Pi telemetry bridge

이 프로그램은 Arduino의 USB serial JSON Lines를 검증하고 수신 시각을 UTC로
기록한 뒤 SQLite outbox에 먼저 저장합니다. 백엔드가 끊겨도 데이터가 남으며,
연결이 복구되면 순서대로 재전송합니다.

## 통신 계약

Arduino는 `115200 baud`에서 한 줄에 JSON 객체 하나와 `\n`을 보냅니다.
대기 온도와 상대습도가 유효하면 다음 `telemetry` 메시지를 보냅니다. PPFD를
읽을 수 없을 때는 값을 꾸미지 않고 명시적 `null`로 보냅니다.

```json
{"message_type":"telemetry","protocol_version":1,"node_id":"terrabyte-node-01","sequence":42,"uptime_ms":123456,"air_temperature_c":24.5,"relative_humidity_pct":61.2,"ppfd_umol_m2_s":null}
```

`hello`와 `sensor_status` 메시지는 상태 확인용으로만 기록하며 백엔드로 보내지
않습니다. 온도 `-50..80 °C`, 상대습도 `0..100 %`를 검증하고 PPFD는
명시적 `null` 또는 `0..5000 umol·m⁻²·s⁻¹` 숫자를 허용합니다. PPFD 키
자체가 누락된 메시지는 계약 오류로 폐기합니다.
`sequence`와 `uptime_ms`는 Arduino 재부팅 때 0부터 다시 시작하고 uint32로
wrap될 수 있으므로 영구 식별자로 사용하지 않습니다. Orange Pi가 각 수신 건에
별도 UUID를 부여하며 이 UUID와 수신 UTC 시각은 재전송 중에도 바뀌지 않습니다.

백엔드 전송은 저장소의 handoff 계약을 따릅니다.

```text
POST /api/crop-contexts/{contextId}/environment-observations
Authorization: Bearer <device token>
X-Device-ID: <gateway id>
X-Arduino-Node-ID: <provisioned Arduino node id>
Idempotency-Key: <outbox event UUID>
```

요청 본문은 `capturedAtUtc`, `airTemperatureC`, `relativeHumidityPct`,
`ppfdUmolM2S`, `inputContract=perfect_calibrated_v1`만 포함합니다. `201`과
`DUPLICATE_OBSERVATION`인 `409`는 전달 완료로 처리합니다. 네트워크 오류,
`401`, `403`, `404`, `408`, `425`, `429`, `5xx`는 설정·provisioning 또는
서버 장애가 복구될 수 있으므로 지수 백오프로 재시도합니다. 이때 오래된
이벤트가 성공하기 전에는 뒤의 이벤트를 보내지 않아 수집 순서를 보존합니다.
관측값 자체가 잘못된 나머지 `4xx`는 계속 재시도해 서버를 압박하지 않도록
SQLite의 `dead` 상태로 격리합니다.

현재 source of truth가 단건 endpoint만 정의하므로 HTTP 요청도 단건입니다.
outbox 조회와 HTTP transport가 분리되어 있어 백엔드가 공식 batch endpoint를
정의하면 저장 형식과 serial 수집부를 바꾸지 않고 transport만 확장할 수 있습니다.

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

필수 환경 변수는 `TB_SERIAL_PORT`, `TB_BACKEND_BASE_URL`,
`TB_CROP_CONTEXT_ID`, `TB_DEVICE_ID`, `TB_EXPECTED_NODE_ID`와 인증 토큰입니다.
serial의 `node_id`가 `TB_EXPECTED_NODE_ID`와 다르면 관측을 저장하지 않습니다.
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

## 테스트

테스트에는 외부 서버, serial 장치, pyserial이 필요하지 않습니다.

```bash
cd edge/pi
python -m unittest discover -s tests -v
```
