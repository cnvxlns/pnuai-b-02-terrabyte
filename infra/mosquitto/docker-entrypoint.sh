#!/bin/sh
# mosquitto 컨테이너 기동 전에 두 가지를 처리하고 나서 mosquitto를 실행한다.
# docker-compose.yml 의 mosquitto 서비스가 entrypoint로 이 스크립트를 지정한다.
#
# 1) TLS 인증서 유무에 따라 8883 리스너를 켜거나 끈다 (graceful degrade).
#    mosquitto 설정 문법 자체에는 "파일이 있으면 이 listener 블록을 활성화"
#    같은 조건문이 없다. 그래서 TLS listener 정의(tls.conf.template)는 평소엔
#    include_dir 바깥에 두고, 인증서가 있을 때만 그 안으로 복사해 넣는 방식으로
#    우회했다. 즉 mosquitto.conf 자체는 항상 같고, 켜고 끄는 건 이 스크립트다.
#
# 2) 읽기 전용으로 바인드 마운트된 passwd/aclfile을 쓰기 가능한 런타임 경로로
#    복사하고 권한을 0600으로 좁힌다.
#    왜 레포에 직접 chmod 값을 박아두는 방법이 안 통하는가:
#      - git은 실행 비트(755 vs 644)만 추적한다. 나머지 권한 비트(예: 0600)는
#        추적 대상이 아니라서, clone/체크아웃 시점의 실제 파일 모드는 umask 등
#        로컬 환경에 따라 달라지고 0600을 보장하지 못한다.
#      - 설사 저장소에서 0600이었다 해도, docker-compose.yml이 이 파일들을
#        `:ro`(read-only)로 바인드 마운트하기 때문에 컨테이너 안에서
#        chmod를 실행하면 "Read-only file system" 에러가 난다. 마운트 자체가
#        호스트 파일과 같은 inode를 가리키므로 컨테이너에서 권한을 못 바꾼다.
#    그래서 이 스크립트가 기동 시점에 실제 쓰기 가능한 볼륨(런타임 전용, 매
#    기동마다 다시 만들어짐)으로 파일을 복사하고 그 사본에만 0600을 건다.
set -eu

CONFIG_SRC=/mosquitto/config-src
RUNTIME=/mosquitto/config/runtime

mkdir -p "$RUNTIME/conf.d"

cp "$CONFIG_SRC/passwd" "$RUNTIME/passwd"
cp "$CONFIG_SRC/aclfile" "$RUNTIME/aclfile"
chmod 0600 "$RUNTIME/passwd" "$RUNTIME/aclfile"
# mosquitto.conf에 `user` 지시자가 없어도 mosquitto는 기본값으로 root에서
# "mosquitto"(uid/gid 1883) 사용자로 권한을 낮춘 뒤 파일을 연다. 이 스크립트는
# 컨테이너가 root로 시작할 때 실행되므로, 0600으로 좁힌 뒤에도 소유자를
# mosquitto로 넘겨주지 않으면 실제 mosquitto 프로세스가 자기 파일을 못 읽는다.
chown mosquitto:mosquitto "$RUNTIME/passwd" "$RUNTIME/aclfile" "$RUNTIME" "$RUNTIME/conf.d"

if [ -f /mosquitto/certs/ca.crt ] && [ -f /mosquitto/certs/server.crt ] && [ -f /mosquitto/certs/server.key ]; then
  cp "$CONFIG_SRC/tls.conf.template" "$RUNTIME/conf.d/tls.conf"
  echo "[mosquitto entrypoint] TLS 인증서 확인됨 — 8883 리스너를 켭니다."
else
  rm -f "$RUNTIME/conf.d/tls.conf"
  echo "[mosquitto entrypoint] 경고: infra/mosquitto/certs/ 에 TLS 인증서가 없습니다." \
    "8883 TLS 리스너 없이 1883 평문 리스너만으로 시작합니다." \
    "TLS를 켜려면 './infra/mosquitto/generate-certs.sh' 실행 후" \
    "'docker compose restart mosquitto' 를 실행하세요." >&2
fi

exec mosquitto -c "$CONFIG_SRC/mosquitto.conf"
