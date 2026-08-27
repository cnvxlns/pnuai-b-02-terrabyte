# 백엔드 가이드

## 기술 스택

- Java 17
- Spring Boot 3.5.16
- Gradle 8.14.3 Wrapper
- PostgreSQL: 사용자, 기기 등 업무 데이터
- SQLite: 작물별 환경 점수 프로필 및 계산 결과
- InfluxDB 2.x: 하드웨어 센서 시계열 데이터

## Docker 로 실행 (권장)

저장소 루트에서 `docker compose up --build` 한 번이면 PostgreSQL·InfluxDB·백엔드·프론트엔드가 함께 뜹니다.
JDK/Gradle 버전은 컨테이너에 고정되어 있고, 원격 디버깅 포트(5005)도 열려 있습니다.
자세한 내용은 [`docs/docker_dev_environment.md`](../docs/docker_dev_environment.md) 를 참고하세요.

```bash
cp .env.example .env
docker compose up --build        # 전체 스택
make test                        # 백엔드 테스트만 실행
```

## 로컬 실행 (호스트에 직접 설치)

JDK 17 이상과 실행 중인 PostgreSQL이 필요합니다. 기본 연결 정보는 아래와 같습니다.

```text
database: terrabyte
username: terrabyte
password: terrabyte
```

환경에 맞게 다음 값을 설정할 수 있습니다.

```bash
export POSTGRES_URL='jdbc:postgresql://localhost:5432/terrabyte'
export POSTGRES_USER='terrabyte'
export POSTGRES_PASSWORD='terrabyte'
export SQLITE_URL='jdbc:sqlite:./db/terrabyte-score.db'
export JWT_SECRET='32바이트 이상의 운영용 비밀키로 변경하세요'
export ADMIN_API_KEY='관리자 API용 별도 비밀키'
export INFLUX_URL='http://localhost:8086'
export INFLUX_TOKEN='InfluxDB API 토큰'
export INFLUX_ORG='terrabyte'
export INFLUX_BUCKET='telemetry'
export TELEMETRY_DEVICE_KEY='하드웨어가 X-Device-Key로 보낼 공유 키'
export GEMINI_ENABLED='true'
export GEMINI_API_KEY='Google AI Studio에서 발급한 Gemini API 키'
# 선택: 기본값은 gemini-3.5-flash-lite, 동일 측정값의 계획 캐시는 30분
export GEMINI_MODEL='gemini-3.5-flash-lite'
```

SQLite 점수 스키마와 마이그레이션은 애플리케이션이 시작될 때 자동으로 적용됩니다.
DB 파일이 비어 있으면 전체 스키마를 생성하고, 과거 bootstrap DB(기본 3개 테이블만 존재)는 데이터를 유지한 채 전체 스키마로 보완합니다. 지원되는 기존 스키마에는 마이그레이션을 실행하며, 그 밖의 불완전한 파일은 데이터 손실을 막기 위해 기동을 중단합니다.

애플리케이션을 실행합니다.

```bash
./gradlew bootRun
```

상태 확인 주소는 `http://localhost:8080/actuator/health`입니다.

## 로컬 통합 테스트 가이드

아래 계정과 키는 **로컬 개발 환경 전용**입니다. 운영·배포 환경에서는 같은 값을 사용하지 마세요.

### 1. 테스트 계정 및 기기

현재 팀 로컬 DB에 만들어 둔 프론트엔드 테스트 계정은 다음과 같습니다.

| 항목 | 값 |
| --- | --- |
| 이메일 | `demo@terrabyte.local` |
| 비밀번호 | `password1` |
| 등록 기기 코드 | `483920` |
| 하드웨어 ID | `orangepi-pro-01` |

이 계정은 Flyway가 자동 생성하는 계정이 아니므로 PostgreSQL을 새로 만든 경우에는 프론트엔드 회원가입 화면에서
같은 이메일과 비밀번호로 가입한 뒤 기기 코드 `483920`을 등록합니다. 기기 등록 화면에서는 공간명, 공간 유형,
면적도 함께 입력합니다. 이미 다른 사용자에게 등록된 기기라는 메시지가 나오면 기존 데모 계정으로 로그인하거나
아직 사용되지 않은 개발용 코드 `123456`을 사용합니다. `123456`의 하드웨어 ID는 `orangepi-pro-02`입니다.

