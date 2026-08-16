"""Does this box's configuration match the gateway it was provisioned as?

Kept out of the UI package on purpose: this decides whether a registration
number may be shown at all, so it must be testable on any machine, with or
without a display toolkit installed.

The failure it catches is silent otherwise. Clone an SD image onto a second
board and it will confidently display the first board's registration number;
whoever types that number into the app claims someone else's gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

VERIFIED = "verified"
UNVERIFIED = "unverified"
FAILED = "failed"


@dataclass(frozen=True)
class Verdict:
    state: str
    detail: str
    expected: str = ""
    actual: str = ""

    @property
    def may_show_code(self) -> bool:
        """Only a hard mismatch withholds the code.

        Offline is not a mismatch. Refusing to show the number because the
        venue Wi-Fi is down would block a demo to prevent a mistake that has
        not been shown to have happened.
        """

        return self.state != FAILED


def verify_identity(
    *,
    device_id: str,
    claim_code: str,
    manifest_path: Path,
    online: bool,
) -> Verdict:
    if not claim_code:
        return Verdict(FAILED, "TB_CLAIM_CODE 가 설정되어 있지 않습니다")

    if not manifest_path.is_file():
        return Verdict(UNVERIFIED, "공장 프로비저닝 파일이 없어 대조하지 못했습니다")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Verdict(UNVERIFIED, f"프로비저닝 파일을 읽지 못했습니다: {exc}")
    if not isinstance(manifest, dict):
        return Verdict(UNVERIFIED, "프로비저닝 파일 형식이 올바르지 않습니다")

    provisioned_device = manifest.get("device_id")
    provisioned_code = manifest.get("claim_code")
    if not isinstance(provisioned_device, str) or not isinstance(provisioned_code, str):
        return Verdict(UNVERIFIED, "프로비저닝 파일에 필요한 값이 없습니다")

    if provisioned_device != device_id:
        return Verdict(
            FAILED,
            "게이트웨이 ID 가 프로비저닝 값과 다릅니다",
            expected=provisioned_device,
            actual=device_id,
        )
    if provisioned_code != claim_code:
        return Verdict(
            FAILED,
            "등록 번호가 프로비저닝 값과 다릅니다",
            expected=provisioned_code,
            actual=claim_code,
        )

    if not online:
        return Verdict(UNVERIFIED, "오프라인 상태라 서버 확인은 하지 못했습니다")
    return Verdict(VERIFIED, "설정 파일과 프로비저닝 값이 일치합니다")
