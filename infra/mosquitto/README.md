# Mosquitto (개발용 MQTT 브로커)

Orange Pi 게이트웨이 ↔ Spring 백엔드 통신을 HTTP 대신 MQTT로 처리하기 위한
로컬 개발용 브로커 설정이다. 계약 원본: `docs/design/device_model_and_telemetry_contract.md` §6.3, §6.4.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `mosquitto.conf` | 브로커 설정. 익명 접속 차단, 패스워드/ACL 파일 위치, 영속화 경로, TLS conf.d 포함(`include_dir`) 지정. |
| `aclfile` | 게이트웨이별 토픽 write/read 권한. 토픽 위조를 프로토콜 레벨에서 막는 핵심 파일 — 파일 안 주석 참고. |
| `passwd` | 해시된 사용자 패스워드 파일 (mosquitto 표준 포맷). **평문 비밀번호는 들어있지 않다.** |
| `generate-passwd.sh` | `passwd` 파일을 재생성하는 스크립트. |
| `docker-entrypoint.sh` | 컨테이너 기동 스크립트. TLS 인증서 유무로 8883 리스너를 켜거나 끄고, passwd/aclfile 권한을 0600으로 좁힌다. |
| `generate-certs.sh` | 자체 서명 CA + mosquitto 서버 인증서를 `certs/` 아래 생성하는 스크립트 (TLS용). |
| `tls.conf.template` | 8883 TLS 리스너 설정 조각. 인증서가 있을 때만 `docker-entrypoint.sh` 가 활성 경로로 복사한다. |
| `certs/` | `generate-certs.sh` 산출물 (CA/서버 인증서·키). **git-ignore 대상 — 저장소에 없다.** |

## 토픽 구조

```
tb/v2/{gatewayId}/up/telemetry     게이트웨이 → 서버   QoS 1
tb/v2/{gatewayId}/up/status        온라인 상태 (LWT 등록, retain)
tb/v2/{gatewayId}/up/ack           명령 응답            QoS 1
tb/v2/{gatewayId}/dn/command       서버 → 게이트웨이     QoS 1
tb/v2/{gatewayId}/dn/heartbeat     서버 → 게이트웨이 생존 확인
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

## `aclfile`/`passwd` 권한 (world-readable 경고)

mosquitto 2.x는 `aclfile`/`passwd`가 world-readable이면 경고를 내고, 향후
버전은 로드 자체를 거부할 예정이다. 저장소에는 이 파일들이 0644로 커밋돼
있는데 (git은 실행 비트만 추적하고 0600 여부는 추적하지 않는다), 이 파일들은
`docker-compose.yml`에서 `:ro`로 바인드 마운트되므로 컨테이너 안에서
`chmod`로 고칠 수도 없다(호스트 파일과 같은 inode라 Read-only file system
에러가 난다). 그래서 `docker-entrypoint.sh`가 컨테이너 기동 시 이 파일들을
쓰기 가능한 런타임 볼륨으로 복사하면서 0600으로 좁히고, mosquitto는 그
복사본을 읽는다. 자세한 내용은 `mosquitto.conf`와 `docker-entrypoint.sh`의
주석 참고.

## TLS(8883) 사용법

기본은 지금처럼 1883 평문 리스너다 — Orange Pi 게이트웨이가 이미 여기 붙어 있고
이 경로를 깨면 안 되므로, TLS는 **추가** 리스너로만 존재한다. 인증서를 만들지
않으면 8883은 그냥 열리지 않고 1883만으로 정상 기동한다.

### 1. 인증서 생성

```bash
cd infra/mosquitto
./generate-certs.sh                       # SAN 자동 감지(mosquitto, localhost, 호스트 LAN IP)만 사용
./generate-certs.sh 192.168.0.10 my-host  # 필요하면 SAN을 인자로 더 추가
```

`certs/ca.crt`, `certs/server.crt`, `certs/server.key` 가 생긴다. **개인키
(`*.key`)는 절대 커밋하지 않는다** — `certs/` 디렉터리 전체가 `.gitignore` 대상이다.

인증서의 SAN(Subject Alternative Name)에는 반드시 실제로 접속하는 이름/IP가
들어가야 한다. 컨테이너 안의 백엔드는 `mosquitto`(컴포즈 서비스명)로 붙고,
같은 LAN의 Orange Pi 게이트웨이는 이 macOS/Linux 호스트의 IP로 붙는다.
`CN=localhost` 짜리 인증서는 게이트웨이 입장에서 hostname mismatch로
핸드셰이크가 항상 실패하므로 아무 쓸모가 없다 — 그래서 스크립트가 호스트
LAN IP를 자동 감지해 SAN에 넣는다.

### 2. 브로커에 반영

```bash
docker compose restart mosquitto
```

`docker-entrypoint.sh` 가 재시작 시점에 `certs/` 의 인증서를 다시 확인하고
8883 리스너를 켠다. 로그에서 다음 줄로 확인할 수 있다.

```
[mosquitto entrypoint] TLS 인증서 확인됨 — 8883 리스너를 켭니다.
```

### 3. 접속 확인

```bash
# 평문 (기존과 동일)
mosquitto_sub -h localhost -p 1883 -u terrabyte-backend -P terrabyte-backend-local \
  -t 'tb/v2/+/up/telemetry' -E