### 2. InfluxDB 실행 및 로그인

로컬 InfluxDB 접속 정보는 다음과 같습니다.

| 항목 | 값 |
| --- | --- |
| 웹 UI | `http://localhost:8086` |
| 사용자명 | `terrabyte` |
| 비밀번호 | `terrabyte-admin-password` |
| Organization | `terrabyte` |
| Bucket | `telemetry` |
| API Token | `terrabyte-local-token` |
| 하드웨어 요청 키 | `terrabyte-local-device-key` |

기존 컨테이너가 있다면 다음 명령으로 실행합니다.

```bash
docker start terrabyte-influxdb
```

컨테이너가 아직 없다면 최초 한 번 다음과 같이 생성합니다.

```bash
docker run -d \
  --name terrabyte-influxdb \
  -p 8086:8086 \
  -v terrabyte-influxdb-data:/var/lib/influxdb2 \
  -e DOCKER_INFLUXDB_INIT_MODE=setup \
  -e DOCKER_INFLUXDB_INIT_USERNAME=terrabyte \
  -e DOCKER_INFLUXDB_INIT_PASSWORD=terrabyte-admin-password \
  -e DOCKER_INFLUXDB_INIT_ORG=terrabyte \
  -e DOCKER_INFLUXDB_INIT_BUCKET=telemetry \
  -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=terrabyte-local-token \
  influxdb:2.7
```

컨테이너와 서버 상태를 확인합니다.

```bash
docker ps --filter name=terrabyte-influxdb
curl http://localhost:8086/health
```

브라우저에서 `http://localhost:8086`을 연 뒤 위 사용자명과 비밀번호로 로그인하면
Data Explorer에서 `telemetry` 버킷에 저장된 센서 데이터를 확인할 수 있습니다.

### 3. 백엔드와 프론트엔드 실행

터미널 1에서 백엔드를 실행합니다.

```bash
cd backend
./gradlew bootRun
```

터미널 2에서 프론트엔드를 실행합니다.

```bash
cd frontend/app
npm install
npm run web
```

| 서비스 | 주소 |
| --- | --- |
| 프론트엔드 | `http://localhost:8081` |
| 백엔드 상태 확인 | `http://localhost:8080/actuator/health` |
| InfluxDB UI | `http://localhost:8086` |

Expo가 8081이 아닌 다른 포트를 안내하면 터미널에 출력된 주소로 접속합니다. 프론트엔드는 기본적으로
`.env.example`과 같이 `http://localhost:8080`의 백엔드 API를 사용합니다.

### 4. 테스트 센서 데이터 전송

백엔드와 InfluxDB가 실행 중인 상태에서 아래 요청을 보냅니다. 온도·습도·PPFD는 적합도 계산에 사용되고,
토양수분은 저장 및 모니터링만 됩니다.

```bash
observed_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

curl -X POST http://localhost:8080/api/telemetry \
  -H 'Content-Type: application/json' \
  -H 'X-Device-Key: terrabyte-local-device-key' \
  --data-binary @- <<JSON
  {
    "schema_version": 1,
    "event_type": "telemetry.sample",
    "device_id": "orangepi-pro-01",
    "observed_at": "$observed_at",
    "sequence": 1042,
    "context": {
      "site_id": "pnu-lab",
      "zone_id": "pot-01",
      "soil_type": "loam",
      "crop_type": "basil",
      "calibration_version": "soil-v2"
    },
    "measurements": {
      "soil_moisture_pct": 31.2,
      "soil_moisture_raw_adc": 1847,
      "air_temperature_c": 27.1,
      "air_humidity_pct": 58.0,
      "plant_light_ppfd_umol_m2_s": 230.5
    },
    "quality": {
      "soil_sensor_valid": true,
      "air_sensor_valid": true,
      "light_sensor_valid": true
    }
  }
JSON
```

