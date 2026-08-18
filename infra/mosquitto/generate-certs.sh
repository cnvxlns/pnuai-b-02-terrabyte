#!/usr/bin/env bash
# infra/mosquitto/certs/ 아래에 자체 서명(self-signed) CA와 서버 인증서를 생성한다.
#
# 데모용 TLS 리스너(8883)를 켜기 위한 스크립트다. 진짜 인증기관(CA) 없이 우리가
# 직접 CA 역할을 하는 루트 인증서를 만들고, 그 CA로 mosquitto 서버 인증서에
# 서명한다. **운영 배포에는 쓰지 않는다** — 실제 인증서 운영 계획은
# infra/mosquitto/README.md의 "운영 환경 인증서 계획" 절 참고.
#
# 생성되는 파일 (모두 git-ignore 대상, infra/mosquitto/.gitignore 참고):
#   certs/ca.key      CA 개인키 — 절대 커밋하지 않는다.
#   certs/ca.crt       CA 인증서 — 게이트웨이가 이 파일로 서버를 신뢰한다 (--cafile).
#   certs/server.key  mosquitto 서버 개인키 — 절대 커밋하지 않는다.
#   certs/server.crt  mosquitto 서버 인증서 (CA가 서명).
#   certs/server.csr  서명 요청 (중간 산출물, 재생성 가능).
#
# SAN(Subject Alternative Name) 이 왜 중요한가:
#   컨테이너 내부의 백엔드는 서비스명 "mosquitto"로 접속하고, 다른 기기(Orange Pi
#   게이트웨이)는 이 macOS/Linux 호스트의 LAN IP로 접속한다. 인증서가 "localhost"
#   하나만 커버하면 게이트웨이 쪽 TLS 핸드셰이크가 hostname mismatch로 항상
#   실패한다 — 즉 localhost만 넣은 인증서는 게이트웨이 입장에서 아무 쓸모가 없다.
#   그래서 SAN 목록은 기본으로 mosquitto / localhost / 127.0.0.1 을 넣고,
#   호스트의 LAN IP를 자동 감지해 추가하며, 필요하면 인자나 환경 변수로 더
#   추가할 수 있게 한다.
#
# 사용법:
#   cd infra/mosquitto
#   ./generate-certs.sh                        # SAN 자동 감지만 사용
#   ./generate-certs.sh 192.168.0.10 my-host    # 인자로 SAN 추가
#   TB_MOSQUITTO_CERT_SAN=10.0.0.5,edge.local ./generate-certs.sh   # 콤마 목록으로 추가
#
# openssl 이 필요하다 (macOS/Linux 기본 제공).
set -euo pipefail

cd "$(dirname "$0")"

CERT_DIR="certs"
DAYS=825   # 브라우저/OS가 일반적으로 받아주는 자체 서명 인증서 최대 유효기간 근사치. 데모용이라 넉넉히 잡는다.

mkdir -p "$CERT_DIR"

# --- SAN 목록 구성 -----------------------------------------------------------
# 항상 포함: 컴포즈 서비스명(mosquitto), localhost, 127.0.0.1.
sans=("mosquitto" "localhost" "127.0.0.1")

# 호스트 LAN IP 자동 감지 (macOS 우선, 실패하면 Linux 방식 시도). 실패해도
# 스크립트는 계속 진행한다 — 그 경우 인자나 환경 변수로 IP를 직접 넣어야 한다.
detected_ip=""
if command -v ipconfig >/dev/null 2>&1; then
  detected_ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
fi
if [ -z "$detected_ip" ] && command -v hostname >/dev/null 2>&1; then
  detected_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
if [ -n "$detected_ip" ]; then
  sans+=("$detected_ip")
  echo "감지된 호스트 LAN IP: $detected_ip (SAN에 자동 추가됨)"
else
  echo "경고: 호스트 LAN IP를 자동 감지하지 못했습니다. 게이트웨이가 접속할 IP를" \
       "인자나 TB_MOSQUITTO_CERT_SAN 환경 변수로 직접 넘겨주세요." >&2
fi

# 환경 변수로 추가 SAN (콤마 구분).
if [ -n "${TB_MOSQUITTO_CERT_SAN:-}" ]; then
  IFS=',' read -ra extra_env_sans <<< "$TB_MOSQUITTO_CERT_SAN"
  sans+=("${extra_env_sans[@]}")
fi

# 스크립트 인자로 추가 SAN.
if [ "$#" -gt 0 ]; then
  sans+=("$@")
fi

# openssl SAN 문자열 생성 (IP는 IP:, 나머지는 DNS: 로 분류).
san_entries=()
dns_idx=1
ip_idx=1
for entry in "${sans[@]}"; do
  if [[ "$entry" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    san_entries+=("IP.${ip_idx}:${entry}")
    ip_idx=$((ip_idx + 1))
  else
    san_entries+=("DNS.${dns_idx}:${entry}")
    dns_idx=$((dns_idx + 1))
  fi
done
san_string=$(IFS=,; echo "${san_entries[*]}")
echo "서버 인증서 SAN: $san_string"

# --- 1) CA 키 + 자체 서명 CA 인증서 ------------------------------------------
openssl genrsa -out "$CERT_DIR/ca.key" 4096
openssl req -x509 -new -nodes \
  -key "$CERT_DIR/ca.key" \
  -sha256 -days "$DAYS" \
  -subj "/O=TerraByte Dev/CN=TerraByte Dev Mosquitto CA" \
  -out "$CERT_DIR/ca.crt"

# --- 2) 서버 키 + CSR ---------------------------------------------------------
openssl genrsa -out "$CERT_DIR/server.key" 2048
openssl req -new \
  -key "$CERT_DIR/server.key" \
  -subj "/O=TerraByte Dev/CN=mosquitto" \
  -out "$CERT_DIR/server.csr"

# --- 3) CA로 서버 인증서 서명 (SAN 확장 포함) --------------------------------
openssl x509 -req \
  -in "$CERT_DIR/server.csr" \
  -CA "$CERT_DIR/ca.crt" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
  -out "$CERT_DIR/server.crt" \
  -days "$DAYS" -sha256 \
  -extfile <(printf "subjectAltName=%s" "$san_string")

# 개인키는 소유자만 읽을 수 있어야 한다.
chmod 600 "$CERT_DIR/ca.key" "$CERT_DIR/server.key"
chmod 644 "$CERT_DIR/ca.crt" "$CERT_DIR/server.crt"
rm -f "$CERT_DIR/server.csr" "$CERT_DIR/ca.srl"

echo
echo "완료: infra/mosquitto/$CERT_DIR/ 에 CA + 서버 인증서를 생성했습니다."
echo "  - ca.crt      게이트웨이/클라이언트가 --cafile 로 사용"
echo "  - server.crt / server.key   mosquitto 8883 리스너가 사용"
echo
echo "docker compose restart mosquitto 로 브로커를 재시작하면 8883 TLS 리스너가 켜집니다."
echo "(인증서가 없으면 8883 없이 1883 평문 리스너만으로 정상 기동합니다 — mosquitto.conf 주석 참고.)"
