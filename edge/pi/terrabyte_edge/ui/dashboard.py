"""Full-screen, read-only Tkinter status dashboard."""

from __future__ import annotations
from pathlib import Path
import time
import tkinter as tk
from tkinter import font as tkfont

from ..state import read_snapshot
from . import theme
from .render import (
    CLAIM_CODE_HELP,
    CLAIM_CODE_LABEL,
    METRIC_LABELS,
    DashboardView,
    build_view,
    format_claim_code,
)


class Dashboard:
    def __init__(self, root: tk.Tk, snapshot_path: Path, slots: int = 4) -> None:
        self.root, self.snapshot_path, self.slots = root, snapshot_path, slots
        available = set(tkfont.families(root))
        mono_family = theme.pick_family(theme.MONO_FAMILIES, available)
        sans_family = theme.pick_family(theme.SANS_FAMILIES, available)
        self.body = tkfont.Font(family=mono_family, size=theme.BODY_FONT_SIZE)
        self.title = tkfont.Font(
            family=sans_family, size=theme.TITLE_FONT_SIZE, weight="bold"
        )
        self.claim_font = tkfont.Font(
            family=mono_family, size=theme.CLAIM_CODE_FONT_SIZE, weight="bold"
        )
        root.configure(bg=theme.BACKGROUND); root.title("TerraByte 게이트웨이")
        self._build(); self.refresh()

    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=theme.BACKGROUND, padx=28, pady=24); outer.pack(fill="both", expand=True)
        self.banner = tk.Label(outer, bg=theme.ERROR, fg="#1b1b1b", font=self.title, anchor="w", padx=16, pady=12)
        header = tk.Frame(outer, bg=theme.SURFACE, padx=20, pady=14); header.pack(fill="x")
        self.gateway = tk.Label(header, bg=theme.SURFACE, fg=theme.ACCENT, font=self.title); self.gateway.pack(side="left")
        self.status = tk.Label(header, bg=theme.SURFACE, fg=theme.TEXT, font=self.body); self.status.pack(side="right")
        claim = tk.Frame(outer, bg=theme.SURFACE_ALT, padx=20, pady=12); claim.pack(fill="x", pady=(14, 0))
        tk.Label(claim, text=CLAIM_CODE_LABEL, bg=theme.SURFACE_ALT, fg=theme.TEXT, font=self.title).pack()
        self.claim_code = tk.Label(claim, bg=theme.SURFACE_ALT, fg=theme.ACCENT, font=self.claim_font)
        self.claim_code.pack()
        tk.Label(claim, text=CLAIM_CODE_HELP, bg=theme.SURFACE_ALT, fg=theme.TEXT_MUTED, font=self.body).pack()
        table = tk.Frame(outer, bg=theme.BACKGROUND, pady=18); table.pack(fill="both", expand=True)
        columns = ("화분", "노드", "상태", *[label for _, label, _ in METRIC_LABELS], "최근 수신")
        for col, text in enumerate(columns):
            table.grid_columnconfigure(col, weight=1)
            tk.Label(table, text=text, bg=theme.BACKGROUND, fg=theme.TEXT_MUTED, font=self.body).grid(row=0, column=col, sticky="ew")
        self.rows = []
        for row in range(self.slots):
            cells = []
            bg = theme.SURFACE if row % 2 == 0 else theme.SURFACE_ALT
            for col in range(len(columns)):
                label = tk.Label(table, bg=bg, fg=theme.TEXT, font=self.body, padx=8, pady=12, anchor="w")
                label.grid(row=row + 1, column=col, sticky="nsew"); cells.append(label)
            self.rows.append(cells)
        self.footer = tk.Label(outer, bg=theme.BACKGROUND, fg=theme.TEXT_MUTED, font=self.body, anchor="w"); self.footer.pack(fill="x")

    def refresh(self) -> None:
        self.apply(build_view(read_snapshot(self.snapshot_path), now_epoch=time.time(), slots=self.slots))
        self.root.after(1000, self.refresh)

    def apply(self, view: DashboardView) -> None:
        if view.banner:
            self.banner.configure(text=f"⚠  {view.banner.text} — {view.banner.detail}", bg=theme.LEVEL_COLORS[view.banner.level]); self.banner.pack(fill="x", pady=(0, 14), before=self.gateway.master)
        else: self.banner.pack_forget()
        self.gateway.configure(text=f"게이트웨이  {view.gateway_id}")
        self.status.configure(text=f"서버 {view.server_text}   대기열 {view.queue_text}   가동 {view.uptime_text}", fg=theme.LEVEL_COLORS[view.server_level])
        self.claim_code.configure(text=format_claim_code(view.claim_code))
        for row, cells in zip(view.rows, self.rows):
            for cell, text in zip(cells, (row.label, row.node_id, row.link_text, *row.values, row.last_seen)): cell.configure(text=text)
            cells[2].configure(fg=theme.LEVEL_COLORS[row.link_level])
        self.footer.configure(text=view.footer)


def run(snapshot_path: Path, *, fullscreen: bool = True) -> int:
    root = tk.Tk()
    if fullscreen: root.attributes("-fullscreen", True); root.config(cursor="none")
    else: root.geometry("1600x900")
    root.bind("<Escape>", lambda _: root.destroy()); root.bind("q", lambda _: root.destroy())
    Dashboard(root, snapshot_path); root.mainloop(); return 0