성공하면 HTTP `202 Accepted`가 반환됩니다. 여러 건을 연속 전송할 때는 `sequence`도 새 값으로 변경하는 것이 좋습니다.

### 5. 화면에서 확인

1. `http://localhost:8081`에 접속합니다.
2. `demo@terrabyte.local` / `password1`로 로그인합니다.
3. 대시보드에서 온도·습도·PPFD·토양수분 최신값을 확인합니다.
4. 공간 진단 화면에서 온도·습도·PPFD 기반 종합 적합도를 확인합니다.
5. `적합도 계산식`을 누르면 항목별 사다리꼴 점수 기준과 기하평균 산식을 확인할 수 있습니다.
6. InfluxDB의 Data Explorer에서도 같은 측정값이 `telemetry` 버킷에 저장됐는지 확인합니다.

백엔드 자동 테스트는 외부 InfluxDB 없이 실행할 수 있습니다.

```bash
cd backend
./gradlew test
```

## Swagger API 문서

백엔드 실행 후 아래 주소에서 API 명세를 확인하고 요청을 직접 테스트할 수 있습니다.

| 구분 | 주소 |
| --- | --- |
| Swagger UI | `http://localhost:8080/swagger-ui.html` |
| OpenAPI JSON | `http://localhost:8080/v3/api-docs` |
| OpenAPI YAML | `http://localhost:8080/v3/api-docs.yaml` |

인증이 필요한 API는 Swagger UI 우측 상단의 **Authorize**에서 로그인 또는 회원가입 응답으로 받은 액세스 토큰을 입력한 뒤 테스트합니다.

## 인증 API

```text
POST  /api/auth/signup                              회원가입 및 액세스 토큰 발급 (공개)
POST  /api/auth/login                               로그인 및 액세스 토큰 발급 (공개)
GET   /api/me                                       현재 사용자 조회

GET   /api/spaces                                   내 재배 공간 목록 조회
POST  /api/spaces                                   재배 공간 등록

POST  /api/devices                                  기기 등록 또는 연결
GET   /api/devices                                  내 기기 목록 조회
GET   /api/devices/{deviceId}                       기기 상세 조회
GET   /api/devices/{deviceId}/sensors               기기별 센서 목록·상태 조회
POST  /api/devices/{deviceId}/pots                  기기에 화분 등록

GET   /api/pots                                     내 화분 목록 조회
GET   /api/pots/{potId}                             화분 상세 조회
PATCH /api/pots/{potId}                             화분 정보 수정

GET   /api/crops                                    작물 목록·검색 조회
PATCH /api/pots/{potId}/crop                        화분 재배 작물 선택 또는 변경
PATCH /api/devices/{deviceId}/crop                  기기 재배 작물 선택 또는 변경 (deprecated)

GET   /api/pots/{potId}/measurements/latest         화분 최신 측정값 조회
GET   /api/pots/{potId}/measurements                화분 측정값 시계열 조회
GET   /api/pots/{potId}/score                       화분 최신 환경 적합도 조회
GET   /api/pots/{potId}/soil-recommendation         화분 토양 배합 추천 조회
GET   /api/pots/{potId}/crop-recommendations        화분 대체 작물 추천 조회
GET   /api/pots/{potId}/diagnostic-history          화분 진단 이력 조회
POST  /api/pots/{potId}/irrigation                  수동 관수 요청
GET   /api/pots/{potId}/irrigation/timeline         관수 결정·명령 결과 이력 조회

GET   /api/devices/{deviceId}/measurements/latest   기기 최신 측정값 조회 (deprecated)
GET   /api/devices/{deviceId}/measurements          기기 측정값 시계열 조회 (deprecated)
GET   /api/devices/{deviceId}/score                 기기 최신 환경 적합도 조회 (deprecated)
GET   /api/devices/{deviceId}/soil-recommendation   기기 토양 배합 추천 조회 (deprecated)

GET   /api/products                                 상품 목록 조회
GET   /api/products/{productId}                     상품 상세 조회

GET   /api/cart                                     장바구니 조회
POST  /api/cart/items                               장바구니 상품 추가
PATCH /api/cart/items/{productId}                   장바구니 수량 변경
DELETE /api/cart/items/{productId}                  장바구니 상품 삭제
DELETE /api/cart                                    장바구니 비우기

POST  /api/orders                                   주문 생성
GET   /api/orders                                   내 주문 목록 조회
GET   /api/orders/{orderId}                         내 주문 상세 조회
POST  /api/orders/{orderId}/cancel                  결제 전 주문 취소

POST  /api/payments/ready                           토스 결제 준비
POST  /api/payments/confirm                         토스 결제 승인
POST  /api/payments/fail                            결제창 실패 기록
POST  /api/payments/{paymentId}/cancel              승인 결제 취소
GET   /api/orders/{orderId}/payment                 주문 결제 정보 조회

GET   /api/admin/products                           관리자 상품 전체 조회
GET   /api/admin/products/{productId}               관리자 상품 상세 조회
POST  /api/admin/products                           상품 등록
PUT   /api/admin/products/{productId}               상품 정보 수정
PATCH /api/admin/products/{productId}/stock         상품 재고 변경
GET   /api/admin/orders                             전체 주문 조회
GET   /api/admin/orders/{orderId}                   관리자 주문 상세 조회
PATCH /api/admin/orders/{orderId}/status            배송 단계 변경

POST  /api/telemetry                                하드웨어 센서 데이터 수신 (공개, 설정 시 활성화)
```

