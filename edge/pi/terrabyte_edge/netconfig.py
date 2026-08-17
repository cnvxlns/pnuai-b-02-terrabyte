"""Wi-Fi configuration through NetworkManager's nmcli.

Verified on the target board: Orange Pi 1.0.8 Bookworm runs NetworkManager
(nmcli 1.42.4) and the desktop user can already scan without root.

The password never appears in argv. ``nmcli --ask`` prompts on stdin instead,
which keeps it out of ``ps`` output on a machine where other local users may
exist. It is also never logged, never put in an exception message, and never
written to the status snapshot.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

LOGGER = logging.getLogger(__name__)

SCAN_TIMEOUT_SECONDS = 20
CONNECT_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class AccessPoint:
    ssid: str
    signal: int
    security: str

    @property
    def secured(self) -> bool:
        return bool(self.security) and self.security != "--"

    @property
    def bars(self) -> str:
        filled = max(1, min(4, round(self.signal / 25)))
        return "█" * filled + "░" * (4 - filled)


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    output: str
    error: str


Runner = Callable[[Sequence[str], str | None, int], CommandResult]


def _run(command: Sequence[str], stdin: str | None, timeout: int) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(False, "", "nmcli 를 찾을 수 없습니다")
    except subprocess.TimeoutExpired:
        return CommandResult(False, "", "명령이 시간 안에 끝나지 않았습니다")
    return CommandResult(
        completed.returncode == 0,
        completed.stdout.strip(),
        completed.stderr.strip(),
    )


class WifiManager:
    """nmcli operations the setup wizard needs, and nothing else."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = runner or _run

    @staticmethod
    def available() -> bool:
        """Whether this image actually uses NetworkManager.

        Some Armbian builds ship systemd-networkd instead, in which case the
        wizard must show manual instructions rather than pretend to work.
        """

        return shutil.which("nmcli") is not None

    def scan(self, *, rescan: bool = True) -> list[AccessPoint]:
        result = self._run(
            [
                "nmcli",
                "-t",
                "-f",
                "SSID,SIGNAL,SECURITY",
                "device",
                "wifi",
                "list",
                "--rescan",
                "yes" if rescan else "no",
            ],
            None,
            SCAN_TIMEOUT_SECONDS,
        )
        if not result.ok:
            LOGGER.warning("wifi scan failed reason=%s", result.error)
            return []

        seen: dict[str, AccessPoint] = {}
        for line in result.output.splitlines():
            # -t escapes embedded colons as "\:", so split on unescaped ones.
            fields = _split_terse(line)
            if len(fields) < 3 or not fields[0]:
                continue
            try:
                signal = int(fields[1])
            except ValueError:
                signal = 0
            point = AccessPoint(fields[0], signal, fields[2])
            # The same SSID appears once per band and per AP; keep the strongest.
            if point.ssid not in seen or seen[point.ssid].signal < signal:
                seen[point.ssid] = point
        return sorted(seen.values(), key=lambda ap: ap.signal, reverse=True)

    def active_ssid(self) -> str | None:
        result = self._run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
            None,
            SCAN_TIMEOUT_SECONDS,
        )
        if not result.ok:
            return None
        for line in result.output.splitlines():
            fields = _split_terse(line)
            if len(fields) >= 2 and "wireless" in fields[1]:
                return fields[0]
        return None

    def has_route(self) -> bool:
        """Whether NetworkManager believes the box has general connectivity.

        Wired counts: a gateway on ethernet is a perfectly good outcome and the
        wizard offers to skip Wi-Fi entirely in that case.
        """

        result = self._run(
            ["nmcli", "-t", "-f", "CONNECTIVITY", "general", "status"],
            None,
            SCAN_TIMEOUT_SECONDS,
        )
        return result.ok and result.output.strip().endswith("full")

    def connect(self, ssid: str, password: str | None) -> CommandResult:
        """Join a network.

        With a password we use ``--ask`` and feed it on stdin, so it never
        reaches argv. Without one we pass the plain form.
        """

        if password:
            result = self._run(
                ["nmcli", "--ask", "device", "wifi", "connect", ssid],
                password + "\n",
                CONNECT_TIMEOUT_SECONDS,
            )
        else:
            result = self._run(
                ["nmcli", "device", "wifi", "connect", ssid],
                None,
                CONNECT_TIMEOUT_SECONDS,
            )

        if result.ok:
            LOGGER.info("wifi connected ssid=%s", ssid)
        else:
            # nmcli's own message names the actual cause ("Secrets were
            # required, but not provided" for a wrong key), which is far more
            # useful on screen than a generic failure.
            LOGGER.warning("wifi connect failed ssid=%s reason=%s", ssid, result.error)
        return result


def _split_terse(line: str) -> list[str]:
    """Split an ``nmcli -t`` line, honouring backslash-escaped colons."""

    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    fields.append("".join(current))
    return fields