# TLS
mosquitto_sub -h localhost -p 8883 -u terrabyte-backend -P terrabyte-backend-local \
  -t 'tb/v2/+/up/telemetry' -E --cafile infra/mosquitto/certs/ca.crt
```

### 4. 게이트웨이(Orange Pi)를 TLS로 돌리기

게이트웨이 쪽 환경 변수(예상 표기, 실제 게이트웨이 구현이 정의):

```
TB_MQTT_TLS=true
TB_MQTT_PORT=8883
```

게이트웨이는 브로커의 `ca.crt`를 신뢰 저장소에 넣어야 한다 — 자체 서명
인증서라 시스템 CA 목록에 없기 때문이다. `certs/ca.crt`를 게이트웨이로
복사해 전달한다.

## 운영 환경 인증서 계획 (production)

여기 있는 `generate-certs.sh`는 **데모/로컬 개발 전용**이다. 팀이 스스로 CA
행세를 하는 자체 서명 인증서이므로, 운영 배포에서는 다음이 달라져야 한다.

- **진짜 CA를 쓴다.** Let's Encrypt(도메인이 있는 경우) 또는 사내/클라우드
  프로바이더가 제공하는 사설 CA(AWS IoT Core, Azure IoT Hub 등)로 발급받는다.
  자체 서명 CA를 운영에 그대로 쓰면 모든 클라이언트가 그 CA를 신뢰 저장소에
  수동으로 넣어야 하고, 잃어버리면 전체 신뢰 체계가 깨진다.
- **개인키가 저장소에 닿지 않는다.** 지금은 로컬 파일(`certs/*.key`)이지만,
  운영에서는 비밀 관리 시스템(Vault, AWS Secrets Manager, 클라우드 KMS 등)이나
  최소한 배포 파이프라인 전용 시크릿 스토어에만 존재해야 하고, git-ignore 는
  최소한의 방어선일 뿐 근본 대책이 아니다.
- **회전(rotation) 절차가 있어야 한다.** 자체 서명 인증서는 유효기간을
  넉넉히(825일) 잡아 데모 기간에 갱신을 신경 쓰지 않게 했지만, 운영 인증서는
  통상 90일(Let's Encrypt) ~ 1년 주기로 자동 갱신되어야 하고, 갱신 후
  브로커 재시작(또는 무중단 리로드)까지 파이프라인화되어야 한다. 게이트웨이가
  많아지면 각 게이트웨이가 새 CA를 받아들이는 절차도 함께 자동화해야 한다.
- **호스트명이 안정적이어야 한다.** 지금은 LAN IP를 SAN에 자동 감지해
  넣지만, IP는 DHCP로 바뀔 수 있다. 운영에서는 고정 도메인/DNS 레코드를
  SAN에 넣고 IP 직접 지정은 피한다.
- **모니터링.** 인증서 만료를 알람으로 잡아야 한다 — mosquitto는 만료된
  인증서로도 일단 뜨지만(재시작 전까지는 기존 연결이 끊기지 않을 수 있음)
  신규 TLS 핸드셰이크는 만료 이후 클라이언트 쪽에서 실패하기 시작한다.

## 왜 ACL이 핵심인가

`aclfile` 덕분에 각 게이트웨이 계정은 자신의 `gatewayId` 이름공간 밖으로 쓰거나
읽을 수 없다. 즉 어떤 게이트웨이도 다른 게이트웨이인 척 토픽을 위조할 수 없다.
그 결과 백엔드는 MQTT 토픽에 담긴 `gatewayId` 를 그대로 신뢰해도 되고, 기존에
모든 게이트웨이가 공유하던 `X-Device-Key` 검증 로직
(`MeasurementService.authenticateDevice`)을 제거할 수 있다. 자세한 내용은
`aclfile` 상단 주석과 계약 문서 §6.4 참고.