상품 응답의 `price`는 정상가, `discountRate`는 할인율, `salePrice`는 서버에서 계산한 판매가입니다.
장바구니·주문·결제 금액에는 `salePrice`가 적용되며, 관리자 상품 등록·수정 요청은 `discountRate`(0~90)를 포함합니다.

회원가입 요청 예시:

```json
{
  "email": "user@example.com",
  "password": "password1",
  "nickname": "테라바이트"
}
```

보호된 API에는 로그인 또는 회원가입 응답의 토큰을 전달합니다.

```text
Authorization: Bearer {accessToken}
```

관리자 API는 Bearer 토큰과 별도로 `ADMIN_API_KEY`에 설정한 값을 함께 전달해야 합니다.
키가 비어 있으면 관리자 API는 비활성화됩니다.

```text
X-Admin-Key: {ADMIN_API_KEY}
```

주문 상태 변경 API는 결제 완료 이후 `PAID → PREPARING → SHIPPED → DELIVERED` 순서만 허용합니다.
결제 취소는 관리자 상태 변경 API가 아니라 토스 결제 취소 API를 사용해야 합니다.

기기 등록 요청 예시:

```json
{
  "serialCode": "483920",
  "spaceName": "부산 도심 옥상 A",
  "spaceType": "건물 옥상",
  "areaSquareMeters": 42
}
```

공간 정보와 기기는 하나의 요청에서 함께 등록됩니다. 로컬 개발용 기기 코드로 `483920`, `123456`이 등록되며, 기기 등록 API에는 Bearer 토큰이 필요합니다.

개발용 JWT 비밀키는 기본값이 있지만 운영 환경에서는 반드시 `JWT_SECRET` 환경 변수로 교체해야 합니다.

## 작물 선택 API

작물 목록은 SQLite 점수 프로필과 같은 작물 코드를 사용하는 PostgreSQL 마스터 데이터에서 조회합니다.
Bearer 토큰이 필요하며 `q`를 전달하면 작물 코드 또는 한글 이름을 부분 검색합니다.

```text
GET /api/crops
GET /api/crops?q=바질
```

기기에 작물을 선택하거나 기존 선택을 변경할 때는 작물 목록에서 받은 `code`를 전달합니다.

```text
PATCH /api/devices/{deviceId}/crop
Authorization: Bearer {accessToken}
Content-Type: application/json
```

```json
{
  "cropCode": "basil"
}
```

