"""First-boot setup wizard.

Runs once, on a monitor and USB keyboard that are attached for setup and then
taken away. Four steps: check the Arduinos, join Wi-Fi, confirm this box is who
its configuration claims, show the six-digit code the user types into the app.

The identity check exists because the failure it catches is silent otherwise:
a cloned SD image shows a confident, wrong registration number, and the user
ends up claiming somebody else's gateway. On mismatch the wizard stops on a red
screen and does not write the completion marker, so it runs again next boot
rather than degrading into a dashboard.
"""

from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from typing import Callable

from . import theme
from ..identity import FAILED, VERIFIED, Verdict, verify_identity
from ..netconfig import AccessPoint, WifiManager
from ..state import read_snapshot

LOGGER = logging.getLogger(__name__)

MARKER_NAME = "setup-complete"


class Wizard:
    def __init__(
        self,
        root: tk.Tk,
        *,
        device_id: str,
        claim_code: str,
        snapshot_path: Path,
        manifest_path: Path,
        marker_path: Path,
        wifi: WifiManager | None = None,
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.device_id = device_id
        self.claim_code = claim_code
        self.snapshot_path = snapshot_path
        self.manifest_path = manifest_path
        self.marker_path = marker_path
        self.wifi = wifi or WifiManager()
        self.on_finish = on_finish or root.destroy

        available = set(tkfont.families(root))
        mono = theme.pick_family(theme.MONO_FAMILIES, available)
        sans = theme.pick_family(theme.SANS_FAMILIES, available)
        self.font_code = tkfont.Font(family=mono, size=76, weight="bold")
        self.font_h1 = tkfont.Font(family=sans, size=30, weight="bold")
        self.font_h2 = tkfont.Font(family=sans, size=19)
        self.font_body = tkfont.Font(family=mono, size=17)
        self.font_small = tkfont.Font(family=sans, size=14)

        root.configure(bg=theme.BACKGROUND)
        self.body = tk.Frame(root, bg=theme.BACKGROUND, padx=64, pady=48)
        self.body.pack(fill="both", expand=True)

        self._selected_index = 0
        self._points: list[AccessPoint] = []
        self.step_nodes()

    # -- helpers ---------------------------------------------------------

    def _clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        for sequence in ("<Up>", "<Down>", "<Return>", "<r>", "<s>", "<F2>", "<Escape>"):
            self.root.unbind(sequence)

    def _heading(self, step: str, title: str) -> None:
        tk.Label(
            self.body, text=step, bg=theme.BACKGROUND, fg=theme.ACCENT,
            font=self.font_small, anchor="w",
        ).pack(fill="x")
        tk.Label(
            self.body, text=title, bg=theme.BACKGROUND, fg=theme.TEXT,
            font=self.font_h1, anchor="w",
        ).pack(fill="x", pady=(4, 28))

    def _footer(self, text: str) -> None:
        # Inherit whatever the current step painted the body: the identity
        # failure step turns the whole screen red, and a dark strip along the
        # bottom would read as a separate widget rather than part of the alarm.
        background = self.body.cget("bg")
        foreground = "#1b1b1b" if background == theme.ERROR else theme.TEXT_MUTED
        tk.Label(
            self.body, text=text, bg=background, fg=foreground,
            font=self.font_small, anchor="w",
        ).pack(side="bottom", fill="x", pady=(28, 0))

    # -- step 1: Arduinos -------------------------------------------------

    def step_nodes(self) -> None:
        self._clear()
        self._heading("1 / 4", "아두이노 연결 확인")

        snapshot = read_snapshot(self.snapshot_path)
        ports = list((snapshot or {}).get("ports", []))

        table = tk.Frame(self.body, bg=theme.SURFACE, padx=24, pady=18)
        table.pack(fill="x")

        found = 0
        for index in range(4):
            port = ports[index] if index < len(ports) else None
            node = (port or {}).get("node_id")
            fault = (port or {}).get("fault")
            link = (port or {}).get("link")
            if fault == "duplicate_node":
                status, colour = "중복된 노드 ID", theme.ERROR
            elif fault == "unknown_node":
                status, colour = "허용 목록에 없음", theme.ERROR
            elif link == "up" and node:
                status, colour = "연결됨", theme.OK
                found += 1
            elif port is None:
                status, colour = "포트 없음", theme.IDLE
            else:
                status, colour = "신호 없음", theme.WARN

            row = tk.Frame(table, bg=theme.SURFACE)
            row.pack(fill="x", pady=6)
            tk.Label(row, text=f"화분 {index + 1}", bg=theme.SURFACE, fg=theme.TEXT,
                     font=self.font_body, width=8, anchor="w").pack(side="left")
            tk.Label(row, text=node or "—", bg=theme.SURFACE, fg=theme.TEXT_MUTED,
                     font=self.font_body, width=24, anchor="w").pack(side="left")
            tk.Label(row, text=status, bg=theme.SURFACE, fg=colour,
                     font=self.font_body, anchor="w").pack(side="left")

        message = (
            f"{found}대를 찾았습니다."
            if found
            else "아직 아두이노를 찾지 못했습니다. USB 케이블과 전원을 확인하세요."
        )
        # Fewer than four is a warning, never a blocker: a two-pot demo has to
        # be able to proceed.
        tk.Label(self.body, text=message, bg=theme.BACKGROUND,
                 fg=theme.OK if found else theme.WARN, font=self.font_h2,
                 anchor="w").pack(fill="x", pady=(24, 0))

        self._footer("Enter 계속   ·   r 다시 검사")
        self.root.bind("<Return>", lambda _e: self.step_wifi())
        self.root.bind("<r>", lambda _e: self.step_nodes())

    # -- step 2: Wi-Fi ----------------------------------------------------

    def step_wifi(self) -> None:
        self._clear()
        self._heading("2 / 4", "네트워크 연결")

        if not self.wifi.available():
            # Some Armbian builds use systemd-networkd. Show the manual route
            # rather than pretending a button will work.
            tk.Label(
                self.body,
                text="이 기기는 NetworkManager 를 쓰지 않아 자동 설정을 할 수 없습니다.\n"
                     "터미널에서 직접 연결한 뒤 Enter 를 누르세요.",
                bg=theme.BACKGROUND, fg=theme.WARN, font=self.font_h2,
                justify="left", anchor="w",
            ).pack(fill="x")
            self._footer("Enter 계속")
            self.root.bind("<Return>", lambda _e: self.step_verify())
            return

        active = self.wifi.active_ssid()
        if active and self.wifi.has_route():
            tk.Label(
                self.body,
                text=f"이미 연결되어 있습니다:  {active}",
                bg=theme.BACKGROUND, fg=theme.OK, font=self.font_h2, anchor="w",
            ).pack(fill="x", pady=(0, 20))

        self._points = self.wifi.scan()
        self._selected_index = 0
        self.list_frame = tk.Frame(self.body, bg=theme.SURFACE, padx=24, pady=18)
        self.list_frame.pack(fill="both", expand=True)
        self._render_networks()

        self._footer("↑↓ 이동   ·   Enter 선택   ·   r 다시 검색   ·   s 건너뛰기")
        self.root.bind("<Up>", lambda _e: self._move(-1))
        self.root.bind("<Down>", lambda _e: self._move(1))
        self.root.bind("<Return>", lambda _e: self._choose())
        self.root.bind("<r>", lambda _e: self.step_wifi())
        self.root.bind("<s>", lambda _e: self.step_verify())

    def _render_networks(self) -> None:
        for child in self.list_frame.winfo_children():
            child.destroy()
        if not self._points:
            tk.Label(self.list_frame, text="주변 네트워크를 찾지 못했습니다. r 을 눌러 다시 검색하세요.",
                     bg=theme.SURFACE, fg=theme.WARN, font=self.font_h2, anchor="w").pack(fill="x")
            return
        for index, point in enumerate(self._points[:8]):
            selected = index == self._selected_index
            background = theme.SURFACE_ALT if selected else theme.SURFACE
            row = tk.Frame(self.list_frame, bg=background)
            row.pack(fill="x", pady=3)
            tk.Label(row, text="▸" if selected else " ", bg=background,
                     fg=theme.ACCENT, font=self.font_body, width=3).pack(side="left")
            tk.Label(row, text=point.ssid, bg=background, fg=theme.TEXT,
                     font=self.font_body, width=34, anchor="w").pack(side="left")
            tk.Label(row, text=point.bars, bg=background, fg=theme.ACCENT,
                     font=self.font_body, width=8, anchor="w").pack(side="left")
            tk.Label(row, text="잠김" if point.secured else "개방", bg=background,
                     fg=theme.TEXT_MUTED, font=self.font_small).pack(side="left")

    def _move(self, delta: int) -> None:
        if not self._points:
            return
        limit = min(len(self._points), 8)
        self._selected_index = (self._selected_index + delta) % limit
        self._render_networks()

    def _choose(self) -> None:
        if not self._points:
            return
        point = self._points[self._selected_index]
        if point.secured:
            self.step_password(point)
        else:
            self._attempt(point, None)

    def step_password(self, point: AccessPoint) -> None:
        self._clear()
        self._heading("2 / 4", f"{point.ssid} 비밀번호")

        entry = tk.Entry(
            self.body, show="•", font=self.font_code, width=18,
            bg=theme.SURFACE, fg=theme.TEXT, insertbackground=theme.TEXT,
            relief="flat", justify="center",
        )
        entry.pack(pady=20)
        entry.focus_set()

        self.error_label = tk.Label(self.body, text="", bg=theme.BACKGROUND,
                                    fg=theme.ERROR, font=self.font_h2,
                                    anchor="w", justify="left", wraplength=1400)
        self.error_label.pack(fill="x")

        def toggle(_event=None) -> None:
            entry.configure(show="" if entry.cget("show") else "•")

        self._footer("Enter 연결   ·   F2 비밀번호 표시   ·   Esc 뒤로")
        self.root.bind("<F2>", toggle)
        self.root.bind("<Escape>", lambda _e: self.step_wifi())
        self.root.bind("<Return>", lambda _e: self._attempt(point, entry.get()))

    def _attempt(self, point: AccessPoint, password: str | None) -> None:
        result = self.wifi.connect(point.ssid, password)
        if result.ok:
            self.step_verify()
            return
        self._clear()
        self._heading("2 / 4", "연결 실패")
        tk.Label(self.body, text=point.ssid, bg=theme.BACKGROUND, fg=theme.TEXT,
                 font=self.font_h2, anchor="w").pack(fill="x")
        # nmcli's own wording names the cause; a generic message would not.
        tk.Label(self.body, text=result.error or "원인을 알 수 없습니다",
                 bg=theme.SURFACE, fg=theme.ERROR, font=self.font_body,
                 anchor="w", justify="left", wraplength=1400,
                 padx=20, pady=16).pack(fill="x", pady=20)
        self._footer("Enter 다시 시도   ·   Esc 목록으로")
        self.root.bind("<Return>", lambda _e: self.step_password(point)
                       if point.secured else self._attempt(point, None))
        self.root.bind("<Escape>", lambda _e: self.step_wifi())

    # -- step 3: identity -------------------------------------------------

    def step_verify(self) -> None:
        self._clear()
        verdict = verify_identity(
            device_id=self.device_id,
            claim_code=self.claim_code,
            manifest_path=self.manifest_path,
            online=self.wifi.available() and self.wifi.has_route(),
        )
        if verdict.state == "failed":
            self.step_identity_failure(verdict)
        else:
            self.step_code(verdict)

    def step_identity_failure(self, verdict: Verdict) -> None:
        self._clear()
        self.root.configure(bg=theme.ERROR)
        self.body.configure(bg=theme.ERROR)

        tk.Label(self.body, text="⚠  기기 신원 확인 실패", bg=theme.ERROR,
                 fg="#1b1b1b", font=self.font_h1, anchor="w").pack(fill="x", pady=(0, 20))
        tk.Label(
            self.body,
            text="이 상자가 자기 것이 아닌 등록 번호를 표시할 위험이 있어 중단했습니다.\n"
                 f"{verdict.detail}",
            bg=theme.ERROR, fg="#1b1b1b", font=self.font_h2,
            anchor="w", justify="left",
        ).pack(fill="x")

        if verdict.expected or verdict.actual:
            box = tk.Frame(self.body, bg="#1b1b1b", padx=24, pady=18)
            box.pack(fill="x", pady=24)
            for caption, value in (
                ("설정 파일", verdict.actual),
                ("프로비저닝", verdict.expected),
            ):
                row = tk.Frame(box, bg="#1b1b1b")
                row.pack(fill="x", pady=3)
                tk.Label(row, text=caption, bg="#1b1b1b", fg=theme.TEXT_MUTED,
                         font=self.font_body, width=14, anchor="w").pack(side="left")
                tk.Label(row, text=value or "—", bg="#1b1b1b", fg=theme.TEXT,
                         font=self.font_body, anchor="w").pack(side="left")

        tk.Label(
            self.body,
            text="가장 흔한 원인은 다른 기기의 SD 이미지를 복제한 경우입니다.\n"
                 "/etc/terrabyte-edge.env 를 이 상자의 값으로 고친 뒤 재부팅하세요.",
            bg=theme.ERROR, fg="#1b1b1b", font=self.font_small,
            anchor="w", justify="left",
        ).pack(fill="x")
        # Deliberately no path forward and no completion marker: the wizard
        # must run again next boot rather than fall through to a dashboard.
        self._footer("r 다시 확인")
        self.root.bind("<r>", lambda _e: self.step_verify())

    # -- step 4: claim code -----------------------------------------------

    def step_code(self, verdict: Verdict) -> None:
        self._clear()
        self._heading("4 / 4", "등록 번호")

        tk.Label(self.body, text=" ".join(self.claim_code), bg=theme.BACKGROUND,
                 fg=theme.ACCENT, font=self.font_code).pack(pady=(10, 0))
        tk.Label(self.body, text="TerraByte 앱의 기기 등록 화면에 이 번호를 입력하세요",
                 bg=theme.BACKGROUND, fg=theme.TEXT, font=self.font_h2).pack(pady=(8, 30))

        info = tk.Frame(self.body, bg=theme.SURFACE, padx=24, pady=18)
        info.pack(fill="x")
        checked = verdict.state == "verified"
        for caption, value, colour in (
            ("게이트웨이", self.device_id, theme.TEXT),
            ("확인", verdict.detail, theme.OK if checked else theme.WARN),
        ):
            row = tk.Frame(info, bg=theme.SURFACE)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=caption, bg=theme.SURFACE, fg=theme.TEXT_MUTED,
                     font=self.font_body, width=12, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=theme.SURFACE, fg=colour,
                     font=self.font_body, anchor="w").pack(side="left")

        self._footer("Enter 설정 완료 (이후 이 화면은 상태판으로 바뀝니다)")
        self.root.bind("<Return>", lambda _e: self._finish())

    def _finish(self) -> None:
        try:
            self.marker_path.parent.mkdir(parents=True, exist_ok=True)
            self.marker_path.write_text("ok\n", encoding="utf-8")
        except OSError as exc:
            LOGGER.error("could not write setup marker reason=%s", exc)
        self.on_finish()


def run(
    *,
    device_id: str,
    claim_code: str,
    snapshot_path: Path,
    manifest_path: Path,
    marker_path: Path,
    fullscreen: bool = True,
) -> int:
    root = tk.Tk()
    root.title("TerraByte 최초 설정")
    if fullscreen:
        root.attributes("-fullscreen", True)
    else:
        root.geometry("1600x900")
    Wizard(
        root,
        device_id=device_id,
        claim_code=claim_code,
        snapshot_path=snapshot_path,
        manifest_path=manifest_path,
        marker_path=marker_path,
    )
    root.mainloop()
    return 0
