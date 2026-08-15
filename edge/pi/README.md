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