선택 결과는 PostgreSQL의 기기에 저장됩니다. 이후 `/api/me`의 `hasCrop`과 `device.cropCode`, 환경 적합도 계산의
작물 기준은 저장된 선택값을 사용합니다. 텔레메트리의 `context.crop_type`은 측정 당시 컨텍스트로 계속 저장되지만
사용자 환경 적합도의 작물 기준을 덮어쓰지 않습니다.

## 센서 데이터 API

하드웨어는 다음 헤더와 함께 JSON을 전송합니다.

```text
POST /api/telemetry
Content-Type: application/json
X-Device-Key: {TELEMETRY_DEVICE_KEY}
```

`device_id`는 등록용 6자리 코드와 다른 하드웨어 식별자입니다. 개발용 등록 코드 `483920`은
`orangepi-pro-01`과 연결되어 있습니다. 수신 성공 시 기기 상태와 마지막 수신 시각도 갱신됩니다.
전체 요청 예시는 위의 `로컬 통합 테스트 가이드`를 참고합니다.

시계열 조회의 `metric`은 `soil_moisture_pct`, `soil_moisture_raw_adc`,
`air_temperature_c`, `air_humidity_pct`, `plant_light_ppfd_umol_m2_s`를 지원하고,
`range`는 `1h`, `24h`, `7d`, `30d`를 지원합니다.

## 환경 적합도

SQLite의 활성 작물 프로필에서 온도·습도·PPFD의 `[0점 하한, 적정 하한, 적정 상한, 0점 상한]`을
읽고 각 축을 사다리꼴 함수로 0~100점화합니다. 종합점수는 팀 합의 산식인 아래 기하평균을 사용합니다.

```text
total = 100 × (temperatureScore/100 × humidityScore/100 × lightScore/100)^(1/3)
```

오염도, CO₂, 토양수분은 종합점수에 포함하지 않습니다. 토양수분은 모니터링 값으로만 저장·조회합니다.
SQLite의 `crop_score_model_config`는 프로필별 집계 모델을 불변 버전으로 저장합니다. 현재 모든 프로필은
`equal_geometric_v1`, 지수 `1/1/1`, `trapezoid_v1`이며 정규화 후 기존 `1/3·1/3·1/3`과 같습니다.
`crop_environment_score` 뷰와 Java API는 이 설정을 읽어 같은 가중 기하평균과 `GOOD/NORMAL/BAD` 등급을
계산합니다. 기존 `crop_score_profile`의 `40/25/35` 열은 legacy 조화평균 데이터이므로 새 지수로 사용하지 않습니다.

## 자동 제어·관수 이력

`RuleEngine`은 기본 1분 간격으로 자동 제어가 활성화된 온라인 화분을 확인합니다. 자동 제어는
`pot.auto_control_enabled`가 `true`이고 작물과 최신 측정값이 있는 화분에만 적용되며, 마이그레이션 기본값은
`true`입니다. 현재 이 설정을 변경하는 사용자 API는 제공하지 않습니다.

- 토양 수분이 `RULE_SOIL_DRY_GATE_PCT`(기본 35%) 미만이고 토양 센서값이 유효하면 관수를 요청합니다.
  룰 엔진은 건조 상태를 감지할 뿐이며, 실제 승인·용량·쿨다운·일일 예산·측정값 신선도는 기존
  `IrrigationGovernor`가 최종 판단합니다.
- 조명은 `RULE_PHOTOPERIOD_START`~`RULE_PHOTOPERIOD_END`(기본 06:00~22:00) 안에서 작물 점수 프로필의
  PPFD 적정 범위를 기준으로 켜고 끕니다. 광주기 밖에서는 조명을 끕니다.
- `GET /api/pots/{potId}/irrigation/timeline?limit=20`은 자동·수동 관수의 승인 및 거절 사유, 연결된 명령의
  실행 상태를 함께 반환합니다. `limit`은 1~100 사이로 제한됩니다.

