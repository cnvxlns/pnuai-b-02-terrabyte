"""Full-screen status display for an attached monitor.

Runs inside the desktop session as a separate process from the bridge. It only
ever reads the published snapshot — it holds no serial port, opens no socket,
and cannot affect telemetry. A crash here loses a screen, not a reading.

Korean renders here and not on a text console: the Linux console font format
tops out at 512 glyphs, while X has NanumGothicCoding installed.
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

from . import theme
from .render import METRIC_LABELS, DashboardView, build_view
from ..state import read_snapshot

REFRESH_MS = 1000


class Dashboard:
    def __init__(self, root: tk.Tk, snapshot_path: Path, *, slots: int = 4) -> None:
        self.root = root
        self.snapshot_path = snapshot_path
        self.slots = slots

        available = set(tkfont.families(root))
        mono = theme.pick_family(theme.MONO_FAMILIES, available)
        sans = theme.pick_family(theme.SANS_FAMILIES, available)

        self.font_code = tkfont.Font(family=mono, size=64, weight="bold")
        self.font_title = tkfont.Font(family=sans, size=20, weight="bold")
        self.font_body = tkfont.Font(family=mono, size=17)
        self.font_value = tkfont.Font(family=mono, size=19, weight="bold")
        self.font_small = tkfont.Font(family=sans, size=13)

        root.configure(bg=theme.BACKGROUND)
        root.title("TerraByte 게이트웨이")

        self._build()
        self.refresh()

    # -- construction ----------------------------------------------------

    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=theme.BACKGROUND, padx=32, pady=24)
        outer.pack(fill="both", expand=True)

        self.banner = tk.Frame(outer, bg=theme.ERROR)
        self.banner_title = tk.Label(
            self.banner, bg=theme.ERROR, fg="#1b1b1b", font=self.font_title, anchor="w"
        )
        self.banner_title.pack(fill="x", padx=20, pady=(12, 0))
        self.banner_detail = tk.Label(
            self.banner, bg=theme.ERROR, fg="#1b1b1b", font=self.font_small, anchor="w"
        )
        self.banner_detail.pack(fill="x", padx=20, pady=(0, 12))

        header = tk.Frame(outer, bg=theme.SURFACE)
        header.pack(fill="x")
        self.header = header

        left = tk.Frame(header, bg=theme.SURFACE, padx=24, pady=18)
        left.pack(side="left")
        tk.Label(
            left,
            text="등록 번호",
            bg=theme.SURFACE,
            fg=theme.TEXT_MUTED,
            font=self.font_small,
            anchor="w",
        ).pack(anchor="w")
        self.code_label = tk.Label(
            left, bg=theme.SURFACE, fg=theme.ACCENT, font=self.font_code
        )
        self.code_label.pack(anchor="w")
        tk.Label(
            left,
            text="앱에서 이 번호를 입력하세요",
            bg=theme.SURFACE,
            fg=theme.TEXT_MUTED,
            font=self.font_small,
            anchor="w",
        ).pack(anchor="w")

        right = tk.Frame(header, bg=theme.SURFACE, padx=24, pady=18)
        right.pack(side="right")
        self.stat_labels: dict[str, tk.Label] = {}
        for key, caption in (
            ("gateway", "게이트웨이"),
            ("server", "서버"),
            ("queue", "대기열"),
            ("uptime", "가동"),
        ):
            row = tk.Frame(right, bg=theme.SURFACE)
            row.pack(anchor="e", pady=2)
            tk.Label(
                row,
                text=caption,
                bg=theme.SURFACE,
                fg=theme.TEXT_MUTED,
                font=self.font_small,
                width=8,
                anchor="e",
            ).pack(side="left", padx=(0, 12))
            value = tk.Label(
                row, bg=theme.SURFACE, fg=theme.TEXT, font=self.font_body, anchor="w"
            )
            value.pack(side="left")
            self.stat_labels[key] = value

        # grid, not pack: Label(width=) counts characters of that widget's own
        # font, so a row mixing the body and value fonts drifts out of
        # alignment with the header. Weighted columns are font-independent.
        table = tk.Frame(outer, bg=theme.BACKGROUND, pady=18)
        table.pack(fill="both", expand=True)

        columns = ("화분", "노드", "상태", *[label for _, label, _ in METRIC_LABELS], "최근 수신")
        weights = (3, 8, 5, 4, 3, 3, 4, 3, 5)
        for index, weight in enumerate(weights):
            table.grid_columnconfigure(index, weight=weight, uniform="cell")

        for index, text in enumerate(columns):
            tk.Label(
                table,
                text=text,
                bg=theme.BACKGROUND,
                fg=theme.TEXT_MUTED,
                font=self.font_small,
                anchor="w",
                padx=12,
            ).grid(row=0, column=index, sticky="ew", pady=(0, 6))

        self.row_widgets = []
        for index in range(self.slots):
            self.row_widgets.append(self._build_row(table, index, len(columns)))
            # Let the pot rows share the leftover height rather than leaving a
            # dead band between the table and the event pane.
            table.grid_rowconfigure(1 + index * 2, weight=1)

        events = tk.Frame(outer, bg=theme.SURFACE_ALT, padx=20, pady=12)
        events.pack(fill="x")
        tk.Label(
            events,
            text="최근 이벤트",
            bg=theme.SURFACE_ALT,
            fg=theme.TEXT_MUTED,
            font=self.font_small,
            anchor="w",
        ).pack(fill="x")
        self.event_labels = []
        for _ in range(6):
            label = tk.Label(
                events,
                text="",
                bg=theme.SURFACE_ALT,
                fg=theme.TEXT,
                font=self.font_small,
                anchor="w",
            )
            label.pack(fill="x")
            self.event_labels.append(label)

        self.footer = tk.Label(
            outer,
            text="",
            bg=theme.BACKGROUND,
            fg=theme.TEXT_MUTED,
            font=self.font_small,
            anchor="w",
        )
        self.footer.pack(fill="x", pady=(10, 0))

    def _build_row(self, parent: tk.Frame, index: int, column_count: int) -> dict:
        background = theme.SURFACE if index % 2 == 0 else theme.SURFACE_ALT
        # Two grid rows per pot: the readings, and a fault line that stays
        # empty unless something is wrong.
        value_row = 1 + index * 2
        fault_row = value_row + 1

        cells: list[tk.Label] = []
        for position in range(column_count):
            font = self.font_value if 3 <= position <= 7 else self.font_body
            label = tk.Label(
                parent,
                text="",
                bg=background,
                fg=theme.TEXT,
                font=font,
                anchor="w",
                # Padding inside the label, not around the grid cell: cell
                # padding would show the page background through as a gap and
                # break the striped row into disconnected blocks.
                padx=12,
                pady=10,
            )
            label.grid(row=value_row, column=position, sticky="nsew")
            cells.append(label)

        fault = tk.Label(
            parent,
            text="",
            bg=background,
            fg=theme.ERROR,
            font=self.font_small,
            anchor="w",
            padx=24,
        )
        return {
            "cells": cells,
            "fault": fault,
            "fault_row": fault_row,
            "columns": column_count,
            "bg": background,
        }

    # -- update ----------------------------------------------------------

    def refresh(self) -> None:
        view = build_view(
            read_snapshot(self.snapshot_path), now_epoch=time.time(), slots=self.slots
        )
        self.apply(view)
        self.root.after(REFRESH_MS, self.refresh)

    def apply(self, view: DashboardView) -> None:
        if view.banner is None:
            self.banner.pack_forget()
        else:
            colour = theme.LEVEL_COLORS[view.banner.level]
            for widget in (self.banner, self.banner_title, self.banner_detail):
                widget.configure(bg=colour)
            self.banner_title.configure(text=f"⚠  {view.banner.text}")
            self.banner_detail.configure(text=view.banner.detail)
            self.banner.pack(fill="x", pady=(0, 16), before=self.header)

        self.code_label.configure(text=" ".join(view.claim_code))
        self.stat_labels["gateway"].configure(text=view.gateway_id)
        self.stat_labels["server"].configure(
            text=view.server_text, fg=theme.LEVEL_COLORS[view.server_level]
        )
        self.stat_labels["queue"].configure(
            text=view.queue_text, fg=theme.LEVEL_COLORS[view.queue_level]
        )
        self.stat_labels["uptime"].configure(text=view.uptime_text)

        for row, widgets in zip(view.rows, self.row_widgets):
            colour = theme.LEVEL_COLORS[row.link_level]
            texts = (row.label, row.node_id, row.link_text, *row.values, row.last_seen)
            for cell, text in zip(widgets["cells"], texts):
                cell.configure(text=text)
            widgets["cells"][2].configure(fg=colour)
            muted = row.link_level in {"idle", "warn"}
            for cell in widgets["cells"][3:8]:
                cell.configure(fg=theme.TEXT_MUTED if muted else theme.TEXT)
            if row.fault:
                widgets["fault"].configure(text=f"↳ {row.fault}")
                widgets["fault"].grid(
                    row=widgets["fault_row"],
                    column=0,
                    columnspan=widgets["columns"],
                    sticky="ew",
                    pady=(0, 8),
                )
            else:
                widgets["fault"].grid_remove()

        for index, label in enumerate(self.event_labels):
            if index < len(view.events):
                clock, text = view.events[index]
                label.configure(text=f"  {clock}   {text}")
            else:
                label.configure(text="")

        self.footer.configure(text=view.footer)


def run(snapshot_path: Path, *, fullscreen: bool = True) -> int:
    root = tk.Tk()
    if fullscreen:
        root.attributes("-fullscreen", True)
        root.config(cursor="none")
    else:
        root.geometry("1600x900")
    # Escape and q exist for a developer at a keyboard. The unit restarts the
    # process, so on the appliance this redraws rather than dropping the user
    # to a desktop.
    root.bind("<Escape>", lambda _event: root.destroy())
    root.bind("q", lambda _event: root.destroy())

    Dashboard(root, snapshot_path)
    root.mainloop()
    return 0
