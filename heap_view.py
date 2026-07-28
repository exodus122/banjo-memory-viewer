"""
heap_view.py - Banjo-Kazooie heap visualizer.

Performance improvements:
  - Tag cache: tag_block is only called when a block is new or dynamic.
  - Whole-frame skip: if the visible row fingerprint hasn't changed, the
    entire Treeview update is skipped (most frames when heap is stable).
  - addr_to_iid map is maintained across frames — no per-frame tree.item()
    calls to rebuild it.
  - tree.index() (O(n) Tk IPC) is eliminated; reorder is detected by
    comparing the new addr order list against the previous one in Python,
    then tree.move() is only called when the sequence actually changed.
  - Dynamic types (asset, particle, unknown) are re-tagged every frame;
    all other types are cached indefinitely until the block disappears.

Features:
  - Clickable column headers: sort ascending / descending.
  - Filter toolbar: ALL / FREE / USED / PERM tabs + free-text search.
  - Dump CSV: saves current filtered+sorted view to a timestamped file.
"""

import csv
import os
import re
import time
import tkinter as tk
from tkinter import ttk
from bt_assets import BT_ASSETS, BT_ANIM_ASSETS
from app_paths import app_dir, resource_path

# ── Colours ───────────────────────────────────────────────────────────────────
C_BG     = "#0D1117"
C_PANEL  = "#161B22"
C_BORDER = "#21262D"
C_HEADER = "#00FF88"
C_TEXT   = "#C9D1D9"
C_DIM    = "#667788"
FONT     = ("Courier New", 9)
FONT_B   = ("Courier New", 9, "bold")

C_FREE   = "#FF4444"
C_USED   = "#7EE787"
C_PERM   = "#80C0FF"
C_UNK    = "#888888"

BASE_ADDR  = 0x80000000
HEAP_START = 0x8002D500 # default value if no profile selected
HEAP_SIZE  = 0x210520   # default value if no profile selected
HEAP_END   = HEAP_START + HEAP_SIZE
HEAP_HEADER_SIZE = 0x10

HEAP_STATE_EMPTY = 0
HEAP_STATE_USED  = 1
HEAP_STATE_PERM  = 2

# ── Column definitions ────────────────────────────────────────────────────────
def _hex_or_int(s):
    if isinstance(s, int):
        return s
    try:
        return int(s, 16) if s.startswith("0x") else int(s)
    except (ValueError, TypeError):
        return 0

COLS = [
    # (id,       label,    w,   anchor,   stretch)
    ("#",        "#",      40,  "center", False),
    ("State",    "State",  50,  "center", False),
    ("Address",  "Addr",   90,  "center", False),
    ("End",      "End",    90,  "center", False),
    ("Chunk",    "Chunk",  70,  "center", False),
    ("Used",     "Used",   70,  "center", False),
    ("Source",   "Source", 200, "w",      False),
    ("Type",     "Type",   180, "center", False),
    ("Label",    "Label",  200, "w",      True),
]
COL_IDS = [c[0] for c in COLS]

COL_SORT = {
    "#":       lambda v: _hex_or_int(v[0]),
    "Address": lambda v: _hex_or_int(v[2]),
    "End":     lambda v: _hex_or_int(v[3]),
    "Chunk":   lambda v: _hex_or_int(v[4]),
    "Used":    lambda v: _hex_or_int(v[5]),
}
for _col_id in ("State", "Source", "Type", "Label"):
    _idx = COL_IDS.index(_col_id)
    COL_SORT[_col_id] = (lambda idx: (lambda v: v[idx].lower()))(_idx)

_STATE_TAG  = {HEAP_STATE_EMPTY: "free", HEAP_STATE_USED: "used", HEAP_STATE_PERM: "perm"}
_STATE_NAME = {HEAP_STATE_EMPTY: "FREE", HEAP_STATE_USED: "USED", HEAP_STATE_PERM: "PERM"}

def state_name(state):  return _STATE_NAME.get(state, "UNK ")
def state_color(state): return {HEAP_STATE_EMPTY: C_FREE,
                                 HEAP_STATE_USED:  C_USED,
                                 HEAP_STATE_PERM:  C_PERM}.get(state, C_UNK)


class _LockedScrollbar(ttk.Scrollbar):
    """Keep the thumb a stable size while the user drags it, without adding
    input lag.

    Background (found through a lot of isolated testing, see
    scrollbar_repro_test*.py in the project root): with a wide, many-column
    Treeview, rapidly re-scrolling it during a fast thumb-drag makes the
    Treeview's OWN yscrollcommand callback occasionally report a
    transiently-wrong (first, last) pair before its internal geometry has
    actually settled - visible as the thumb momentarily growing/shrinking
    mid-drag. This reproduces with 100% static, unchanging data, so it's not
    our own table refresh (already paused during a drag, see `dragging`
    below) or CPU contention from the poll loop - it's the Treeview itself
    under a wide layout.

    A fixed-delay throttle on how often we forward drag motion to the
    Treeview does prevent it (giving Tk time to fully settle each redraw
    before the next), but a delay long enough to reliably avoid it (~100ms)
    makes the thumb visibly lag behind the mouse. after_idle-based
    throttling removes the lag but brings the resize back (Tk considers
    itself idle before the wide Treeview has actually finished settling).

    The fix that gets both: decouple the thumb's own rendering from the
    Treeview's. While dragging, we draw the thumb ourselves immediately on
    every motion event (zero lag) using the size last known to be correct
    from BEFORE the drag started, and we stop trusting the Treeview's own
    set() reports until the drag is fully over - so a mid-drag redraw
    hiccup from the Treeview can never reach the screen as a resize. The
    real underlying scroll of the Treeview still happens in the background
    (throttled to a modest rate just so it doesn't fall further behind);
    its possibly-wrong intermediate geometry is simply discarded until we
    unlock.
    """

    _DRAG_FLUSH_MS = 40   # background Treeview scroll rate while dragging

    def __init__(self, *args, **kwargs):
        self._real_command = kwargs.pop("command", None)
        kwargs["command"] = self._on_command
        super().__init__(*args, **kwargs)
        self.dragging       = False
        self._unlock_id     = None
        self._pending_args  = None
        self._throttle_id   = None
        self._known_size    = 1.0   # last-known-good (last - first), frozen during drag
        self.bind("<ButtonPress-1>",   self._on_press,   add=True)
        self.bind("<ButtonRelease-1>", self._on_release, add=True)

    def set(self, first, last):
        first = float(first)
        last  = float(last)
        if self.dragging:
            # Ignore the Treeview's own geometry while dragging - it can be
            # transiently wrong under rapid re-scrolling. Keep the size we
            # already know is correct, only ever let position move.
            last = min(1.0, first + self._known_size)
        else:
            self._known_size = max(0.0, last - first)
        super().set(first, last)

    def _on_command(self, *args):
        # Immediate visual feedback: move the thumb right now, at the
        # requested position, holding the pre-drag size steady. This can't
        # glitch since it never depends on the Treeview's own response.
        if self.dragging and args and args[0] == "moveto":
            frac = float(args[1])
            new_first = max(0.0, min(1.0 - self._known_size, frac))
            super().set(new_first, new_first + self._known_size)

        # Background: still tell the Treeview to actually scroll, but
        # coalesced to a modest rate - all that matters is it keeps up
        # reasonably, not that every single motion event reaches it.
        self._pending_args = args
        if self._throttle_id is None:
            self._flush_command()

    def _flush_command(self):
        args = self._pending_args
        self._pending_args = None
        if self._real_command and args is not None:
            self._real_command(*args)
        self._throttle_id = self.after(self._DRAG_FLUSH_MS, self._throttle_tick)

    def _throttle_tick(self):
        self._throttle_id = None
        if self._pending_args is not None:
            self._flush_command()

    def _on_press(self, _=None):
        if self._unlock_id is not None:
            self.after_cancel(self._unlock_id)
            self._unlock_id = None
        self.dragging = True

    def _on_release(self, _=None):
        # Short tail so the last set() from the treeview after release
        # (which carries the correct fractions) still gets suppressed until
        # the dust settles, then we unlock.
        self._unlock_id = self.after(150, self._do_unlock)

    def _do_unlock(self):
        self.dragging   = False
        self._unlock_id = None

    def _bump(self):
        """One-shot lock for mousewheel — press then immediately schedule release."""
        self._on_press()
        self._on_release()


