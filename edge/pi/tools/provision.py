"""Factory-provision one TerraByte gateway: env, manifest and backend SQL.

A gateway's identity lives in three places and they must agree:

  * the backend ``device`` row (``hardware_id``, ``claim_code``, ``mqtt_username``)
  * ``/etc/terrabyte-edge.env`` on the box (``TB_DEVICE_ID``, ``TB_CLAIM_CODE``, ...)
  * ``/etc/terrabyte-edge/provisioning.json`` on the box

``terrabyte_edge.identity.verify_identity`` compares the env against the
manifest at boot and refuses to show a registration number when they disagree,
because the failure is otherwise silent: a cloned SD image confidently displays
another board's code and whoever types it claims someone else's gateway. That
check is only worth anything if the two files were produced together from one
source — this tool is that source, and it emits the matching backend statement
in the same breath so the DB row cannot drift either.

Runs on a provisioning laptop, not on the Pi, but it is stdlib-only anyway so
that it stays runnable on a freshly imaged box with nothing installed.

Usage::

    python tools/provision.py --device-id orangepi-pro-03 --output-dir ./out
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = "provisioning.json"
ENV_NAME = "terrabyte-edge.env"
SQL_NAME = "backend.sql"

# The env fragment carries the MQTT password, so it is owner-read-only. The
# manifest is the thing the wizard compares against and nothing should be
# rewriting it in the field, hence read-only for everyone.
ENV_MODE = 0o600
MANIFEST_MODE = 0o444

CLAIM_CODE_DIGITS = 6

# device.hardware_id is VARCHAR(100) (V5__add_hardware_device_id.sql) and the id
# is interpolated into SQL and into shell-sourced env, so keep it to a charset
# that cannot mean anything in either language.
DEVICE_ID_PATTERN = re.compile(r"\A[a-z0-9][a-z0-9-]{0,99}\Z")

# 'gw-' || hardware_id, from V7__relax_device_ownership.sql. The backend
# generated every existing username this way; a different convention here would
# silently fail to match rows that migration already created.
MQTT_USERNAME_PREFIX = "gw-"


class ProvisionError(Exception):
    """A refusal the operator has to act on, not a crash."""


@dataclass(frozen=True)
class Identity:
    device_id: str
    claim_code: str
    mqtt_username: str
    mqtt_password: str
    provisioned_at: str
    mqtt_password_hash: str = ""


def generate_claim_code() -> str:
    """Six digits, leading zeros included.

    ``secrets`` rather than ``random``: the code is the only thing standing
    between a stranger and claiming this gateway, so a predictable PRNG stream
    would let one guessed code expose the whole provisioning batch.
    """

    return f"{secrets.randbelow(10 ** CLAIM_CODE_DIGITS):0{CLAIM_CODE_DIGITS}d}"


def generate_mqtt_password() -> str:
    return secrets.token_urlsafe(32)


def validate_device_id(device_id: str) -> str:
    if not DEVICE_ID_PATTERN.match(device_id):
        raise ProvisionError(
            f"device id {device_id!r} 가 올바르지 않습니다: "
            "소문자·숫자·하이픈만, 100자 이내, 첫 글자는 소문자나 숫자"
        )
    return device_id


def validate_claim_code(claim_code: str) -> str:
    if len(claim_code) != CLAIM_CODE_DIGITS or not claim_code.isdigit():
        raise ProvisionError(
            f"claim code {claim_code!r} 가 올바르지 않습니다: 숫자 "
            f"{CLAIM_CODE_DIGITS}자리여야 합니다 (앞자리 0 포함)"
        )
    # ``str.isdigit`` also accepts superscripts and other Unicode digits, which
    # would pass CHAR_LENGTH = 6 in Postgres and then never match what the user
    # types on a phone keypad.
    if not claim_code.isascii():
        raise ProvisionError(f"claim code {claim_code!r} 에 ASCII 가 아닌 숫자가 있습니다")
    return claim_code


def utc_now_iso() -> str:
    """ISO-8601 with a literal Z; the manifest example uses that shape."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_identity(
    *,
    device_id: str,
    claim_code: str | None = None,
    mqtt_password: str | None = None,
    mqtt_password_hash: str = "",
    now: str | None = None,
) -> Identity:
    device_id = validate_device_id(device_id)
    claim_code = validate_claim_code(claim_code) if claim_code is not None else generate_claim_code()
    return Identity(
        device_id=device_id,
        claim_code=claim_code,
        mqtt_username=MQTT_USERNAME_PREFIX + device_id,
        mqtt_password=mqtt_password or generate_mqtt_password(),
        mqtt_password_hash=mqtt_password_hash,
        provisioned_at=now or utc_now_iso(),
    )


