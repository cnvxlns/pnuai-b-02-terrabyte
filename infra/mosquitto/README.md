# Mosquitto (개발용 MQTT 브로커)

Orange Pi 게이트웨이 ↔ Spring 백엔드 통신을 HTTP 대신 MQTT로 처리하기 위한
로컬 개발용 브로커 설정이다. 계약 원본: `docs/design/device_model_and_telemetry_contract.md` §6.3, §6.4.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `mosquitto.conf` | 브로커 설정. 익명 접속 차단, 패스워드/ACL 파일 위치, 영속화 경로 지정. |
| `aclfile` | 게이트웨이별 토픽 write/read 권한. 토픽 위조를 프로토콜 레벨에서 막는 핵심 파일 — 파일 안 주석 참고. |
| `passwd` | 해시된 사용자 패스워드 파일 (mosquitto 표준 포맷). **평문 비밀번호는 들어있지 않다.** |
| `generate-passwd.sh` | `passwd` 파일을 재생성하는 스크립트. |

## 토픽 구조

```
tb/v2/{gatewayId}/up/telemetry     게이트웨이 → 서버   QoS 1
tb/v2/{gatewayId}/up/status        온라인 상태 + 링크 상태 (LWT 등록, retain)
tb/v2/{gatewayId}/up/ack           명령 응답            QoS 1
tb/v2/{gatewayId}/up/irrigation    클라우드 장애 중 자율 관수 기록  QoS 1
tb/v2/{gatewayId}/dn/command       서버 → 게이트웨이     QoS 1
tb/v2/{gatewayId}/dn/heartbeat     서버 → 게이트웨이 생존 확인      QoS 0
```

`up/status` 의 `state` 필드는 게이트웨이의 링크 상태(`CLOUD_ONLINE`, `RESYNC` 등)이며,
서버는 `RESYNC`/`SAFE_HOLD` 인 게이트웨이에 명령을 발행하지 않는다. 아직 서버가 받지 못한
자율 관수 기록이 남아 있는 동안 명령을 보내면, 이미 흙에 들어간 물을 모르는 예산으로 승인한
명령이 실행된다.

`up/irrigation` 은 ack 이 아니다. 서버가 발행한 명령이 없으므로 대응할 command_id 도 없고,
`device_command(origin=EDGE_FALLBACK, state=COMPLETED)` 로 기록되어 일일 예산 질의에 그대로
합산된다.

```
```

## 개발용 자격 증명 (dev-only, 비밀 아님)

아래 계정/비밀번호는 **로컬 개발 전용 기본값**이다. 운영 배포에서는 반드시 새로
발급하고, 이 값을 그대로 쓰지 않는다.

| 사용자 | 비밀번호 | 용도 |
|---|---|---|
| `terrabyte-backend` | `terrabyte-backend-local` | Spring 백엔드 — 모든 uplink 구독, 모든 downlink 발행 |
| `gw-orangepi-pro-01` | `gw-orangepi-pro-01-local` | 게이트웨이 1호기 |
| `gw-orangepi-pro-02` | `gw-orangepi-pro-02-local` | 게이트웨이 2호기 |

## `passwd` 파일은 어떻게 만들어졌나

`eclipse-mosquitto:2` 공식 이미지의 `mosquitto_passwd` 로 생성한 **진짜 해시**다
(SHA-512 기반 PBKDF2, mosquitto 기본 `-7` 포맷). 손으로 지어낸 값이 아니다.

재생성하려면 (Docker 필요):

```bash
cd infra/mosquitto
./generate-passwd.sh
```

내부적으로 다음을 실행한다:

```bash
docker run --rm -v "$(pwd)":/work -w /work eclipse-mosquitto:2 sh -c '
  mosquitto_passwd -b passwd terrabyte-backend  terrabyte-backend-local &&
  mosquitto_passwd -b passwd gw-orangepi-pro-01 gw-orangepi-pro-01-local &&
  mosquitto_passwd -b passwd gw-orangepi-pro-02 gw-orangepi-pro-02-local
'
```

새 게이트웨이를 추가할 때는 `generate-passwd.sh` 에 계정을 추가하고 재실행한 뒤,
`aclfile` 에도 동일한 이름공간 규칙(`tb/v2/{gatewayId}/up/#` write,
`tb/v2/{gatewayId}/dn/#` read)으로 블록을 추가한다.

## 왜 ACL이 핵심인가

`aclfile` 덕분에 각 게이트웨이 계정은 자신의 `gatewayId` 이름공간 밖으로 쓰거나
읽을 수 없다. 즉 어떤 게이트웨이도 다른 게이트웨이인 척 토픽을 위조할 수 없다.
그 결과 백엔드는 MQTT 토픽에 담긴 `gatewayId` 를 그대로 신뢰해도 되고, 기존에
모든 게이트웨이가 공유하던 `X-Device-Key` 검증 로직
(`MeasurementService.authenticateDevice`)을 제거할 수 있다. 자세한 내용은
`aclfile` 상단 주석과 계약 문서 §6.4 참고.
