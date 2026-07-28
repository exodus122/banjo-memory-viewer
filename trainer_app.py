"""
trainer_app.py - Main window and polling loop for Banjo Memory Viewer.
Supports Banjo-Kazooie and Banjo-Tooie via selectable game profiles.
"""

import os
import time
import tkinter as tk
from tkinter import ttk

from bizhawk_memory import (
    BizHawkMemoryReader, XeniaMemoryReader,
    BK_PROFILE, BT_PROFILE, XENIA_BT_PROFILE, XENIA_BK_PROFILE, ALL_PROFILES,
    HEAP_STATE_EMPTY, HEAP_STATE_USED, HEAP_STATE_PERM,
    BASE_ADDR,
)
from hex_view    import HexView
from heap_view   import HeapView
from watches_view import WatchesView
from actors_view import ActorsView
from app_paths import watches_dir

POLL_INTERVAL_MS = 200   # ~5 fps

C_BG     = "#0D1117"
C_PANEL  = "#161B22"
C_BORDER = "#21262D"
C_GREEN  = "#00FF88"
C_RED    = "#FF4444"
C_YELLOW = "#FFD700"
C_BLUE   = "#58A6FF"
C_TEXT   = "#C9D1D9"
C_DIM    = "#667788"


class TrainerApp:
    def __init__(self, root: tk.Tk):
        self.root     = root
        self.root.title("Banjo Memory Viewer")
        self.root.configure(bg=C_BG)
        self.root.minsize(1000, 640)
        self.root.geometry("1280x780")

        self._profile   = BK_PROFILE
        # Reader is created lazily in _try_connect to pick the right emulator.
        # During construction, start with a BizHawk reader.
        self._reader    = BizHawkMemoryReader(self._profile)
        # Track which emulator backend is active ("bizhawk" | "xenia")
        self._emulator  = "bizhawk"
        self._connected = False
        self._paused    = False
        # Incremented every time a new poll loop is started.  Each scheduled
        # poll checks it still matches before proceeding, so stale callbacks
        # from a previous loop (e.g. after rapid game switching) self-cancel.
        self._poll_gen  = 0
        # Guard flag: True while a profile switch is in progress.  The poll
        # loop checks this and skips _refresh_all (including watch reads/writes)
        # until the switch is fully committed and the new watches are loaded.
        self._switching_profile = False
        # Tracks which JSON file the active watches should be saved to.
        # Initialised to the default profile; updated by _apply_profile_switch.
        self._current_watches_path = os.path.join(
            watches_dir(), self._profile.watches_file
        )

        self._build_ui()
        self._try_connect()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self.root.destroy()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Title bar ─────────────────────────────────────────────────────────
        bar = tk.Frame(self.root, bg=C_PANEL, height=44)
        bar.pack(fill=tk.X, side=tk.TOP)
        bar.pack_propagate(False)

        tk.Label(bar, text="Banjo Memory Viewer",
                 font=("Courier New", 13, "bold"),
                 fg=C_GREEN, bg=C_PANEL).pack(side=tk.LEFT, padx=12, pady=8)

        self._rom_label = tk.Label(bar, text="BizHawk / N64",
                                   font=("Courier New", 10),
                                   fg=C_YELLOW, bg=C_PANEL)
        self._rom_label.pack(side=tk.LEFT, padx=(8, 16))

        self._status_dot = tk.Label(bar, text="●", font=("Courier New", 14),
                                    fg=C_RED, bg=C_PANEL)
        self._status_dot.pack(side=tk.LEFT)
        self._status_label = tk.Label(bar, text="Disconnected",
                                      font=("Courier New", 9),
                                      fg=C_DIM, bg=C_PANEL)
        self._status_label.pack(side=tk.LEFT, padx=4)

        btn_style = dict(font=("Courier New", 9, "bold"), relief=tk.FLAT,
                         padx=10, pady=4, cursor="hand2")

        self._connect_btn = tk.Button(
            bar, text="⟳  CONNECT",
            bg="#1F6FEB", fg="white",
            activebackground="#388BFD", activeforeground="white",
            command=self._try_connect, **btn_style)
        self._connect_btn.pack(side=tk.RIGHT, padx=8, pady=6)

        self._pause_btn = tk.Button(
            bar, text="⏸  PAUSE",
            bg="#21262D", fg=C_TEXT,
            activebackground="#30363D", activeforeground=C_TEXT,
            command=self._toggle_pause, **btn_style)
        self._pause_btn.pack(side=tk.RIGHT, padx=(0, 4), pady=6)

        self._fps_var = tk.StringVar(value="— fps")
        tk.Label(bar, textvariable=self._fps_var,
                 font=("Courier New", 8), fg=C_DIM, bg=C_PANEL
                 ).pack(side=tk.RIGHT, padx=8)

        # ── Separator ─────────────────────────────────────────────────────────
        tk.Frame(self.root, bg=C_BORDER, height=1).pack(fill=tk.X)

        # ── Notebook ──────────────────────────────────────────────────────────
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=C_BG, borderwidth=0)
        style.configure("TNotebook.Tab",
                        background=C_PANEL, foreground=C_DIM,
                        font=("Courier New", 10, "bold"),
                        padding=(14, 6))
        style.map("TNotebook.Tab",
                  background=[("selected", C_BG)],
                  foreground=[("selected", C_GREEN)])

        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True)
        self._nb = nb

        # Tab indices, assigned as each tab is added below.  _refresh_all()
        # uses these (instead of hardcoded numbers) to decide which view to
        # poll based on which tab is actually visible - keep them in sync
        # with the nb.add() order if tabs are ever reordered.
        # Tracks which tab was active on the previous _refresh_all() tick, so
        # we can tell when a tab has *just* become visible and defer that
        # first heavy refresh - see the comment in _refresh_all().
        self._last_active_tab = -1

        self._heap_view    = HeapView(nb)
        self._heap_view.set_profile(self._profile)
        nb.add(self._heap_view, text="  HEAP  ")
        self._TAB_HEAP = 0

        self._actors_view = ActorsView(nb)
        self._actors_view.set_profile(self._profile)
        nb.add(self._actors_view, text="  ACTORS  ")
        self._TAB_ACTORS = 1

        self._watches_view = WatchesView(nb)
        nb.add(self._watches_view, text="  WATCHES  ")
        self._TAB_WATCHES = 2

        self._hex_view = HexView(nb)
        nb.add(self._hex_view, text="  HEX VIEWER  ")
        self._TAB_HEX = 3

        # ── Status bar ────────────────────────────────────────────────────────
        tk.Frame(self.root, bg=C_BORDER, height=1).pack(fill=tk.X)
        sbar = tk.Frame(self.root, bg=C_PANEL, height=24)
        sbar.pack(fill=tk.X, side=tk.BOTTOM)
        sbar.pack_propagate(False)
        self._status_bar_var = tk.StringVar(value="Ready")
        tk.Label(sbar, textvariable=self._status_bar_var,
                 font=("Courier New", 8), fg=C_DIM, bg=C_PANEL,
                 anchor="w").pack(fill=tk.X, padx=8)

    # ── Game switching ────────────────────────────────────────────────────────

    def _apply_profile_switch(self, profile, source="manual"):
        """Apply a game profile switch: update reader, watches, hex regions, UI.

        Always saves the CURRENT watches to the CURRENT profile's file first,
        then loads the new profile's watches.  This is the single place that
        transitions _current_watches_path, so there is no window where a save
        could write to the wrong file.

        Re-entrant calls (e.g. from the poll loop while a switch is already in
        progress) are silently dropped — the in-progress switch already handles
        saving and loading correctly.

        Does NOT touch self._connected or self._paused — callers manage those.
        """
        if profile is self._profile:
            return
        # Drop re-entrant calls so the poll loop can't interleave a read/write
        # between our save and load steps.
        if self._switching_profile:
            return
        self._switching_profile = True

        try:
            # ── 1. Switch profile state ───────────────────────────────────────────
            self._profile = profile
            # Keep rdram_base when auto-detecting — connect() already found it for
            # this process and wiping it would break all reads until reconnect.
            # Clear it on a manual switch so the next connect() re-scans from scratch.
            self._reader.set_profile(profile, clear_rdram=(source != "auto-detect"))

            self._status_bar_var.set(
                f"{'Auto-detected' if source == 'auto-detect' else 'Switched to'} "
                f"{profile.name} — loading watches…"
            )

            # ── 3. Load new watches from the NEW profile's file ───────────────────
            # Update _current_watches_path first so any save that happens during or
            # after _load() writes to the correct file.
            self._current_watches_path = os.path.join(watches_dir(), profile.watches_file)
            self._watches_view._load(self._current_watches_path)

            # ── 4. Update the other views ─────────────────────────────────────────
            self._hex_view.update_regions(profile.hex_regions)
            self._heap_view.set_profile(profile)
            self._actors_view.set_profile(profile)
        finally:
            # Always clear the guard, even if something above raised.
            self._switching_profile = False

    # ── Connection ────────────────────────────────────────────────────────────

    def _try_connect(self):
        self._set_status("Connecting…", C_YELLOW)
        self._status_bar_var.set("Scanning for emulator process…")
        self.root.update_idletasks()

        # Try BizHawk first, then Xenia-canary.
        bizhawk_reader = BizHawkMemoryReader(self._profile)
        ok, msg, detected_profile = bizhawk_reader.connect()
        if ok:
            self._reader   = bizhawk_reader
            self._emulator = "bizhawk"
        else:
            bz_msg = msg
            # BizHawk not found — try Xenia.
            xenia_profile = (self._profile
                             if self._profile.emulator == "xenia"
                             else XENIA_BT_PROFILE)
            xenia_reader = XeniaMemoryReader(xenia_profile)
            ok_x, msg_x, det_x = xenia_reader.connect()
            if ok_x:
                self._reader   = xenia_reader
                self._emulator = "xenia"
                ok, msg, detected_profile = ok_x, msg_x, det_x
            else:
                msg = f"BizHawk: {bz_msg} | Xenia: {msg_x}"

        self._status_bar_var.set(msg)
        if ok:
            self._connected = True

            if detected_profile is not None and detected_profile is not self._profile:
                self._apply_profile_switch(detected_profile, source="auto-detect")

            is_xenia = isinstance(self._reader, XeniaMemoryReader)
            label = "Xenia" if is_xenia else "Connected"
            self._set_status(label, C_GREEN)
            emu_label = "Xenia-canary" if is_xenia else "BizHawk N64"
            self._rom_label.configure(
                text=f"{self._profile.name}  /  {emu_label}"
            )
            self._schedule_poll()
        else:
            self._connected = False
            self._set_status("Disconnected", C_RED)
            self._show_no_game_detected()

    def _show_no_game_detected(self):
        """Reset the title bar to a neutral "nothing found" state. Without
        this, it keeps showing the default profile (Banjo-Kazooie / BizHawk
        N64) even when no emulator was actually found, which reads as if
        it's genuinely connected to that."""
        self._rom_label.configure(text="No emulator detected")

    def _set_status(self, text, color):
        self._status_dot.configure(fg=color)
        self._status_label.configure(text=text)

    def _toggle_pause(self):
        self._paused = not self._paused
        if self._paused:
            self._pause_btn.configure(text="▶  RESUME", bg="#2D6A2D")
            self._status_bar_var.set("Paused")
        else:
            self._pause_btn.configure(text="⏸  PAUSE", bg="#21262D")
            self._status_bar_var.set("Running")
            self._schedule_poll()

    # ── Polling loop ──────────────────────────────────────────────────────────

    # How often (in poll frames) to re-verify the game signature.
    # At 200 ms per frame, 25 frames = ~5 seconds.
    _VERIFY_INTERVAL = 25

    def _schedule_poll(self):
        self._poll_gen += 1
        gen = self._poll_gen
        self.root.after(POLL_INTERVAL_MS, lambda: self._poll(gen))

    def _poll(self, gen):
        # If a newer poll loop has started (rapid switch / reconnect) drop this one.
        if gen != self._poll_gen:
            return
        if self._paused or not self._connected:
            return
        t0 = time.perf_counter()
        try:
            # Periodically verify the game signature still matches.
            # If BizHawk has switched ROMs, auto-reconnect to pick up the change.
            frame = getattr(self, "_poll_frame", 0) + 1
            self._poll_frame = frame
            if frame % self._VERIFY_INTERVAL == 0:
                if not self._verify_game():
                    return   # _verify_game handles reconnect + new poll loop
            self._refresh_all()
        except Exception as e:
            self._status_bar_var.set(f"Poll error: {e}")
            import traceback; traceback.print_exc()
        elapsed = time.perf_counter() - t0
        fps = 1.0 / max(elapsed, 0.001)
        self._fps_var.set(f"{fps:.0f} fps")
        if not self._paused and gen == self._poll_gen:
            self._schedule_poll()

    def _verify_game(self):
        """
        Check that the current profile's scan signature still matches.
        Returns True if still valid.  If the signature is gone or belongs
        to the other game, triggers a silent reconnect and returns False.
        """
        r = self._reader
        if not r.connected:
            self._connected = False
            self._set_status("Disconnected", C_RED)
            self._heap_view.set_no_data("Emulator disconnected")
            self._show_no_game_detected()
            return False

        # Xenia profiles have no scan_signatures — just verify Xenia is still alive.
        if isinstance(r, XeniaMemoryReader):
            if r.find_xenia_pid() == r.pid:
                return True
            self._status_bar_var.set("Xenia process gone — re-detecting…")
            self._connected = False
            self._try_connect()
            return False

        profile = self._profile
        # Try the active profile's first signature at its expected offset.
        offset, magic = profile.scan_signatures[0]
        from bizhawk_memory import BASE_ADDR
        data = r.read_n64(BASE_ADDR + offset, len(magic))
        if data == magic:
            return True   # Still the right game

        # Signature mismatch — game may have changed.  Re-run full auto-detect.
        self._status_bar_var.set("Game signature changed — re-detecting…")
        self._connected = False
        self._try_connect()
        return False

    def _refresh_all(self):
        # Don't read or write any watch addresses while a profile switch is in
        # progress — the watch list is mid-swap and addresses are meaningless.
        if self._switching_profile:
            return
        r       = self._reader
        profile = self._profile
        active_tab = self._nb.index(self._nb.select())

        # A tab that just became visible this tick gets its first heavy
        # refresh deferred by one Tk idle pass (below) - see comment there.
        heap_just_shown = (active_tab == self._TAB_HEAP and
                            self._last_active_tab != self._TAB_HEAP)
        self._last_active_tab = active_tab

        # ── Heap ──────────────────────────────────────────────────────────────
        # Only walk the heap when the heap tab is visible, and skip the walk
        # entirely (not just the tree update) while the user is actively
        # dragging/wheel-scrolling the heap scrollbar - walk_heap() does a
        # ReadProcessMemory call per block (up to a few hundred per
        # refresh), which is real main-thread work that competes with Tk
        # for scroll responsiveness if it isn't skipped here too.
        if active_tab == self._TAB_HEAP and not self._heap_view.is_scroll_locked():
            def _do_heap_refresh():
                blocks = r.walk_heap()
                self._last_blocks = blocks   # cache for status bar
                if blocks:
                    self._heap_view.update_heap(blocks, r)
                else:
                    self._heap_view.set_no_data(
                        f"Heap not readable — is {profile.name} loaded?")

            if heap_just_shown:
                # The notebook has just raised this tab; its header
                # (buttons/checkboxes/labels) was built once at startup and
                # is already there, but Tk hasn't had an idle moment to
                # actually paint it yet. Doing the walk + tag-scan work
                # synchronously right here would eat that idle moment first,
                # making the header appear to "pop in" late. Deferring via
                # after_idle lets the raised tab paint first; the refresh
                # then runs a beat later once Tk is caught up.
                self.root.after_idle(_do_heap_refresh)
            else:
                _do_heap_refresh()

        # ── Actors ────────────────────────────────────────────────────────────
        # Runs on its own faster self-scheduled loop (like Watches) instead of
        # only refreshing whenever this ~200ms shared tick happens to land -
        # that mismatch was why Actors felt much choppier than Watches.
        # start_polling() is idempotent, and the is_visible check means the
        # (heavier, bulk-read) actual work is still skipped while the tab
        # isn't the one on screen.
        self._actors_view.start_polling(
            r, is_visible=lambda: self._nb.index(self._nb.select()) == self._TAB_ACTORS)

        # ── Hex viewer ────────────────────────────────────────────────────────
        if active_tab == self._TAB_HEX:
            hex_addr = self._hex_view.get_selected_region_addr()
            hex_size = self._hex_view.get_selected_region_size()
            hex_data = r.read_n64(hex_addr, hex_size)
            if hex_data:
                self._hex_view.update_data(hex_data, hex_addr)
            else:
                self._hex_view.set_no_data("Could not read memory region")

        # ── Watches ───────────────────────────────────────────────────────────
        self._watches_view.start_polling(r)

        # ── Status bar ────────────────────────────────────────────────────────
        self._update_status_bar(r, getattr(self, "_last_blocks", []) or [])

    def _update_status_bar(self, r, blocks):
        profile = self._profile

        # Overlay / level ID — Xenia profiles don't have a valid overlay addr yet
        ov_id = None
        if profile.emulator == "bizhawk":
            if profile.id == "bt":
                ov_id = r.read_u16_be(profile.overlay_mgr_addr)
            else:
                ov_id = r.read_u32_be(profile.overlay_mgr_addr)

        ov_str = (profile.overlay_names.get(ov_id, f"map 0x{ov_id:X}")
                  if ov_id is not None else "—")

        summary  = r.heap_summary(blocks) if blocks else {}
        free_kb  = summary.get("computed_free", 0) // 1024
        used_kb  = summary.get("computed_occupied", 0) // 1024
        n_blocks = summary.get("block_count", 0)
        n_free   = summary.get("free_count", 0)

        mode_str = "XENIA LIVE" if isinstance(r, XeniaMemoryReader) else "BIZHAWK LIVE"

        self._status_bar_var.set(
            f"[{profile.name}]  Level: {ov_str}   |   "
            f"Heap: {n_blocks} blocks  FREE {free_kb} KB ({n_free} frags)  USED {used_kb} KB   |   "
            f"{mode_str}"
        )