# ─────────────────────────────────────────────────────────────────────────────
class HeapView(tk.Frame):

    # Tag types whose label can change between frames even when addr/state/size
    # are identical — always re-tagged, never frozen in cache.
    _DYNAMIC_TYPES = frozenset({"asset", "ParticleEmitter", "ParticleEmitter *", "unknown",
                                 "ActorArray", "Player Object"})

    # Heap bar zoom (mouse wheel over the bar): multiplicative zoom per
    # wheel notch, and the deepest allowed zoom expressed as a fraction of
    # the full heap range (so you can never zoom in past ~1% of the heap).
    _BAR_ZOOM_STEP      = 1.25
    _BAR_MIN_VIEW_FRAC  = 0.01

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C_BG, **kw)
        self._blocks  = []
        self._reader  = None
        self._selected  = None
        self._profile = None   # Set via set_profile(); None until first call

        # (addr, state, chunk_size) → (btype, blabel, bsource)
        self._tag_cache: dict = {}
        self._tag_scan_cache: list = []

        # iid → values-tuple last written to the tree widget
        self._render_cache: dict = {}

        # addr_str ("0x8012A400") → iid — maintained incrementally so we never
        # need to call tree.item() in a loop just to rebuild this map.
        self._addr_to_iid: dict = {}

        # Ordered list of addr_strs from the previous render pass.
        # Used to detect whether tree.move() calls are needed at all.
        self._last_iid_order: list = []

        # Fingerprint of the last rendered visible set.
        # Tuple of (addr_int, hash(vals)).  If unchanged → skip all Tk work.
        self._last_fp: tuple = ()

        self._sort_col     = "#"
        self._sort_asc     = True
        self._filter_state = None   # None = ALL, or HEAP_STATE_* int
        self._filter_text  = ""

        # Heap bar: [(x0, x1, block), ...] in ascending x0 order, rebuilt by
        # _redraw_bar() - used to hit-test mouse position for hover/click.
        self._bar_segments: list = []
        self._bar_hover_addr  = None   # addr of the currently-hovered block, or None
        self._bar_hover_item  = None   # canvas id of the hover outline rect
        self._bar_tooltip       = None   # Toplevel, created lazily
        self._bar_tooltip_label = None
        self._bar_last_xy       = None   # last mouse (x, y) over the bar, or None

        # Heap bar zoom window, as a fraction (0.0-1.0) of the full heap
        # range. (0.0, 1.0) = fully zoomed out - the whole heap visible,
        # same as before this was zoomable. Updated by scrolling over the
        # bar; _bar_view_start/_bar_view_size (actual addr/byte-count, not
        # fractions) are recomputed each _redraw_bar() call so the wheel
        # handler can convert pixel <-> address without redoing that math.
        self._bar_view_start_frac = 0.0
        self._bar_view_end_frac   = 1.0
        self._bar_view_start = None
        self._bar_view_size  = None

        # Right-click-drag panning, active only while dragging - keeps the
        # zoom span fixed and just slides the window left/right within it.
        self._bar_pan_start_x          = None   # cursor x (canvas-local) when drag began
        self._bar_pan_start_view_start = None   # view_start (bytes) when drag began

        self.asset_enum_names = self.load_asset_enum_names(resource_path("enums.h"))
        self.asset_enum_names_bt = {}
        self._build_ui()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_scroll_locked(self):
        """True while the user is actively dragging the scrollbar thumb (or
        within the brief post-mousewheel lock window) AND the "Pause while
        scrolling" checkbox is on. trainer_app checks this before doing a
        heap walk at all, not just before touching the tree - the walk
        itself (one ReadProcessMemory call per block, up to a few hundred
        per refresh) is real work competing with Tk for the same UI thread,
        and skipping it while the user is actively scrolling is what
        actually keeps scrolling feeling smooth. The checkbox lets the user
        trade that smoothness back for the heap staying live during a
        scroll, if they'd rather have that."""
        return self._vsb.dragging and self._pause_while_scrolling_var.get()

    def set_profile(self, profile):
        """Switch game profile; clears all caches AND the tree widget rows."""
        if profile is self._profile:
            return
        self._profile = profile
        self._tag_cache.clear()
        self._tag_scan_cache = []
        self._render_cache.clear()
        self._addr_to_iid.clear()
        self._last_iid_order.clear()
        self._last_fp = ()
        self._blocks  = []
        self._selected = None
        # Wipe the Treeview directly — the delta logic in _refresh_table only
        # removes rows that are tracked in _addr_to_iid, but after clearing
        # that dict the old rows become orphaned in the widget.
        self._tree.delete(*self._tree.get_children())
        self._canvas.delete("all")
        self._bar_segments = []
        self._bar_hover_addr = None
        self._bar_hover_item = None
        self._hide_bar_tooltip()
        # Different game = different heap range entirely - don't carry a
        # zoom window computed against the old profile's heap over to this one.
        self._bar_view_start_frac = 0.0
        self._bar_view_end_frac   = 1.0
        self._bar_pan_start_x = None
        self._bar_pan_start_view_start = None
        self._summary_var.set("")

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header row
        hdr = tk.Frame(self, bg=C_BG)
        hdr.pack(fill=tk.X, padx=8, pady=(8, 4))
        tk.Label(hdr, text="HEAP VIEWER",
                 font=("Courier New", 11, "bold"),
                 fg=C_HEADER, bg=C_BG).pack(side=tk.LEFT)
        self._summary_var = tk.StringVar(value="")
        tk.Label(hdr, textvariable=self._summary_var,
                 font=FONT, fg=C_DIM, bg=C_BG).pack(side=tk.RIGHT)

        # Visual heap bar
        self._canvas = tk.Canvas(self, bg=C_PANEL, height=22, highlightthickness=0,
                                  cursor="hand2")
        self._canvas.pack(fill=tk.X, padx=8, pady=(0, 2))
        self._canvas.bind("<Configure>", lambda e: self._redraw_bar())
        self._canvas.bind("<Motion>",    self._on_bar_motion)
        self._canvas.bind("<Leave>",     self._on_bar_leave)
        self._canvas.bind("<Button-1>",  self._on_bar_click)
        # Zoom in/out on the heap bar, centred on the cursor. <MouseWheel>
        # covers Windows/macOS; <Button-4>/<Button-5> cover X11, which sends
        # wheel motion as ordinary button clicks instead of a delta.
        self._canvas.bind("<MouseWheel>", self._on_bar_wheel)
        self._canvas.bind("<Button-4>",   self._on_bar_wheel)
        self._canvas.bind("<Button-5>",   self._on_bar_wheel)
        # Double-click resets the zoom back to the whole heap - otherwise
        # there'd be no quick way back out once zoomed in deep.
        self._canvas.bind("<Double-Button-1>", self._on_bar_reset_zoom)
        # Right-click-drag pans left/right within the current zoom level,
        # without changing how zoomed in it is. Button-2 is bound too for
        # macOS two-finger click, matching the convention used elsewhere
        # in this file for right-click.
        for _down in ("<Button-3>", "<Button-2>"):
            self._canvas.bind(_down, self._on_bar_pan_start)
        for _drag in ("<B3-Motion>", "<B2-Motion>"):
            self._canvas.bind(_drag, self._on_bar_pan_motion)
        for _up in ("<ButtonRelease-3>", "<ButtonRelease-2>"):
            self._canvas.bind(_up, self._on_bar_pan_end)

        # Toolbar
        tb = tk.Frame(self, bg=C_PANEL)
        tb.pack(fill=tk.X, padx=8, pady=(0, 2))

        tab_cfg = [("ALL", None), ("FREE", HEAP_STATE_EMPTY),
                   ("USED", HEAP_STATE_USED), ("PERM", HEAP_STATE_PERM)]
        self._tab_btns: dict = {}
        for label, sv in tab_cfg:
            btn = tk.Button(tb, text=label,
                            font=("Courier New", 9, "bold"),
                            relief=tk.FLAT, padx=10, pady=3, cursor="hand2",
                            command=lambda s=sv: self._set_state_filter(s))
            btn.pack(side=tk.LEFT, padx=(0, 2))
            self._tab_btns[sv] = btn

        # Heap selector.  Only the Xenia profiles have more than one heap, so it
        # stays hidden otherwise (see _refresh_heap_choices).
        self._heap_label = tk.Label(tb, text="  Heap:", font=FONT, fg=C_DIM,
                                    bg=C_PANEL)
        self._heap_var = tk.StringVar()
        self._heap_combo = ttk.Combobox(tb, textvariable=self._heap_var,
                                        state="readonly", width=26,
                                        font=FONT)
        self._heap_combo.bind("<<ComboboxSelected>>", self._on_heap_selected)
        self._heap_keys: list = []

        tk.Label(tb, text="  Filter:", font=FONT, fg=C_DIM,
                 bg=C_PANEL).pack(side=tk.LEFT, padx=(8, 2))
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", self._on_filter_change)
        tk.Entry(tb, textvariable=self._filter_var,
                 font=FONT, bg="#0D1117", fg=C_TEXT,
                 insertbackground=C_TEXT, relief=tk.FLAT, width=22,
                 highlightthickness=1, highlightcolor=C_HEADER,
                 highlightbackground=C_BORDER).pack(side=tk.LEFT, padx=2, pady=3)
        tk.Button(tb, text="✕", font=FONT, relief=tk.FLAT,
                  bg=C_PANEL, fg=C_DIM, cursor="hand2", padx=4,
                  command=lambda: self._filter_var.set("")).pack(side=tk.LEFT)

        tk.Button(tb, text="💾  DUMP CSV",
                  font=("Courier New", 9, "bold"),
                  relief=tk.FLAT, padx=10, pady=3, cursor="hand2",
                  bg="#21262D", fg=C_TEXT,
                  activebackground="#30363D", activeforeground=C_TEXT,
                  command=self._dump_csv).pack(side=tk.RIGHT, padx=4)

        self._pause_while_scrolling_var = tk.BooleanVar(value=False)
        tk.Checkbutton(tb, text="Pause while scrolling",
                       variable=self._pause_while_scrolling_var,
                       font=FONT, fg=C_TEXT, bg=C_PANEL,
                       selectcolor=C_BG, activebackground=C_PANEL,
                       activeforeground=C_TEXT).pack(side=tk.RIGHT, padx=(4, 8))

        self._update_tab_styles()

        # PanedWindow
        paned = tk.PanedWindow(self, orient=tk.VERTICAL, bg=C_BORDER,
                               sashwidth=5, sashrelief=tk.FLAT, handlesize=0)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        tree_frame = tk.Frame(paned, bg=C_BG)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self._tree = ttk.Treeview(tree_frame, columns=COL_IDS,
                                  show="headings",
                                  selectmode="browse")
        for col_id, label, w, anchor, stretch in COLS:
            self._tree.heading(col_id, text=col_id,
                               command=lambda c=col_id: self._sort_by(c))
            self._tree.column(col_id, width=w, anchor=anchor, stretch=stretch)
        vsb = _LockedScrollbar(tree_frame, orient=tk.VERTICAL,
                               command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._vsb = vsb
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        paned.add(tree_frame, stretch="always", minsize=80)

        detail_frame = tk.Frame(paned, bg=C_PANEL)
        self._detail_text = tk.Text(detail_frame, font=FONT, fg=C_TEXT,
                                    bg=C_PANEL, relief=tk.FLAT,
                                    borderwidth=0, highlightthickness=0,
                                    state=tk.DISABLED, wrap=tk.NONE,
                                    cursor="arrow")
        detail_vsb = ttk.Scrollbar(detail_frame, orient=tk.VERTICAL,
                                   command=self._detail_text.yview)
        self._detail_text.configure(yscrollcommand=detail_vsb.set)
        detail_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._detail_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        paned.add(detail_frame, stretch="never", minsize=40)
        self.after(50, lambda: paned.sash_place(0, 0, 9999))
        self._paned = paned

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=C_BG, foreground=C_TEXT,
                        fieldbackground=C_BG, font=FONT, rowheight=18)
        style.configure("Treeview.Heading", background=C_PANEL,
                        foreground=C_HEADER, font=FONT_B)
        self._tree.tag_configure("free", foreground=C_FREE)
        self._tree.tag_configure("used", foreground=C_USED)
        self._tree.tag_configure("perm", foreground=C_PERM)
        self._tree.tag_configure("unk",  foreground=C_UNK)
        self._tree.tag_configure("sel",  background="#1F3050")
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.bind("<Button-2>", self._on_right_click)  # macOS two-finger click
        # Mousewheel on the treeview also changes scroll position; lock the
        # scrollbar briefly so the thumb doesn't jump during live updates.
        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._tree.bind(_seq, lambda _e: self._vsb._bump(), add=True)

        # Context menu
        self._ctx_menu = tk.Menu(self, tearoff=0,
                                 bg=C_PANEL, fg=C_TEXT,
                                 activebackground="#1F6FEB", activeforeground="white",
                                 font=FONT, bd=0, relief=tk.FLAT)
        self._ctx_menu.add_command(label="Copy Address",      command=lambda: self._copy_col(2))
        self._ctx_menu.add_command(label="Copy End Address",  command=lambda: self._copy_col(3))
        self._ctx_menu.add_command(label="Copy Chunk Size",   command=lambda: self._copy_col(4))
        self._ctx_menu.add_command(label="Copy Used Size",    command=lambda: self._copy_col(5))
        self._ctx_menu.add_command(label="Copy Type",         command=lambda: self._copy_col(7))
        self._ctx_menu.add_command(label="Copy Label",        command=lambda: self._copy_col(8))
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Copy Row (TSV)",    command=self._copy_row_tsv)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Go to Address…",    command=self._go_to_address_dialog)
        self._ctx_menu_iid = None

        self._detail_text.configure(state=tk.NORMAL)
        self._detail_text.insert(tk.END, "  Click a row to inspect details")
        self._detail_text.configure(state=tk.DISABLED)

    # ── Public API ────────────────────────────────────────────────────────────

    def _refresh_heap_choices(self, reader):
        """Populate the heap dropdown, hiding it when there's nothing to pick."""
        choices = []
        if reader is not None and hasattr(reader, "list_heap_choices"):
            try:
                choices = reader.list_heap_choices()
            except Exception:
                choices = []

        if not choices:
            self._heap_label.pack_forget()
            self._heap_combo.pack_forget()
            self._heap_keys = []
            return

        keys   = [k for k, _ in choices]
        labels = [lbl for _, lbl in choices]
        if keys != self._heap_keys:
            self._heap_keys = keys
            self._heap_combo.configure(values=labels)
            current = reader.get_heap_selection()
            idx = keys.index(current) if current in keys else 0
            self._heap_combo.current(idx)

        if not self._heap_combo.winfo_ismapped():
            self._heap_label.pack(side=tk.LEFT, padx=(8, 2))
            self._heap_combo.pack(side=tk.LEFT, padx=2, pady=3)

    def _on_heap_selected(self, _event=None):
        idx = self._heap_combo.current()
        if self._reader is None or not (0 <= idx < len(self._heap_keys)):
            return
        self._reader.set_heap_selection(self._heap_keys[idx])
        # Switching heaps changes the address range entirely, so drop any
        # selection and reset the bar rather than leaving a stale viewport.
        self._selected = None
        self._bar_view_start_frac = 0.0
        self._bar_view_end_frac = 1.0

    def update_heap(self, blocks, reader):
        self._reader = reader
        self._blocks = blocks
        self._refresh_heap_choices(reader)

        pid = self._profile.id if self._profile is not None else ""
        if pid == "xenia_bt":
            self._tag_scan_cache = self._build_bt_xenia_tag_scan_cache(reader)
        elif pid == "xenia_bk":
            self._tag_scan_cache = self._build_bk_xenia_tag_scan_cache(reader)
        elif pid == "bt":
            self._tag_scan_cache = self._build_bt_tag_scan_cache(reader)
        else:
            self._tag_scan_cache = self._build_bk_tag_scan_cache(reader)

        self._pre_tag_blocks(blocks, reader)
        self._refresh_table()
        self._refresh_summary(blocks)
        self._redraw_bar()

    def set_no_data(self, msg="Waiting for data..."):
        self._tree.delete(*self._tree.get_children())
        self._render_cache.clear()
        self._addr_to_iid.clear()
        self._last_iid_order.clear()
        self._last_fp = ()
        self._summary_var.set(msg)
        self._detail_text.configure(state=tk.NORMAL)
        self._detail_text.delete("1.0", tk.END)
        self._detail_text.insert(tk.END, f"  {msg}")
        self._detail_text.configure(state=tk.DISABLED)

    # ── Tag caching ───────────────────────────────────────────────────────────

    def _cache_key(self, b):
        # Include used_size so that a new allocation at the same address with
        # the same chunk size but different content doesn't get a stale label.
        return (b["addr"], b["state"], b["chunk_size"], b.get("used_size", 0))

    def _pre_tag_blocks(self, blocks, reader):
        for b in blocks:
            k = self._cache_key(b)
            cached = self._tag_cache.get(k)
            if cached is None or cached[0] in self._DYNAMIC_TYPES:
                btype, blabel, bsource = self.tag_block(b, reader)
                self._tag_cache[k] = (btype, blabel, bsource)
        live = {self._cache_key(b) for b in blocks}
        for k in [k for k in self._tag_cache if k not in live]:
            del self._tag_cache[k]

    def _get_tag(self, b):
        return self._tag_cache.get(self._cache_key(b), ("unknown", "", ""))

    # ── Filter / sort ─────────────────────────────────────────────────────────

    def _set_state_filter(self, sv):
        self._filter_state = sv
        self._update_tab_styles()
        self._last_fp = ()
        self._refresh_table()

    def _on_filter_change(self, *_):
        self._filter_text = self._filter_var.get().lower()
        self._last_fp = ()
        self._refresh_table()

    def _update_tab_styles(self):
        for sv, btn in self._tab_btns.items():
            active = (sv == self._filter_state)
            btn.configure(bg="#1F6FEB" if active else "#21262D",
                          fg="white"   if active else C_DIM)

    def _sort_by(self, col_id):
        self._sort_asc = not self._sort_asc if self._sort_col == col_id else True
        self._sort_col = col_id
        self._update_sort_headers()
        self._last_fp = ()
        self._refresh_table()

    def _update_sort_headers(self):
        for col_id, label, *_ in COLS:
            arrow = (" ▲" if self._sort_asc else " ▼") if col_id == self._sort_col else ""
            self._tree.heading(col_id, text=label + arrow)

    def _build_row_values(self, i, b):
        btype, blabel, bsource = self._get_tag(b)
        return (
            i,
            _STATE_NAME.get(b["state"], "UNK "),
            f"0x{b['addr']:08X}",
            f"0x{b['end_addr']:08X}",
            f"0x{b['chunk_size']:05X}",
            f"0x{b['used_size']:05X}",
            bsource,
            btype,
            blabel,
        )

    def _build_visible(self):
        """Return filtered+sorted list of (orig_1based_idx, block, values)."""
        state_f = self._filter_state
        text_f  = self._filter_text
        rows = []
        for i, b in enumerate(self._blocks):
            if state_f is not None and b["state"] != state_f:
                continue
            vals = self._build_row_values(i + 1, b)
            if text_f and text_f not in " ".join(str(v) for v in vals).lower():
                continue
            rows.append((i + 1, b, vals))
        key_fn = COL_SORT.get(self._sort_col)
        if key_fn:
            rows.sort(key=lambda r: key_fn(r[2]), reverse=not self._sort_asc)
        return rows

    # ── Table refresh ─────────────────────────────────────────────────────────

    def _refresh_table(self):
        # Don't mutate the tree while the user is dragging the scrollbar thumb.
        # Row insertions/deletions change the treeview's virtual height, which
        # changes the first/last fractions, which resizes the thumb mid-drag.
        if self._vsb.dragging:
            return

        tree    = self._tree
        visible = self._build_visible()

        fp = (len(visible), *((b["addr"], hash(vals)) for _, b, vals in visible))
        if fp == self._last_fp:
            return
        self._last_fp = fp

        sel_addr       = self._selected
        a2i            = self._addr_to_iid
        new_addr_order = [vals[2] for _, _, vals in visible]
        keep_set       = set(new_addr_order)

        # ── Delete rows no longer visible ─────────────────────────────────────
        gone = [addr for addr in list(a2i) if addr not in keep_set]
        for addr in gone:
            iid = a2i.pop(addr)
            tree.delete(iid)
            self._render_cache.pop(iid, None)

        # ── Insert or update rows ─────────────────────────────────────────────
        sel_iid = None
        for _, b, vals in visible:
            addr_key = vals[2]
            tag      = _STATE_TAG.get(b["state"], "unk")

            if addr_key in a2i:
                iid = a2i[addr_key]
                if self._render_cache.get(iid) != vals:
                    tree.item(iid, values=vals, tags=(tag,))
                    self._render_cache[iid] = vals
            else:
                iid = tree.insert("", tk.END, tags=(tag,), values=vals)
                a2i[addr_key] = iid
                self._render_cache[iid] = vals

            if sel_addr is not None and b["addr"] == sel_addr:
                sel_iid = iid

        # ── Reorder only when order changed ───────────────────────────────────
        if new_addr_order != self._last_iid_order:
            for pos, addr_key in enumerate(new_addr_order):
                tree.move(a2i[addr_key], "", pos)
            self._last_iid_order = list(new_addr_order)

        # ── Selection ─────────────────────────────────────────────────────────
        if sel_iid:
            tree.selection_set(sel_iid)

    # ── Summary / bar ─────────────────────────────────────────────────────────

    def _refresh_summary(self, blocks):
        heap_size = self._profile.heap_size if self._profile else HEAP_SIZE
        free_kb = used_kb = perm_kb = n_free = 0
        for b in blocks:
            s, sz = b["state"], b["chunk_size"]
            if   s == HEAP_STATE_EMPTY: free_kb += sz; n_free += 1
            elif s == HEAP_STATE_USED:  used_kb += sz
            elif s == HEAP_STATE_PERM:  perm_kb += sz
        self._summary_var.set(
            f"  {len(blocks)} blocks   "
            f"FREE {free_kb/1024:.1f} KB ({n_free} frags)   "
            f"USED {used_kb/1024:.1f} KB   "
            f"PERM {perm_kb/1024:.1f} KB   "
            f"TOTAL {heap_size/1024:.0f} KB"
        )

    def _full_heap_range(self):
        """(heap_start, heap_size) for the whole heap, regardless of the
        current bar zoom/pan window. Shared by _redraw_bar, the wheel-zoom
        handler, and the right-click pan handlers so they all agree on the
        same range to zoom/pan within. Returns (None, None) if unknown."""
        heap_start = self._profile.heap_start if self._profile else HEAP_START
        heap_size  = self._profile.heap_size  if self._profile else HEAP_SIZE
        # For profiles where heap bounds are discovered dynamically (heap_size=0),
        # derive the range from the actual block list.
        if not heap_size and self._blocks:
            heap_start = self._blocks[0]["addr"]
            heap_size  = self._blocks[-1]["end_addr"] + 1 - heap_start
        if not heap_size:
            return None, None
        return heap_start, heap_size

    def _redraw_bar(self):
        c = self._canvas
        c.delete("all")
        # delete("all") just wiped the hover outline (if any) along with
        # everything else - forget the stale canvas id so we don't try to
        # delete it again later.
        self._bar_hover_item = None
        self._bar_segments = []
        W = c.winfo_width()
        H = c.winfo_height() or 22
        if not W or not self._blocks:
            return
        heap_start, heap_size = self._full_heap_range()
        if not heap_size:
            return

        # Apply the current zoom window (see _on_bar_wheel). Defaults to
        # (0.0, 1.0) - the whole heap - identical to the pre-zoom behaviour.
        view_start = heap_start + heap_size * self._bar_view_start_frac
        view_size  = heap_size * (self._bar_view_end_frac - self._bar_view_start_frac)
        if view_size <= 0:
            view_size = heap_size
        view_end = view_start + view_size
        # Cached so _on_bar_wheel can convert pixel <-> address without
        # redoing this calculation itself.
        self._bar_view_start = view_start
        self._bar_view_size  = view_size

        scale = W / view_size
        for b in self._blocks:
            # Skip blocks entirely outside the current zoom window.
            if b["end_addr"] < view_start or b["addr"] > view_end:
                continue
            x0 = int((b["addr"]         - view_start) * scale)
            x1 = int((b["end_addr"] + 1 - view_start) * scale)
            x1 = max(x1, x0 + 1)
            # A thin outline in the panel's own background colour visually
            # separates adjacent blocks without needing extra canvas items.
            c.create_rectangle(x0, 0, x1, H,
                               fill=state_color(b["state"]), outline=C_PANEL, width=1)
            self._bar_segments.append((x0, x1, b))

        self._draw_zoom_indicators(c, W, H)

        # If the mouse is already sitting over the bar (e.g. this redraw was
        # just a periodic data refresh, not the user moving the mouse),
        # reapply the hover highlight/tooltip instead of leaving them stale
        # or missing until the next actual mouse movement.
        if self._bar_last_xy is not None:
            self._apply_bar_hover(*self._bar_last_xy)

    def _draw_zoom_indicators(self, c, W, H):
        """When zoomed in, draw a small arrow at whichever edge(s) still
        have heap content scrolled off-screen, so it's obvious the bar
        isn't showing the entire heap right now."""
        zoomed_left  = self._bar_view_start_frac > 1e-9
        zoomed_right = self._bar_view_end_frac   < 1 - 1e-9
        if not zoomed_left and not zoomed_right:
            return
        mid_y   = H / 2
        arrow_h = min(H, 10)
        if zoomed_left:
            c.create_polygon(2, mid_y, 9, mid_y - arrow_h / 2, 9, mid_y + arrow_h / 2,
                              fill="#FFFFFF", outline="")
        if zoomed_right:
            c.create_polygon(W - 2, mid_y, W - 9, mid_y - arrow_h / 2, W - 9, mid_y + arrow_h / 2,
                              fill="#FFFFFF", outline="")

    # ── Heap bar hover / click ──────────────────────────────────────────────

    def _find_bar_segment(self, x):
        """Binary search self._bar_segments (sorted, non-overlapping x-ranges)
        for the segment containing pixel column x. O(log n) instead of a
        linear scan since this runs on every mouse-move over the bar."""
        segs = self._bar_segments
        lo, hi = 0, len(segs) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            x0, x1, _b = segs[mid]
            if x < x0:
                hi = mid - 1
            elif x >= x1:
                lo = mid + 1
            else:
                return segs[mid]
        return None

    def _apply_bar_hover(self, x, y):
        seg = self._find_bar_segment(x)
        if seg is None:
            self._clear_bar_hover()
            return
        x0, x1, b = seg
        if self._bar_hover_addr != b["addr"] or self._bar_hover_item is None:
            self._bar_hover_addr = b["addr"]
            if self._bar_hover_item is not None:
                self._canvas.delete(self._bar_hover_item)
            H = self._canvas.winfo_height() or 22
            self._bar_hover_item = self._canvas.create_rectangle(
                x0, 0, x1, H, outline="#FFFFFF", width=2)
            self._update_bar_tooltip_text(b)
        self._move_bar_tooltip(x, y)

    def _clear_bar_hover(self):
        self._bar_hover_addr = None
        if self._bar_hover_item is not None:
            self._canvas.delete(self._bar_hover_item)
            self._bar_hover_item = None
        self._hide_bar_tooltip()

    def _on_bar_motion(self, event):
        self._bar_last_xy = (event.x, event.y)
        self._apply_bar_hover(event.x, event.y)

    def _on_bar_leave(self, _=None):
        self._bar_last_xy = None
        self._clear_bar_hover()

    def _on_bar_click(self, event):
        seg = self._find_bar_segment(event.x)
        if seg is None:
            return
        _, _, b = seg
        self.go_to_address(b["addr"])

    def _on_bar_wheel(self, event):
        """Zoom the heap bar in/out, centred on whatever address is under
        the cursor right now, so the thing you're scrolling over is the
        thing that stays put on screen as the view scales around it."""
        W = self._canvas.winfo_width()
        if not W or self._bar_view_start is None or self._bar_view_size is None:
            return

        # X11 sends wheel motion as Button-4 (up/zoom-in) or Button-5
        # (down/zoom-out) clicks with no delta; Windows/macOS report a
        # signed <MouseWheel> delta instead (positive = zoom in).
        num = getattr(event, "num", None)
        if num == 4:
            zoom_in = True
        elif num == 5:
            zoom_in = False
        else:
            zoom_in = event.delta > 0

        heap_start, heap_size = self._full_heap_range()
        if not heap_size:
            return

        # Address currently under the cursor, in the *pre-zoom* view - this
        # is what should still be under the cursor after rescaling.
        frac_at_cursor = min(max(event.x / W, 0.0), 1.0)
        anchor_addr = self._bar_view_start + frac_at_cursor * self._bar_view_size

        new_size = (self._bar_view_size / self._BAR_ZOOM_STEP if zoom_in
                    else self._bar_view_size * self._BAR_ZOOM_STEP)
        min_size = heap_size * self._BAR_MIN_VIEW_FRAC
        new_size = max(min_size, min(new_size, heap_size))

        new_start = anchor_addr - frac_at_cursor * new_size
        new_start = max(heap_start, min(new_start, heap_start + heap_size - new_size))

        self._bar_view_start_frac = (new_start - heap_start) / heap_size
        self._bar_view_end_frac   = (new_start + new_size - heap_start) / heap_size

        self._bar_last_xy = (event.x, event.y)
        self._redraw_bar()

    def _on_bar_reset_zoom(self, _=None):
        self._bar_view_start_frac = 0.0
        self._bar_view_end_frac   = 1.0
        self._redraw_bar()

    # ── Heap bar right-click-drag panning ────────────────────────────────────
    # Slides the view left/right while zoomed in, without changing the zoom
    # level itself - the span (view_size / _bar_view_end_frac - _bar_view_
    # start_frac) never changes here, only where that span starts.

    def _on_bar_pan_start(self, event):
        if self._bar_view_start is None or self._bar_view_size is None:
            return
        self._bar_pan_start_x          = event.x
        self._bar_pan_start_view_start = self._bar_view_start
        # Hovering/tooltip doesn't make sense mid-pan-drag.
        self._clear_bar_hover()

    def _on_bar_pan_motion(self, event):
        if self._bar_pan_start_x is None or self._bar_pan_start_view_start is None:
            return
        W = self._canvas.winfo_width()
        if not W:
            return
        heap_start, heap_size = self._full_heap_range()
        if not heap_size:
            return

        view_size = self._bar_view_size
        # Dragging right reveals heap content that was further left, i.e.
        # the view start moves opposite the cursor - the classic "grab and
        # drag the content" panning feel.
        dx_pixels = event.x - self._bar_pan_start_x
        dx_bytes  = dx_pixels * (view_size / W)
        new_start = self._bar_pan_start_view_start - dx_bytes
        new_start = max(heap_start, min(new_start, heap_start + heap_size - view_size))

        self._bar_view_start_frac = (new_start - heap_start) / heap_size
        self._bar_view_end_frac   = (new_start + view_size - heap_start) / heap_size

        # <Motion> doesn't fire while a button is held down, so _bar_last_xy
        # would otherwise stay stuck at wherever the drag started - making
        # _redraw_bar() reapply the hover/tooltip at a now-stale position
        # instead of whatever's actually under the cursor mid-drag.
        self._bar_last_xy = (event.x, event.y)
        self._redraw_bar()

    def _on_bar_pan_end(self, _event=None):
        self._bar_pan_start_x = None
        self._bar_pan_start_view_start = None

    def _ensure_bar_tooltip(self):
        if self._bar_tooltip is not None:
            return
        tw = tk.Toplevel(self)
        tw.overrideredirect(True)
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        lbl = tk.Label(tw, text="", font=FONT, fg=C_TEXT, bg="#1F2733",
                       relief=tk.SOLID, borderwidth=1, justify=tk.LEFT,
                       padx=6, pady=3)
        lbl.pack()
        tw.withdraw()
        self._bar_tooltip = tw
        self._bar_tooltip_label = lbl

    def _update_bar_tooltip_text(self, b):
        self._ensure_bar_tooltip()
        btype, blabel, bsource = self._get_tag(b)
        size = b["chunk_size"]
        lines = [
            f"{_STATE_NAME.get(b['state'], 'UNK')}   "
            f"0x{b['addr']:08X} – 0x{b['end_addr']:08X}  (0x{size:X} bytes)",
            btype + (f"  {blabel}" if blabel else ""),
        ]
        if bsource:
            lines.append(f"Source: {bsource}")
        self._bar_tooltip_label.configure(text="\n".join(lines))

    def _move_bar_tooltip(self, x, y):
        if self._bar_tooltip is None:
            return
        tw = self._bar_tooltip
        # Make sure reqwidth/reqheight reflect the current label text before
        # using them below - they can be stale from a previous (differently
        # sized) tooltip otherwise.
        tw.update_idletasks()
        tw_w = tw.winfo_reqwidth()
        tw_h = tw.winfo_reqheight()

        cursor_x = self._canvas.winfo_rootx() + x
        cursor_y = self._canvas.winfo_rooty() + y
        screen_w = tw.winfo_screenwidth()
        screen_h = tw.winfo_screenheight()

        # Prefer right/below the cursor, but flip to the opposite side
        # whenever that would push the tooltip off the edge of the screen.
        if cursor_x + 14 + tw_w > screen_w:
            final_x = cursor_x - 14 - tw_w
        else:
            final_x = cursor_x + 14
        if cursor_y + 20 + tw_h > screen_h:
            final_y = cursor_y - 20 - tw_h
        else:
            final_y = cursor_y + 20

        final_x = max(0, min(final_x, screen_w - tw_w))
        final_y = max(0, min(final_y, screen_h - tw_h))

        tw.geometry(f"+{final_x}+{final_y}")
        tw.deiconify()

    def _hide_bar_tooltip(self):
        if self._bar_tooltip is not None:
            self._bar_tooltip.withdraw()

    # ── Selection ─────────────────────────────────────────────────────────────

    def _on_select(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        vals = self._tree.item(sel[0], "values")
        if not vals:
            return
        try:
            target_addr = int(vals[2], 16)
        except ValueError:
            return
        b = next((blk for blk in self._blocks if blk["addr"] == target_addr), None)
        if b is None:
            return
        idx = self._blocks.index(b)

        self._selected = b["addr"]

        btype, blabel, bsource = self._get_tag(b)
        lines = [
            f"  Block #{idx+1}   State: {state_name(b['state'])}   "
            f"Type: {btype}  {blabel}",
            f"  Source: {bsource}" if bsource else "  Source: —",
            f"  Addr:  0x{b['addr']:08X}  →  0x{b['end_addr']:08X}",
            f"  Chunk: 0x{b['chunk_size']:05X} ({b['chunk_size']} bytes)   "
            f"Used: 0x{b['used_size']:05X} ({b['used_size']} bytes)   "
            f"Wasted: 0x{b['unused']:04X}",
            f"  Prev:  0x{b['prev']:08X}    Next: 0x{b['next']:08X}",
        ]
        if "prev_free" in b:
            lines.append(f"  PrevFree: 0x{b['prev_free']:08X}   "
                         f"NextFree: 0x{b['next_free']:08X}")
        self._detail_text.configure(state=tk.NORMAL)
        self._detail_text.delete("1.0", tk.END)
        self._detail_text.insert(tk.END, "\n".join(lines))
        self._detail_text.configure(state=tk.DISABLED)

    # ── Right-click / copy ────────────────────────────────────────────────────

    def _on_right_click(self, event):
        if self._tree.identify_region(event.x, event.y) == "heading":
            self._show_column_menu(event)
            return
        iid = self._tree.identify_row(event.y)
        if iid:
            # Select the row under the cursor so the user can see what they're copying
            self._tree.selection_set(iid)
            self._ctx_menu_iid = iid
            # Enable copy commands when a row is right-clicked
            for i in range(self._ctx_menu.index(tk.END) + 1):
                try:
                    label = self._ctx_menu.entrycget(i, "label")
                    if label != "Go to Address…":
                        self._ctx_menu.entryconfig(i, state=tk.NORMAL)
                except tk.TclError:
                    pass
        else:
            self._ctx_menu_iid = None
            # Disable copy commands, only keep "Go to Address…" usable
            for i in range(self._ctx_menu.index(tk.END) + 1):
                try:
                    label = self._ctx_menu.entrycget(i, "label")
                    if label != "Go to Address…":
                        self._ctx_menu.entryconfig(i, state=tk.DISABLED)
                except tk.TclError:
                    pass
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    # ── Column show/hide (right-click a header) ──────────────────────────────

    def _visible_columns(self):
        """Current displaycolumns as a real list, resolving ttk's "#all"
        placeholder (meaning "everything, in declared order") to COL_IDS."""
        cur = list(self._tree["displaycolumns"])
        return list(COL_IDS) if cur == ["#all"] else cur

    def _show_column_menu(self, event):
        menu = tk.Menu(self, tearoff=0, bg=C_PANEL, fg=C_TEXT,
                       activebackground="#1F6FEB", activeforeground="white",
                       font=FONT, bd=0, relief=tk.FLAT)
        visible = self._visible_columns()
        for col_id, label, *_ in COLS:
            shown = col_id in visible
            # add_command with our own mark instead of add_checkbutton - the
            # native checkbutton indicator can render invisibly against this
            # menu's dark custom colours on some platforms/themes.
            menu.add_command(
                label=f"{'✓' if shown else '  '}  {label}",
                command=lambda c=col_id, s=shown: self._toggle_column(c, not s))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _toggle_column(self, col_id, show):
        visible = self._visible_columns()
        if show:
            if col_id not in visible:
                visible.append(col_id)
        else:
            if col_id in visible and len(visible) > 1:
                visible.remove(col_id)
            # else: refuse to hide the last remaining column
        # Keep declared column order regardless of toggle order.
        ordered = [c for c in COL_IDS if c in visible]
        self._tree.configure(displaycolumns=ordered)

    def _copy_col(self, col_index):
        iid = self._ctx_menu_iid
        if iid is None:
            return
        vals = self._tree.item(iid, "values")
        if not vals or col_index >= len(vals):
            return
        self._clip(str(vals[col_index]))

    def _copy_row_tsv(self):
        iid = self._ctx_menu_iid
        if iid is None:
            return
        vals = self._tree.item(iid, "values")
        if not vals:
            return
        self._clip("\t".join(str(v) for v in vals))

    def _go_to_address_dialog(self):
        """Show an input dialog and navigate to the block containing the address."""
        dlg = tk.Toplevel(self)
        dlg.title("Go to Address")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()

        tk.Label(dlg, text="Enter address (hex):",
                 font=FONT, fg=C_TEXT, bg=C_BG).pack(padx=16, pady=(14, 4))

        entry_var = tk.StringVar()
        entry = tk.Entry(dlg, textvariable=entry_var,
                         font=FONT_B, bg="#0D1117", fg=C_HEADER,
                         insertbackground=C_HEADER, relief=tk.FLAT, width=22,
                         highlightthickness=1, highlightcolor=C_HEADER,
                         highlightbackground=C_BORDER, justify="center")
        entry.pack(padx=16, pady=4)
        entry.focus_set()

        status_var = tk.StringVar()
        status_lbl = tk.Label(dlg, textvariable=status_var,
                              font=FONT, fg="#FF6060", bg=C_BG)
        status_lbl.pack(padx=16, pady=(0, 4))

        def _do_go(*_):
            raw = entry_var.get().strip().lstrip("0x").lstrip("0X")
            try:
                addr = int(raw, 16)
            except ValueError:
                status_var.set("Invalid hex address.")
                return
            found = self.go_to_address(addr)
            if found:
                dlg.destroy()
            else:
                status_var.set(f"No block contains 0x{addr:08X}.")

        btn_frame = tk.Frame(dlg, bg=C_BG)
        btn_frame.pack(padx=16, pady=(4, 14))
        tk.Button(btn_frame, text="Go", font=FONT_B, relief=tk.FLAT,
                  bg="#1F6FEB", fg="white", padx=14, pady=4, cursor="hand2",
                  command=_do_go).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(btn_frame, text="Cancel", font=FONT, relief=tk.FLAT,
                  bg="#21262D", fg=C_TEXT, padx=10, pady=4, cursor="hand2",
                  command=dlg.destroy).pack(side=tk.LEFT)

        entry.bind("<Return>", _do_go)
        entry.bind("<KP_Enter>", _do_go)
        dlg.bind("<Escape>", lambda _: dlg.destroy())

        # Centre over parent
        self.update_idletasks()
        px = self.winfo_rootx() + self.winfo_width()  // 2
        py = self.winfo_rooty() + self.winfo_height() // 2
        dlg.update_idletasks()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        dlg.geometry(f"+{px - w // 2}+{py - h // 2}")

    def go_to_address(self, addr):
        """Select and scroll to the block that contains *addr*.

        The block that contains *addr* satisfies:
            block['addr'] <= addr < block['end_addr']

        If the current filter would hide that block, the filter is cleared
        first so the row becomes visible.  Returns True on success.
        """
        target = next(
            (b for b in self._blocks
             if b["addr"] <= addr < b["end_addr"]),
            None,
        )
        if target is None:
            return False

        # Clear any state/text filter that might hide the target block
        filter_changed = False
        if self._filter_state is not None and target["state"] != self._filter_state:
            self._filter_state = None
            self._update_tab_styles()
            filter_changed = True
        if self._filter_text:
            self._filter_var.set("")          # triggers _on_filter_change
            filter_changed = True
        if filter_changed:
            self._last_fp = ()
            self._refresh_table()

        addr_str = f"0x{target['addr']:08X}"
        iid = self._addr_to_iid.get(addr_str)
        if iid is None:
            return False

        self._tree.selection_set(iid)
        self._tree.focus(iid)
        self._tree.see(iid)
        self._on_select()          # populate detail pane
        self._selected = target["addr"]

        # Flash the row briefly so the user's eye is drawn to it
        self._tree.tag_configure("goto_flash", background="#2A4A1A")
        self._tree.item(iid, tags=(*(self._tree.item(iid, "tags") or ()), "goto_flash"))

        def _clear_flash():
            cur_tags = list(self._tree.item(iid, "tags") or ())
            if "goto_flash" in cur_tags:
                cur_tags.remove("goto_flash")
                self._tree.item(iid, tags=cur_tags)
        self.after(600, _clear_flash)

        return True

    def _clip(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        # Flash a brief confirmation in the summary bar
        prev = self._summary_var.get()
        self._summary_var.set(f"  ✓ Copied: {text[:60]}")
        self.after(1500, lambda: self._summary_var.set(prev))

    # ── CSV dump ──────────────────────────────────────────────────────────────

    def _dump_csv(self):
        ts       = time.strftime("%Y%m%d_%H%M%S")
        filename = f"heap_dump_{ts}.csv"
        out_path = os.path.join(app_dir(), filename)
        visible  = self._build_visible()
        headers  = ["#", "State", "Address", "End", "Chunk (hex)", "Used (hex)",
                    "Chunk (bytes)", "Used (bytes)", "Wasted (bytes)",
                    "Source", "Type", "Label", "Prev", "Next",
                    "Heap", "Size16", "Flags", "Self", "Notes"]
        try:
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(headers)
                for _, b, vals in visible:
                    w.writerow([
                        vals[0], vals[1], vals[2], vals[3], vals[4], vals[5],
                        b["chunk_size"], b["used_size"], b.get("unused", 0),
                        vals[6], vals[7], vals[8],
                        f"0x{b['prev']:08X}", f"0x{b['next']:08X}",
                        b.get("xenia_heap", ""),
                        f"0x{b['xenia_size16']:04X}" if "xenia_size16" in b else "",
                        f"0x{b['xenia_flags']:08X}" if "xenia_flags" in b else "",
                        f"0x{b['xenia_self_low']:08X}" if "xenia_self_low" in b else "",
                        "; ".join([n for n in
                                   list(b.get("xenia_errors", []))
                                   + [b.get("xenia_stop_reason")] if n]),
                    ])
            self._flash_dump_status(f"Saved → {filename}", ok=True)
        except OSError as e:
            self._flash_dump_status(f"Save failed: {e}", ok=False)

    def _flash_dump_status(self, msg, ok=True):
        prev = self._summary_var.get()
        self._summary_var.set(f"  {msg}")
        self.after(2500, lambda: self._summary_var.set(prev))

    # ── Tag logic ─────────────────────────────────────────────────────────────

    def load_asset_enum_names(self, path):
        names, current = {}, -1
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                in_enum = False
                for line in f:
                    stripped = line.strip()

                    if "enum asset_e" in line:
                        in_enum = True; continue
                    if in_enum and "};" in line:
                        break
                    if not in_enum:
                        continue

                    # Check for commented-out entry: // 2fb CCW Summer Leaf
                    comment_match = re.search(r"//\s*([0-9A-Fa-f]+)\s+(.+)", stripped)

                    # Strip comments for normal enum parsing
                    code = line.split("//")[0]

                    # Normal enum: SOME_NAME = 0xVAL or decimal
                    m = re.search(r"([A-Za-z_]\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", code)
                    if m:
                        name, val = m.groups()
                        current = int(val, 0); names[current] = name; continue

                    # Normal enum: SOME_NAME (no explicit value)
                    m = re.search(r"([A-Za-z_]\w+)", code)
                    if m:
                        current += 1; names[current] = m.group(1); continue

                    # Commented-out entry: // 2fb CCW Summer Leaf
                    if comment_match:
                        hex_val = int(comment_match.group(1), 16)
                        label = comment_match.group(2).strip()
                        current = hex_val
                        names[current] = f"ASSET_{comment_match.group(1).upper()} {label}"
                        continue

                    # Commented-out "// Unused" — just advance counter
                    if re.search(r"//\s*[Uu]nused", stripped):
                        current += 1

        except FileNotFoundError:
            pass
            
        return names

    def _build_bk_tag_scan_cache(self, reader):
        # BK-only: all pointer addresses and symbol tables in this method are BK-specific.

        read32 = reader.read_u32_be
        read16 = reader.read_u16_be
        read8  = reader.read_u8

        cache = []
        
        PART_EMIT_MGR_PTR_ADDR      = 0x803689B0
        PART_EMIT_MGR_LEN_ADDR      = 0x803689B4
        ASSET_CACHE_LEN_ADDR        = 0x80370A14
        ASSET_CACHE_PTR_LIST_ADDR   = 0x80383CD0
        ASSET_CACHE_ID_LIST_ADDR    = 0x80383CDC
        MAP_SAVESTATE_PTR_LIST_ADDR = 0x8037E650
        OVERLAY_MGR_LOADED_ID       = 0x80282800
        suBaddieActorArray_ADDR     = 0x8036E560

        POINTER_TAGS = [
            (0x802758E0, "s16",                    "D_802758E0 (sfx)",         "code_7090.c"),
            (0x80276580, "Gfx",                    "sGfxStack[0]",             "code_15B30.c"),
            (0x80276584, "Gfx",                    "sGfxStack[1]",             "code_15B30.c"),
            (0x80276CB8, "u16",                    "D_80276CB8",               "ml.c"),
            (0x802765B4, "void",                   "D_802765B4",               "memory.c"),
            (0x80276E30, "CoMusic",                "musicTracks",              "musicplayer.c"),
            (0x80276E44, "SnsPayload",             "snsBasePayloadPtr1",       "memory.c"),
            (0x80276E48, "SnsPayload",             "snsBasePayloadPtr2",       "memory.c"),
            (0x8027D000, "u8",                     "D_8027D000 (audio heap)",  "code_1D00.c"),
            (0x802820E0, "MusicTrack *",           "D_802820E0",               "code_11AC0.c"),
            (0x80282108, "ALBank",                 "D_80282108 (musicInstruments related)", "code_11AC0.c"), 
            (0x80282FF0, "Mtx",                    "sMtxStack[0]",             "code_15B30.c"),
            (0x80282FF4, "Mtx",                    "sMtxStack[1]",             "code_15B30.c"),
            (0x80282FF8, "Vtx",                    "sVtxStack[0]",             "code_15B30.c"),
            (0x80282FFC, "Vtx",                    "sVtxStack[1]",             "code_15B30.c"),
            
            (0x80363780, "struct5Bs",              "D_80363780",               "code_C31A0.c"),
            (0x80365DC8, "FunctionQueue",          "spawnQueue",               "spawnqueue.c"),
            (0x803689B0, "ParticleEmitter *",      "partEmitMgr",              "particle.c"),
            (0x80368AB0, "Struct_core2_6B030_0",   "D_80368AB0",               "code_6B030.c"),
            (0x80369280, "struct4Cs",              "D_80369280",               "code_72060.c"),
            (0x8036A9B4, "ActorSpawn",             "sSpawnableActorList",      "gccube.c"),
            (0x8036A9BC, "Struct_core2_7AF80_1",   "D_8036A9BC",               "gccube.c"),
            (0x8036A9C8, "Struct_core2_7AF80_1",   "D_8036A9C8",               "gccube.c"),
            (0x8036A9D4, "Struct_core2_7AF80_1",   "D_8036A9D4",               "gccube.c"),
            (0x8036A9E0, "bitfield_s",             "D_8036A9E0",               "gccube.c -> mapspecificflags.c"),
            (0x8036ABA0, "s16",                    "sProp1TotalCounts",        "gccube.c"),
            (0x8036ABA4, "s16",                    "sProp2TotalCounts",        "gccube.c"),
            (0x8036E560, "ActorArray",             "suBaddieActorArray",       "code_9E370.c"),
            (0x8036E568, "struct5Bs",              "D_8036E568",               "code_9E370.c -> code_C31A0.c"),
            (0x8036E570, "void",                   "D_8036E570",               "code_6B030.c"),
            (0x8036E7C0, "ModelCache",             "modelCache",               "code_A5BC0.c"),
            (0x8036E7C4, "u8",                     "D_8036E7C4",               "code_A5BC0.c"),
            (0x8036E7C8, "ActorMarker",            "D_8036E7C8",               "code_A5BC0.c"),
            
            (0x8037C180, "ParticleEmitter",        "gEggShatter_controller",   "eggshatter.c -> particle.c"),
            (0x8037C200, "struct0",                "D_8037C200",               "code_94A20.c"),
            (0x80371E60, "void",                   "gUnusedBlock",             "code_B7F40.c"),
            (0x80371E70, "struct56s",              "D_80371E70",               "code_B9770.c"),
            (0x80371E74, "SplineList",             "D_80371E74",               "code_B9770.c"),
            (0x803720A0, "ParticleEmitter",        "D_803720A0.unk0 (waterfall particles)", "code_C8760.c -> particle.c"),
            (0x8037BF20, "AnimCtrl",               "playerAnimCtrl",           "anim.c -> anctrl.c"),
            (0x8037D190, "Struct5Ds",              "D_8037D190",               "code_6D490.c"),
            (0x8037DDC4, "FREE_LIST(ActorMarker)",  "D_8037DDC4",            "code_42CB0.c -> fla.c"),
            (0x8037DEAC, "BKModel",                "D_8037DEAC (bottlesbonus)", "code_BB080.c"),
            (0x8037DEB8, "Struct_core2_560F0_1",   "D_8037DEB8",               "bottlesbonus.c"),
            (0x8037DEBC, "Struct_core2_560F0_1",   "D_8037DEBC",               "bottlesbonus.c"),
            (0x8037DEC0, "Struct_core2_560F0_1",   "D_8037DEC0",               "bottlesbonus.c"),
            (0x8037DEC4, "Struct_core2_560F0_1",   "D_8037DEC4",               "bottlesbonus.c"),
            (0x8037E900, "struct5DBC0s",           "D_8037E900",               "code_5DBC0.c"),
            
            (0x80380A10, "f32",                    "D_80380A10",               "code_6B030.c"),
            (0x80380AE0, "PrintBuffer",            "print_sPrintBuffer",       "print.c"),
            (0x80381030, "struct6s",               "D_80381030 (CCW rain or leaf)", "code_71820.c"),
            (0x80381034, "struct3s",               "D_80381034",               "code_70F20.c"),
            (0x80383220, "u8",                     "quizQuestionAskedBitfield", "quizquestionaskedbitfield.c"),
            (0x80382350 + 0x10, "BKModel",         "mapModel.model_opa",       "mapModel.c"),
            (0x80382350 + 0x14, "BKModel",         "mapModel.model_xlu",       "mapModel.c"),
            #(0x80382350 + 0x18, "BKModelBin",     "mapModel.model_bin_opa",   "mapModel.c"), # 0x80382350 is mapModel
            #(0x80382350 + 0x1C, "BKModelBin",     "mapModel.model_bin_xlu",   "mapModel.c"),
            (0x80382350 + 0x24, "struct5Bs",       "mapModel.unk24",           "mapModel.c -> code_C31A0.c"),
            (0x80382390, "PropModelData",          "sPropModelList",           "propModelList.c"),
            (0x80382394, "PropSpriteData",         "sPropSpriteList",          "propModelList.c"),
            (0x80382430 + 0x18, "AnimCtrl",        "s_current_transition.anctrl", "transition.c -> anctrl.c"),
            (0x80382450, "s16",                    "D_80382450",               "code_851D0.c"),
            (0x80382454, "void",                   "D_80382454",               "code_851D0.c"),
            (0x803830E0, "Struct_Core2_91E10",     "sD_803830E0",              "code_91E10.c"),
            (0x80383604, "ALBankFile",             "sfx_sound_bank (sfxInstruments)", "code_AE290.c"),
            (0x80383CC0, "AssetROMHead",           "assetSectionRomHeader",    "code_B3A80.c"),
            (0x80383CC4, "AssetFileMeta",          "assetSectionRomMetaList",  "code_B3A80.c"),
            (0x80383CD0, "void",                   "assetCachePtrList",        "code_B3A80.c"),
            (0x80383CD8, "u8",                     "assetCacheDependencyCount","code_B3A80.c"),
            (0x80383CDC, "s16",                    "assetCacheAssetIdList",    "code_B3A80.c"),
            (0x803858A0, "s16",                    "D_803858A0",               "code_B9770.c"),
            (0x803860C0, "FREE_LIST(AnimTextureList)", "AnimTextureListCache", "animtexturecache.c -> fla.c"),
            (0x80386150+0x8, "Struct_core2_C89C0_1",  "D_80386150.unk8",       "code_C89C0.c"),
            (0x80386150+0x14,"Struct_core2_C89C0_0",  "D_80386150.unk14",      "code_C89C0.c"),
            (0x803861B0+0x4, "Struct68s",          "D_803861B0.unk4",          "code_C9F00.c"),
            
            (0x803810A0, "vector(struct4Es)",              "D_803810A0",               "code_72B10.c -> vla.c"),
            (0x80383380, "VLA",                            "D_80383380.ptr",           "timedfuncqueue.c -> vla.c"),
            (0x80383550, "vector(ActorMarker)",          "D_80383550",               "code_A5BC0.c -> vla.c"),
            (0x80383554, "vector(ActorMarker)",          "D_80383554",               "code_A5BC0.c -> vla.c"),
            (0x80383570, "vector(Lighting)",               "sLightingVectorList.vector_ptr", "gclights.c -> vla.c"),
            (0x803835C0, "vector(Struct_core2_AD110_0)",   "D_803835C0",               "code_AD110.c -> vla.c"),
            (0x80383730, "AnimMtxList",                    "modelRenderAnimMtxList",   "modelRender.c"),
            (0x80383CE0, "vector(struct21s)",              "D_80383CE0[0]",            "code_AD110.c -> vla.c"),
            (0x80383CE4, "vector(struct21s)",              "D_80383CE0[1]",            "code_AD110.c -> vla.c"),
            (0x80386140+0x4, "vector(struct1Ds)",          "D_80386140.unk4",          "code_C5440.c -> vla.c"),

        ]
        
        try:
            # ── POINTER_TAGS ─────────────────────────────────────────────
            for ptr_addr, btype, label, source in POINTER_TAGS:
                ptr = read32(ptr_addr)
                if ptr:
                    cache.append((ptr, btype, label, source))

            # ── ASSETS ───────────────────────────────────────────────────
            asset_len = read8(ASSET_CACHE_LEN_ADDR) or 0
            ptr_list  = read32(ASSET_CACHE_PTR_LIST_ADDR)
            id_list   = read32(ASSET_CACHE_ID_LIST_ADDR)

            if 0 < asset_len < 255 and ptr_list and id_list:
                for i in range(asset_len):
                    ptr = read32(ptr_list + i * 4)
                    if not ptr:
                        continue
                    aid = read16(id_list + i * 2) or 0
                    label = self.asset_enum_names.get(aid, f"0x{aid:04X}")
                    cache.append((ptr, "asset", label, "code_B3A80.c"))

            # ── MAP SAVESTATES ───────────────────────────────────────────
            for i in range(0x9A):
                ptr = read32(MAP_SAVESTATE_PTR_LIST_ADDR + i * 4)
                if ptr:
                    cache.append((ptr, "MapSavestate", f"D_8037E650[{i}]", "code_5BEB0.c"))

            # ── AUDIO MANAGER ────────────────────────────────────────────
            audioManagerPtr = 0x8027bf40
            for i in range(2):
                ptr = read32(audioManagerPtr + i * 4)
                if ptr:
                    cache.append((ptr, "Acmd", f"audioManager.ACMDList[{i}]", "code_1D00.c"))

            for i in range(3):
                audioInfoPtr = read32(audioManagerPtr + (i + 2) * 4)
                if audioInfoPtr:
                    ptr = read32(audioInfoPtr)
                    if ptr:
                        cache.append((ptr, "AudioInfo", f"audioManager.audioInfo[{i}].data", "code_1D00.c"))

            # ── suBaddieActorArray (Actor struct) ───────────────────────────────────────
            base = read32(suBaddieActorArray_ADDR)
            #markers = []
            if base:
                count = read32(base) or 0
                for i in range(count):
                    base_i = base + 8 + (i * 0x180)
                    
                    ptr = read32(base_i + 0x0)
                    '''if ptr not in markers:
                        markers.append(ptr)
                    else:
                        print("Dupe Marker: " + hex(ptr))'''
                    #markerID = (read32(ptr + 0x14) >> 11) & 0x2FF
                    #print(hex(markerID))
                    if ptr:
                        ptr2 = read32(ptr + 0x48)
                        if ptr2:
                            cache.append((ptr2, "BKModel", f"suBaddieActorArray[{i}]->marker->unk48", "?"))
                        ptr2 = read32(ptr + 0x50)
                        if ptr2:
                            cache.append((ptr2, "Struct83s", f"suBaddieActorArray[{i}]->marker->unk50", "code_B9090.c"))

                    ptr = read32(base_i + 0x14)
                    if ptr:
                        cache.append((ptr, "AnimCtrl", f"suBaddieActorArray[{i}]->anctrl", "code_9E370.c -> anctrl.c"))
                        
                    ptr = read32(base_i + 0x40)
                    if ptr:
                        cache.append((ptr, "Struct25s", f"suBaddieActorArray[{i}]->unk40", "code_41460.c"))

                    ptr = read32(base_i + 0x134)
                    if ptr:
                        cache.append((ptr, "vector(AnSeqElement)*", f"suBaddieActorArray[{i}]->unk134", "anseq.c"))
                        ptr = read32(ptr)
                        if ptr:
                            cache.append((ptr, "vector(AnSeqElement)", f"*suBaddieActorArray[{i}]->unk134", "anseq.c -> vla"))
                        
                    ptr = read32(base_i + 0x148)
                    if ptr:
                        cache.append((ptr, "SkeletalAnimation", f"suBaddieActorArray[{i}]->unk148", "code_9E370.c -> skeletalanim.c")) # also VLA
                        
                        ptr2 = read32(ptr)
                        if ptr2:
                            cache.append((ptr2, "BoneTransformList", f"suBaddieActorArray[{i}]->unk148->bone_transform", "code_B3580.c"))
                        ptr2 = read32(ptr + 0x24)
                        if ptr2:
                            cache.append((ptr2, "BoneTransformList", f"suBaddieActorArray[{i}]->unk148->transition_start", "code_B3580.c"))
                        ptr2 = read32(ptr + 0x28)
                        if ptr2:
                            cache.append((ptr2, "BoneTransformList", f"suBaddieActorArray[{i}]->unk148->transition_target", "code_B3580.c"))
                        
                        
                    ptr = read32(base_i + 0x14C)
                    if ptr:
                        cache.append((ptr, "BKVertexList", f"suBaddieActorArray[{i}]->unk14C[0]", "code_9E370.c"))
                    ptr = read32(base_i + 0x14C + 4)
                    if ptr:
                        cache.append((ptr, "BKVertexList", f"suBaddieActorArray[{i}]->unk14C[1]", "code_9E370.c"))

                    ptr = read32(base_i + 0x160)
                    if ptr:
                        cache.append((ptr, "void", f"suBaddieActorArray[{i}]->unk160", "code_9D760.c"))
            
            # ── code_5DBC0 ───────────────────────────────────────────────
            unkPtr = read32(0x8037E900)
            if unkPtr:
                ptr = read32(unkPtr + 0)
                if ptr:
                    cache.append((ptr, "struct5DBC0_1s", "D_8037E900->unk0", "code_5DBC0.c"))

                ptr = read32(unkPtr + 4)
                if ptr:
                    cache.append((ptr, "struct5DBC0_2s", "D_8037E900->unk4", "code_5DBC0.c"))

                    ptr2 = read32(ptr + 8)
                    if ptr2:
                        cache.append((ptr2, "BKSpriteTextureBlock", "D_8037E900->unk4->letter_texture", "code_5DBC0.c"))

                ptr = read32(unkPtr + 8)
                if ptr:
                    cache.append((ptr, "char", "D_8037E900->string", "code_5DBC0.c"))

            # ── code_6A4B0 ───────────────────────────────────────────────
            unkPtr = read32(0x8037E8C0)
            if unkPtr:
                ptr = read32(unkPtr + 0x10)
                if ptr:
                    ptr2 = read32(ptr + 8)
                    if ptr2:
                        cache.append((ptr2, "u16", "D_8037E8C0->unk10->tmem_raw_ptr", "code_6A4B0.c"))

            # ── code_6B030 ───────────────────────────────────────────────
            unkPtr = read32(0x8036E570)
            if unkPtr:
                ptr = read32(unkPtr + 0)
                if ptr:
                    cache.append((ptr, "struct_65_s", "D_8036E570->unk0", "code_6B030.c"))

            # ── Camera nodes ─────────────────────────────────────────────
            cameraTypeSources = ["", "code_336F0.c", "code_33AB0.c", "code_33310.c", "code_33250.c"]
            for i in range(0x46):
                base = 0x8037d5e0 + i * 8
                ptr = read32(base + 4)
                if ptr:
                    cameraType = read32(base) >> 8   # top 24 bits
                    if 0 < cameraType:
                        cache.append((ptr, f"CameraNodeType{cameraType}", f"sNcCameraNodeList[{i}].data_ptr", f"cameranodelist.c -> {cameraTypeSources[cameraType]}"))
            
            # ── code_91E10 ───────────────────────────────────────────────
            unkPtr = read32(0x803830E0)
            if unkPtr:
                ptr = read32(unkPtr + 0)
                if ptr:
                    cache.append((ptr, "QuizQuestionBin", "sD_803830E0->unkC", "code_91E10.c"))
                    
            # ── ActorMarker array, (D_8036E7C8) ───────────────────────────────────────
            base = read32(0x8036E7C8)
            if base:
                for i in range(0xE0):
                    ptr = read32(base + i * 0x60 + 0x20)
                    if ptr:
                        cache.append((ptr, "AnimMtxList", f"D_8036E7C8[{i}]->unk20", "code_630D0.c"))
                    ptr = read32(base + i * 0x60 + 0x44)
                    if ptr:
                        cache.append((ptr, "struct5Bs", f"D_8036E7C8[{i}]->unk44", "code_9E370.c -> code_C31A0.c"))
                    ptr = read32(base + i * 0x60 + 0x48)
                    if ptr:
                        cache.append((ptr, "BKModel", f"D_8036E7C8[{i}]->unk48", "code_B8080.c"))
                    ptr = read32(base + i * 0x60 + 0x4C)
                    if ptr:
                        cache.append((ptr, "vector(Struct70s)", f"D_8036E7C8[{i}]->unk4C", "code_C4F40.c -> vla.c"))
                        
                        
                        
            # ── code_72060 ───────────────────────────────────────────────
            unkPtr = read32(0x80369280)
            if unkPtr:
                ptr = read32(unkPtr + 0x1C)
                if ptr:
                    cache.append((ptr, "struct4Cs", "D_80369280->unk1C (2D_light)", "code_72060.c"))
            
            # sCubeList->cubes[sCubeList.cubeCnt]->prop2Ptr (Prop *)
            base = read32(0x80381FA0)
            if base:
                cache.append((base, "Cube", f"sCubeList->cubes", "gccube.c"))
                
                ptr = read32(0x80381fA0 + 0x3C)
                if ptr:
                    cache.append((ptr, "Cube", f"sCubeList->unk3C // fallback cube?", "gccube.c"))
                    ptr2 = read32(ptr + 0x4)
                    if ptr2:
                        cache.append((ptr2, "Prop", f"sCubeList->unk3C->prop1Ptr", "gccube.c"))
                    ptr2 = read32(ptr + 0x8)
                    if ptr2:
                        cache.append((ptr2, "Prop", f"sCubeList->unk3C->prop2Ptr", "gccube.c"))
                ptr = read32(0x80381fA0 + 0x40)
                if ptr:
                    cache.append((ptr, "Cube", f"sCubeList->unk40 // some other fallback cube?", "gccube.c"))
                    ptr2 = read32(ptr + 0x4)
                    if ptr2:
                        cache.append((ptr2, "Prop", f"sCubeList->unk40->prop1Ptr", "gccube.c"))
                    ptr2 = read32(ptr + 0x8)
                    if ptr2:
                        cache.append((ptr2, "Prop", f"sCubeList->unk40->prop2Ptr", "gccube.c"))
                
                cubeCnt = read32(0x80381fA0 + 0x28)
                for i in range(cubeCnt):
                    ptr = read32(base + i * 0xC + 0x4)
                    if ptr:
                        cache.append((ptr, "Prop", f"sCubeList->cubes[{i}]->prop1Ptr", "code_A5BC0.c"))
                    ptr = read32(base + i * 0xC + 0x8)
                    if ptr:
                        cache.append((ptr, "Prop", f"sCubeList->cubes[{i}]->prop2Ptr", "code_A5BC0.c"))
            
            # more gccube.c
            base = read32(0x8036A9BC)
            if base:
                length = read32(0x8036A9B8)
                for i in range(length):
                    ptr = read32(base + i*0xC + 0x8)
                    if ptr:
                        cache.append((ptr, "Struct_core2_7AF80_2", f"D_8036A9BC[{i}].unk8", "gccube.c"))
            base = read32(0x8036A9C8)
            if base:
                length = read32(0x8036A9C4)
                for i in range(length):
                    ptr = read32(base + i*0xC + 0x8)
                    if ptr:
                        cache.append((ptr, "Struct_core2_7AF80_2", f"D_8036A9C8[{i}].unk8", "gccube.c"))
            base = read32(0x8036A9D4)
            if base:
                length = read32(0x8036A9D0)
                for i in range(length):
                    ptr = read32(base + i*0xC + 0x8)
                    if ptr:
                        cache.append((ptr, "Struct_core2_7AF80_2", f"D_8036A9D4[{i}].unk8", "gccube.c"))
            
            # code_B9770
            base = read32(0x80371E70)
            if base:
                length = read32(0x80371E78)
                for i in range(length):
                    ptr = read32(base + i*4)
                    if ptr:
                        cache.append((ptr, "struct56s", f"D_80371E70[{i}]", "code_B9770.c"))
            base = read32(0x80371E74)
            if base:
                length = read32(0x80371E78)
                for i in range(length):
                    ptr = read32(base + i*4)
                    if ptr:
                        cache.append((ptr, "SplineList", f"D_80371E74[{i}]", "code_B9770.c"))
            
            
            # D_80379E20 / boneTransformList / code_B3580.c / size 340 / each entry is 8 bytes
            for i in range(340):
                ptr = read32(0x80379E20 + i * 8 + 0)
                if ptr:
                    cache.append((ptr, "BoneTransformList", f"D_80379E20[{i}].bone_xform", "code_B3580.c"))
            
            # gcSky
            for i in range(3):
                ptr = read32(0x80382410 + 4 + i * 4)
                if ptr:
                    cache.append((ptr, "BKModel", f"gcSky.model[{i}]", "code_B3580.c"))
                    
            # musicTracks[i]->unk18
            base = read32(0x80276E30)
            if base:
                for i in range(6):
                    ptr = read32(base + 0x54 * i + 0x18)
                    if ptr:
                        cache.append((ptr, "FREE_LIST(struct12s)", f"musicTracks[{i}]->unk18", "musicplayer.c -> fla.c"))
            
            # print.c
            for i in range(4):
                ptr = read32(0x80380ad0 + 0x4 * i)
                if ptr:
                    cache.append((ptr, "FontLetter ", f"print_sFonts[{i}]", "print.c"))
            
            # vla.c
            base = read32(0x80381034)
            if base:
                ptr = read32(base + 0x20)
                if ptr:
                    cache.append((ptr, "vector(struct struct_4_s)", f"D_80381034->unk20", "code_70F20.c -> vla.c"))
                    
            # BKSpriteDisplayData, code_B3A80.c -> code_BD100.c
            base = read32(0x80383CD4)
            if base:
                cache.append((base, "BKSpriteDisplayData *", f"D_80383CD4", "code_B3A80.c"))
                for i in range(150):
                    ptr = read32(base + i * 4)
                    if ptr:
                        cache.append((ptr, "BKSpriteDisplayData", f"D_80383CD4[{i}]", "code_B3A80.c -> code_BD100.c"))
                        
            
            # ── Particle emitters ────────────────────────────────────────
            emitter_array_ptr = read32(PART_EMIT_MGR_PTR_ADDR)
            emitter_count     = reader.read_s32_be(PART_EMIT_MGR_LEN_ADDR) or 0
            if emitter_array_ptr and 0 < emitter_count <= 512:
                # The array block itself
                cache.append((
                    emitter_array_ptr,
                    "ParticleEmitter *",
                    f"particle_mgr[{emitter_count}]",
                    "particle.c",
                ))
                for i in range(emitter_count):
                    emitter_ptr = read32(emitter_array_ptr + i * 4)
                    if not emitter_ptr or not (0x80000000 <= emitter_ptr < 0x80800000):
                        continue
                    auto_free = read8(emitter_ptr + 1) or 0
                    dead      = read8(emitter_ptr + 2) or 0
                    p_start   = read32(emitter_ptr + 0x124)
                    p_end     = read32(emitter_ptr + 0x128)
                    p_count   = (
                        (p_end - p_start) // 0x60
                        if p_start and p_end and p_end >= p_start else 0
                    )
                    status = "(dead)" if dead == 1 else ("(auto)" if auto_free == 1 else "")
                    cache.append((
                        emitter_ptr,
                        "ParticleEmitter",
                        f"Emitter[{i}] ({p_count} particles) {status}",
                        "particle.c",
                    ))
            
            # zoomboxes
            # Struct_Core2_91E10 *sD_803830E0;
            base = read32(0x803830E0)
            if base:
                for i in range(4):
                    ptr = read32(base + 0x24 + i*4)
                    if ptr:
                        cache.append((ptr, "GcZoombox", f"D_803830E0->zoomboxes[{i}]", "code_91E10.c -> zoombox.c"))
                        
                        ptr2 = read32(ptr + 0xF4)
                        if ptr2:
                            cache.append((ptr2, "AnimCtrl", f"D_803830E0->zoomboxes[{i}].anim_ctrl", "zoombox.c -> anctrl.c"))
            
            
            base = 0x80382E20
            for i in range(2):
                ptr = read32(base + 0x104 + i*4)
                if ptr:
                    cache.append((ptr, "BKDialog", f"g_Dialog.dialog[{i}]", "dialog.c"))
            
            ptr = read32(0x80382E20+0x11C)
            if ptr:
                cache.append((ptr, "GcZoombox", "g_Dialog.zoombox[0]", "dialog.c -> zoombox.c"))
                ptr2 = read32(ptr + 0xF4)
                if ptr2:
                    cache.append((ptr2, "AnimCtrl", f"g_Dialog.zoombox[0].anim_ctrl", "zoombox.c -> anctrl.c"))
            ptr = read32(0x80382E20+0x120)
            if ptr:
                cache.append((ptr, "GcZoombox", "g_Dialog.zoombox[1]", "dialog.c -> zoombox.c"))
                ptr2 = read32(ptr + 0xF4)
                if ptr2:
                    cache.append((ptr2, "AnimCtrl", f"g_Dialog.zoombox[1].anim_ctrl", "zoombox.c -> anctrl.c"))
            ptr = read32(0x8037dcf0)
            if ptr:
                cache.append((ptr, "GcZoombox", "chGameSelectTopZoombox", "gameSelect.c -> zoombox.c"))
                ptr2 = read32(ptr + 0xF4)
                if ptr2:
                    cache.append((ptr2, "AnimCtrl", f"chGameSelectTopZoombox.anim_ctrl", "zoombox.c -> anctrl.c"))
            ptr = read32(0x8037dcf4)
            if ptr:
                cache.append((ptr, "GcZoombox", "chGameSelectBottomZoombox", "gameSelect.c -> zoombox.c"))
                ptr2 = read32(ptr + 0xF4)
                if ptr2:
                    cache.append((ptr2, "AnimCtrl", f"chGameSelectBottomZoombox.anim_ctrl", "zoombox.c -> anctrl.c"))
            
            # D_80383010
            for i in range(4):
                ptr = read32(0x80383010 + 0x10 + i*4)
                if ptr:
                    cache.append((ptr, "GcZoombox", f"D_80383010->zoombox[{i}]", "pauseMenu.c -> zoombox.c"))
                    ptr2 = read32(ptr + 0xF4)
                    if ptr2:
                        cache.append((ptr2, "AnimCtrl", f"D_80383010->zoomboxes[{i}].anim_ctrl", "zoombox.c -> anctrl.c"))
            
            
        except Exception as e:
            print(e)
        
        #for r in cache:
            #print(hex(cache[0]) + ", " + cache[1] + ", " + cache[2] + ", " + cache[3])
            #print("{} {} {} {}".format(r))
        return cache
        
    def _build_bt_tag_scan_cache(self, reader):
        # BT-only: all pointer addresses and symbol tables in this method are BT-specific.

        read32 = reader.read_u32_be
        read16 = reader.read_u16_be
        read8  = reader.read_u8

        def read_string(addr, length, max_len=64):
            """Read a fixed-length (non-null-terminated-in-general) byte
            string out of N64 memory and decode it for display, stopping
            early at the first NUL if one is present. length comes from a
            single byte in the overlay header, so it's capped defensively
            in case of a bad/garbage read."""
            if not addr or not length:
                return ""
            n = max(0, min(int(length), max_len))
            if n == 0:
                return ""
            data = reader.read_n64(addr, n)
            if not data:
                return ""
            nul = data.find(b"\x00")
            if nul != -1:
                data = data[:nul]
            return data.decode("latin-1", errors="replace")

        cache = []
        
        
        POINTER_TAGS = [
            (0x80135490, "Player Object",            "Player Object",             ""),
            (0x801289D0, "Text Buffer",              "Text Buffer",               ""),
            (0x8012C770, "Flag Block",               "Flag Block",                ""),
            (0x80136EE0, "ActorArray",               "Actor Array",               ""),
            (0x80132DB0, "Actor-related Array (?)",  "Actor-related Array (?)",   ""),
        ]
        
        try:
            # ── POINTER_TAGS ─────────────────────────────────────────────
            for ptr_addr, btype, label, source in POINTER_TAGS:
                ptr = read32(ptr_addr)
                if ptr:
                    cache.append((ptr, btype, label, source))
                    
            '''ptr = read32(0x80382E20+0x11C)
            if ptr:
                cache.append((ptr, "GcZoombox", "g_Dialog.zoombox[0]", "dialog.c -> zoombox.c"))'''
            a = 1
            
            # ── ASSETS ───────────────────────────────────────────────────
            asset_len = 0x80
            ptr_list  = 0x8012B450
            id_list   = 0x8012B6E0
            
            #if 0 < asset_len < 255 and ptr_list : # and id_list
            for i in range(asset_len):
                #print(hex(ptr_list + i * 4))
                ptr = read32(ptr_list + i * 4)
                if not ptr:
                    continue
                aid = read16(id_list + i * 2) or 0
                
                if aid in BT_ANIM_ASSETS:
                    label = BT_ANIM_ASSETS.get(aid, f"0x{aid:04X}")
                else:
                    name = BT_ASSETS[aid]["name"]
                    label = "Asset "+f"{aid:04X}: "+name
                #else:
                #    label = self.asset_enum_names_bt.get(aid, f"0x{aid:04X}")
                    
                cache.append((ptr, "asset", label, ""))
            
            
            # ── OVERLAYS ───────────────────────────────────────────────────
            overlay_len = 0x80
            ptr_list  = 0x80126738
            
            for i in range(overlay_len):
                #print(hex(ptr_list + i * 4))
                ptr = read32(ptr_list + i * 4)
                if not ptr:
                    continue

                num_entrypoints = read16(ptr + 0x8)
                name_length     = read8(ptr + 0xE)
                name_addr = ptr + 0x10 + 0x28 + (num_entrypoints * 4)
                name = read_string(name_addr, name_length)
                
                cache.append((ptr, "overlay", name, ""))
            
            
        except Exception as e:
            print(e)
            
        #for r in cache:
            #print(hex(cache[0]) + ", " + cache[1] + ", " + cache[2] + ", " + cache[3])
            #print("{} {} {} {}".format(r))
        return cache
        
    def _build_bt_xenia_tag_scan_cache(self, reader):
        # BT-Xenia-only: all pointer addresses and symbol tables in this method are BT-Xenia-specific.

        read32 = reader.read_u32_be
        read16 = reader.read_u16_be
        read8  = reader.read_u8

        cache = []
        
        POINTER_TAGS = [
            (0x1826A2BCC, "ActorArray",               "Actor Array",               ""),
            (0x1826A29A0, "Player Object",            "Player Object",             ""),
        ]
        
        try:
            # ── POINTER_TAGS ─────────────────────────────────────────────
            for ptr_addr, btype, label, source in POINTER_TAGS:
                raw = read32(ptr_addr)
                if not raw:
                    continue
                ptr = raw + 0x100000000
                cache.append((ptr, btype, label, source))
            
            
        except Exception as e:
            print(e)
        
        #print(cache)
        return cache
        
    def _build_bk_xenia_tag_scan_cache(self, reader):
        # BK-Xenia-only: all pointer addresses and symbol tables in this method are BK-Xenia-specific.

        read32 = reader.read_u32_be
        read16 = reader.read_u16_be
        read8  = reader.read_u8

        cache = []
        
        POINTER_TAGS = [
            #(0x80135490, "Player Object",            "Player Object",             ""),
            (0x18249F68C, "ActorArray",               "Actor Array",               ""),
        ]
        
        try:
            # ── POINTER_TAGS ─────────────────────────────────────────────
            for ptr_addr, btype, label, source in POINTER_TAGS:
                raw = read32(ptr_addr)
                if not raw:
                    continue
                ptr = raw + 0x100000000
                cache.append((ptr, btype, label, source))
            
            
        except Exception as e:
            print(e)
            
        return cache
        
    def tag_block(self, block, reader):
        def contains(ptr):
            if self._profile.id in ("bk", "bt"):
                header_size = 0x10
            elif "xenia_hdr_size" in block:
                # Xenia allocator nodes come in two shapes: tracked (0x60
                # header) and untracked (0x10).  The walker records which, so
                # use that rather than assuming one size for the whole heap.
                # Both Kazooie and Tooie use this allocator.
                header_size = block["xenia_hdr_size"]
            else:
                header_size = 0x40

            return ptr and (block["addr"] + header_size) == ptr

        try:
            if block["state"] == HEAP_STATE_EMPTY:
                return "free", "", ""

            # Symbol table tagging
            if self._profile is not None and self._profile.id not in ("bk", "bt", "xenia_bt", "xenia_bk"):
                return "unknown", "", ""

            # Xenia profiles: no symbol tables yet — tag by payload length heuristics only
            '''if self._profile is not None and self._profile.id in ("xenia_bt", "xenia_bk"):
                payload_len = block.get("xenia_data_len", block.get("used_size", 0))
                if payload_len == 0:
                    return "free", "", ""
                return "used", f"payload={payload_len:#x}", ""'''

            # All pointer lookups — including particle emitters — are now
            # precomputed in _build_bk_tag_scan_cache/_build_bt_tag_scan_cache, so this is a single pass.
            for ptr, btype, label, source in self._tag_scan_cache:
                if contains(ptr):
                    return btype, label, source
            
            if self._profile.id == "bk":
                if block["addr"] == 0x8002D500:
                    return "EmptyHeapBlock", f"D_8002D500", "memory.c"
                elif block["addr"] == 0x8023DA00:
                    return "EmptyHeapBlock", f"D_8023DA00", "memory.c"
                elif block["addr"] == 0x800659D0:
                    return "soundfont", f"soundfont1ctl (sfxInstruments)", "code_AE290.c -> pidma.c"
                elif block["addr"] == 0x800767D0:
                    return "soundfont", f"soundfont2ctl (musicInstruments)", "code_11AC0.c -> pidma.c"
            elif self._profile.id == "bt":
                if block["addr"] == 0x80137800:
                    return "EmptyHeapBlock", f"", ""
                elif block["addr"] == 0x803FFFE0:
                    return "EmptyHeapBlock", f"", ""
                elif block["chunk_size"] == 0x1110:
                    return "BoneTransformList", f"", ""
            elif self._profile.id == "xenia_bt":
                if block["used_size"] == 0xEF0:
                    return "Actor related array?", f"", ""
                elif block["used_size"] == 0x1110:
                    return "BoneTransformList", f"", ""

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Failed to tag block {hex(block['addr'])}. Exception: {e}")

        return "unknown", "", ""