Orange Pi는 MQTT `dn/heartbeat`가 15분간 끊긴 경우에만 제한적인 긴급 관수를 수행할 수 있습니다. 이 관수는
토양 수분 15% 미만, 60mL, 최소 12시간 간격, 하루 120mL 한도를 따르며 서버의 일반 자동 관수 규칙을 복제하지
않습니다. 복구 후 게이트웨이는 `up/irrigation`으로 긴급 관수 기록을 전송하고, 백엔드는 이를
`device_command(origin=EDGE_FALLBACK, state=COMPLETED)`로 저장해 일일 예산에 반영합니다. 기록 동기화가 끝나기
전의 `RESYNC` 또는 `SAFE_HOLD` 상태 게이트웨이에는 명령을 발행하지 않습니다.

관련 설정은 다음과 같습니다.

```text
RULE_INTERVAL_MS=60000
RULE_SOIL_DRY_GATE_PCT=35.0
RULE_PHOTOPERIOD_START=06:00
RULE_PHOTOPERIOD_END=22:00
```

## 휴대폰 푸시 알림

인증 사용자는 `/api/push-tokens`로 Android FCM 토큰을 등록·교체·해제하고 `/api/notifications`에서 저장된 알림을 조회·읽음 처리할 수 있습니다. `/api/notifications/unread-count`는 목록 페이지 크기와 무관한 정확한 미확인 개수를 반환합니다. 센서 quality 장애와 MQTT 기기 오프라인 이벤트는 같은 상태가 지속되는 동안 중복 억제됩니다. 펌프 명령은 게이트웨이의 `completed` ACK가 도착했을 때만 관수 완료 알림을 생성하며, 조명 ACK는 이 알림을 만들지 않습니다. 알림과 발송 작업은 원본 이벤트와 같은 트랜잭션에 저장되며, 실제 FCM 호출은 영속 delivery outbox 작업자가 제한된 배치로 처리합니다. Firebase가 비활성화되어도 알림 이력은 저장됩니다.

실제 FCM 전송에는 다음 환경 변수가 필요합니다.

```text
FIREBASE_ENABLED=true
FIREBASE_PROJECT_ID=<firebase-project-id>
FIREBASE_CREDENTIALS_PATH=<service-account-json-path>
```

서비스 계정 JSON은 Git에 커밋하지 않습니다. 실제 전송을 켤 때는 런타임 secret/file mount로 서비스 계정 파일을 주입해야 합니다.

Docker Compose에서는 서비스 계정 JSON의 절대 경로를 `.env`에 지정하고 Firebase 전용 override를 함께 사용합니다.

```text
FIREBASE_PROJECT_ID=<firebase-project-id>
FIREBASE_CREDENTIALS_HOST_PATH=C:\absolute\path\firebase-service-account.json
```

개발 스택:

```powershell
docker compose -f docker-compose.yml -f docker-compose.firebase.yml up --build
```

프로덕션 유사 스택:

```powershell
docker compose -f docker-compose.prod.yml -f docker-compose.firebase.yml up -d --build
```

override는 파일을 컨테이너의 `/run/secrets/firebase-service-account.json`에 읽기 전용으로 마운트하고
`FIREBASE_ENABLED=true`와 컨테이너 내부 경로를 설정합니다. 백엔드용 서비스 계정 JSON은 Android 앱의
`google-services.json`과 다른 파일이며, 두 파일 모두 저장소에는 추가하지 않습니다.

발송 실패는 기본 30초부터 지수 간격으로 최대 5회 재시도합니다. `NOTIFICATION_DELIVERY_*` 환경변수로
배치 크기, 재시도 간격·횟수와 작업 claim 제한 시간을 조정할 수 있습니다. 만료된 FCM 토큰은 자동으로
비활성화되며, 로그아웃 시 해당 사용자의 활성 토큰을 해제합니다.

## 테스트

테스트에서는 외부 PostgreSQL 대신 PostgreSQL 호환 모드의 인메모리 H2를 사용하고, 점수 DB는 인메모리 SQLite를 사용합니다.

```bash
./gradlew test
```