def render_manifest(identity: Identity) -> str:
    # Key order and names are fixed by deploy/provisioning.json.example and by
    # what identity.verify_identity reads.
    manifest = {
        "device_id": identity.device_id,
        "claim_code": identity.claim_code,
        "mqtt_username": identity.mqtt_username,
        "provisioned_at": identity.provisioned_at,
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def render_env(identity: Identity) -> str:
    """A fragment, not a whole env file.

    The rest of terrabyte-edge.env is site configuration (serial ports, broker
    host) that this tool has no business guessing; only the four per-gateway
    identity values belong here.
    """

    return "\n".join(
        [
            f"# TerraByte gateway {identity.device_id}",
            f"# tools/provision.py 생성, {identity.provisioned_at}",
            "# /etc/terrabyte-edge.env 의 해당 항목을 이 값으로 교체하십시오.",
            f"TB_DEVICE_ID={identity.device_id}",
            f"TB_CLAIM_CODE={identity.claim_code}",
            f"TB_MQTT_USERNAME={identity.mqtt_username}",
            f"TB_MQTT_PASSWORD={identity.mqtt_password}",
            "",
        ]
    )


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_sql(identity: Identity) -> str:
    """Idempotent upsert of the backend ``device`` row.

    Re-running it must be safe: an operator who is unsure whether the statement
    landed has to be able to run it again rather than guess.

    ``serial_code`` is NOT NULL UNIQUE with CHAR_LENGTH = 6 (V2__create_device),
    and V7 seeded claim_code from it, so the claim code doubles as serial_code
    for a new row. A pre-existing row with a *different* hardware_id already
    holding this serial_code would collide instead of upserting — that is the
    duplicate-code case, and failing loudly there is the point.
    """

    device_id = sql_literal(identity.device_id)
    claim_code = sql_literal(identity.claim_code)
    mqtt_username = sql_literal(identity.mqtt_username)

    columns = ["serial_code", "hardware_id", "claim_code", "mqtt_username"]
    values = [claim_code, device_id, claim_code, mqtt_username]
    updates = ["claim_code = EXCLUDED.claim_code", "mqtt_username = EXCLUDED.mqtt_username"]
    if identity.mqtt_password_hash:
        columns.append("mqtt_password_hash")
        values.append(sql_literal(identity.mqtt_password_hash))
        updates.append("mqtt_password_hash = EXCLUDED.mqtt_password_hash")

    lines = [
        f"-- TerraByte gateway {identity.device_id}",
        f"-- tools/provision.py 생성, {identity.provisioned_at}",
        "-- /etc/terrabyte-edge.env, /etc/terrabyte-edge/provisioning.json 과",
        "-- 같은 값을 백엔드 device 행에 반영한다. 여러 번 실행해도 안전하다.",
        "",
        f"INSERT INTO device ({', '.join(columns)})",
        f"VALUES ({', '.join(values)})",
        "ON CONFLICT (hardware_id) DO UPDATE SET",
    ]
    lines += [f"    {update}," for update in updates[:-1]]
    lines.append(f"    {updates[-1]};")

    if not identity.mqtt_password_hash:
        # A bcrypt/Spring delegating-encoder hash cannot be produced from the
        # stdlib, and writing a fake one would leave the row looking provisioned
        # while no gateway could ever authenticate. Leave it to the operator.
        lines += [
            "",
            "-- mqtt_password_hash 는 여기서 계산하지 않는다. Spring 위임 인코더",
            "-- 형식({bcrypt}...)이라 표준 라이브러리로는 만들 수 없다. 해시를",
            "-- 따로 만들어 --mqtt-password-hash 로 다시 생성하거나, 아래를 직접",
            "-- 채워 실행하십시오.",
            "-- UPDATE device SET mqtt_password_hash = '<hash>'",
            f"--  WHERE hardware_id = {device_id};",
        ]

    return "\n".join(lines) + "\n"


def _prepare_output_dir(output_dir: Path, force: bool) -> None:
    targets = [output_dir / name for name in (MANIFEST_NAME, ENV_NAME, SQL_NAME)]
    existing = [path for path in targets if path.exists()]
    if output_dir.exists() and any(output_dir.iterdir()) and not force:
        raise ProvisionError(
            f"출력 디렉터리 {output_dir} 가 비어 있지 않습니다. 이미 발급된 신원을 "
            "덮어쓰면 같은 claim code 를 가진 게이트웨이가 두 대 생깁니다. "
            "정말 다시 발급하려면 --force 를 주십시오."
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    # The manifest is written 0444, so an overwrite has to unlink first.
    for path in existing:
        path.unlink()


def write_outputs(identity: Identity, output_dir: Path, *, force: bool = False) -> dict[str, Path]:
    _prepare_output_dir(output_dir, force)

    paths = {
        "manifest": output_dir / MANIFEST_NAME,
        "env": output_dir / ENV_NAME,
        "sql": output_dir / SQL_NAME,
    }
    paths["manifest"].write_text(render_manifest(identity), encoding="utf-8")
    paths["env"].write_text(render_env(identity), encoding="utf-8")
    paths["sql"].write_text(render_sql(identity), encoding="utf-8")

    # Tighten the env before anything else can read it; chmod after write is a
    # small window, but the alternative (os.open with mode) does not survive an
    # overwrite of an existing file either, and this runs on a provisioning
    # laptop rather than a multi-user host.
    paths["env"].chmod(ENV_MODE)
    paths["manifest"].chmod(MANIFEST_MODE)
    return paths


def render_summary(identity: Identity, paths: dict[str, Path]) -> str:
    """Deliberately omits the MQTT password.

    It is already in the env fragment; echoing it would also put it in terminal
    scrollback and in any CI log that captured this run.
    """

    return "\n".join(
        [
            f"게이트웨이 {identity.device_id} 프로비저닝 완료",
            f"  claim code    : {identity.claim_code}",
            f"  mqtt username : {identity.mqtt_username}",
            f"  provisioned_at: {identity.provisioned_at}",
            f"  manifest      : {paths['manifest']}",
            f"  env fragment  : {paths['env']} (0600, MQTT 비밀번호 포함)",
            f"  backend sql   : {paths['sql']}",
            "",
            "MQTT 비밀번호는 화면에 출력하지 않습니다. env 파일에서 확인하십시오.",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="게이트웨이 한 대의 신원(env·manifest·backend SQL)을 한 번에 생성한다.",
    )
    parser.add_argument("--device-id", required=True, help="hardware_id / TB_DEVICE_ID")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--claim-code",
        help="6자리 숫자. 생략하면 secrets 로 생성한다.",
    )
    parser.add_argument(
        "--mqtt-password",
        help="생략하면 secrets.token_urlsafe 로 생성한다.",
    )
    parser.add_argument(
        "--mqtt-password-hash",
        default="",
        help="백엔드 device.mqtt_password_hash 에 넣을 해시. 생략하면 SQL 에 "
        "주석으로 남긴다(표준 라이브러리로는 만들 수 없음).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="비어 있지 않은 출력 디렉터리를 덮어쓴다.",
    )
    return parser


def main(argv: list[str] | None = None, *, stdout=None, stderr=None) -> int:
    args = build_parser().parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        identity = build_identity(
            device_id=args.device_id,
            claim_code=args.claim_code,
            mqtt_password=args.mqtt_password,
            mqtt_password_hash=args.mqtt_password_hash,
        )
        paths = write_outputs(identity, args.output_dir, force=args.force)
    except ProvisionError as exc:
        print(f"오류: {exc}", file=stderr)
        return 2
    print(render_summary(identity, paths), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
