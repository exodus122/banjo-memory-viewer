"""
actors_view.py - Live ActorArray viewer for Banjo-Kazooie / Banjo-Tooie.

Reads an ActorArray struct from a fixed N64 address per game profile:
  typedef struct actor_array {
      s32 cnt;
      s32 max_cnt;
      Actor data[];       // each Actor is 0x180 bytes
  } ActorArray;

For each live Actor slot the viewer decodes the key fields from the
Actor struct and the ActorMarker it points to, then displays them in a
sortable, filterable Treeview — consistent with the HeapView style.

Actor struct layout (big-endian N64, offsets in hex):
  0x00  ActorMarker* marker        → pointer to ActorMarker
  0x04  f32 position[3]            → x, y, z  (0x04, 0x08, 0x0C)
  0x10  u32 packed_flags           → state bits
  0x50  f32 yaw
  0x68  f32 pitch
  0x110 f32 roll
  0x128 f32 scale
  0xF4  u32 packed_flags2          → initialized flag (bit 10 from top = bit 21)

ActorMarker layout (big-endian N64):
  0x14  u32 packed — id field is bits [20:11] (10 bits)
        full word: yaw:9 | unk14_22:1 | unk14_21:1 | id:10 | unk14_10:11
  0x3E  u16 packed — modelId bits [13:1] (13 bits)
"""

import csv
import os
import re
import struct
import time
import tkinter as tk
from tkinter import ttk
from bt_assets import BT_ASSETS, BT_ANIM_ASSETS
from app_paths import app_dir, resource_path

# ── Colours (same palette as the rest of the app) ─────────────────────────────
C_BG      = "#0D1117"
C_PANEL   = "#161B22"
C_BORDER  = "#21262D"
C_HEADER  = "#00FF88"
C_TEXT    = "#C9D1D9"
C_DIM     = "#667788"
C_ADDR    = "#58A6FF"
C_LIVE    = "#FFC080"   # active actor row
C_DEAD    = "#444C56"   # slot exists but not "initialized"
C_SEL_BG  = "#1F2D3D"

FONT   = ("Courier New", 9)
FONT_B = ("Courier New", 9, "bold")

# ── Per-game actor layout configs ─────────────────────────────────────────────
#
# Each entry describes how to parse one Actor slot and the ActorArray header.
#
#   actor_size        : bytes per Actor slot
#   array_data_off    : byte offset from ActorArray struct base to data[]
#                       BK: 8  (cnt s32 + max_cnt s32)
#                       BT: 16 (cnt s32 + first* + unk* + end*)
#   off_marker_ptr    : offset of the leading pointer (ActorMarker* / Unk80132ED0*)
#   off_pos           : offset of f32 position[3]
#   off_yaw           : offset of yaw f32  (None if not present)
#   off_pitch         : offset of pitch f32 (None if not present)
#   off_roll          : offset of roll f32  (None if not present)
#   off_scale         : offset of scale f32
#   off_state_word    : offset of the packed u32 that contains state (None = N/A)
#   state_shift/mask  : how to extract state from that word
#   off_init_word     : offset of u32 containing "initialized" flag (None = use marker ptr != 0)
#   init_shift/mask   : how to extract initialized from that word
#   read_marker       : whether to follow marker_ptr to read id/modelId
#   mkr_id_off        : ActorMarker offset for the packed id word
#   mkr_id_shift/mask : how to extract actor id
#   mkr_model_off     : ActorMarker offset for the packed modelId halfword
#   mkr_model_shift/mask

_BK_LAYOUT = dict(
    actor_size      = 0x180,
    array_data_off  = 8,          # cnt(4) + max_cnt(4)
    off_marker_ptr  = 0x00,
    off_pos         = 0x04,       # f32[3]: x=0x04 y=0x08 z=0x0C
    off_yaw         = 0x50,
    off_pitch       = 0x68,
    off_roll        = 0x110,
    off_scale       = 0x128,
    off_state_word  = 0x10,
    state_shift     = 26,
    state_mask      = 0xFC000000,
    off_despawn_word = None, #0x44,
    despawn_shift   = 3,
    despawn_mask    = 0x8,
    off_init_word   = None, #0xF4,
    init_shift      = 12,
    init_mask       = 0x1000,
    read_marker     = True,
    mkr_id_off      = 0x14,
    mkr_id_shift    = 11,
    mkr_id_mask     = 0x1FF800,
    mkr_model_off   = 0x3E,       # u16
    mkr_model_shift = 2,
    mkr_model_mask  = 0x7FFC,
    mkr_read_size   = 0x60,       # bytes to read from ActorMarker
)

_BT_LAYOUT = dict(
    actor_size      = 0x9C,
    # BT ActorArray header layout:
    #   0x00  u32    actor_size   (= 0x9C, sanity-check value)
    #   0x04  Actor* first        → pointer to first (oldest used) actor slot
    #   0x08  Actor* first_free   → pointer to first free slot;
    #                               slots in [first_free, end) hold garbage data
    #   0x0C  Actor* end          → pointer one-past the last slot
    #
    # live_cnt  = (first_free - first) / actor_size
    # total_cnt = (end        - first) / actor_size
    array_data_off  = None,       # BT uses pointer arithmetic, not a fixed offset
    off_marker_ptr  = 0x00,       # Unk80132ED0* — BT equivalent of ActorMarker*
    off_pos         = 0x04,       # f32[3]: x=0x04 y=0x08 z=0x0C
    off_pitch       = 0x44,       # rotation[0]
    off_yaw         = 0x48,       # rotation[1]
    off_roll        = 0x4C,       # rotation[2]
    off_scale       = 0x38,
    off_state_word  = None,       # no clean state field
    state_shift     = 0,
    state_mask      = 0,
    off_despawn_word = None,
    despawn_shift   = 0,
    despawn_mask    = 0,
    off_init_word   = None,       # use marker_ptr != 0 as proxy
    init_shift      = 0,
    init_mask       = 0,
    read_marker     = True,       # follow Unk80132ED0* to read model id
    # Unk80132ED0 layout:
    #   0x14  u16 unk14  — model id (full u16, no bit extraction needed)
    #   0x16  u16 unk16
    # No actor id field identified yet — leave as 0.
    mkr_id_off      = 0,          # no actor id field known yet
    mkr_id_shift    = 0,
    mkr_id_mask     = 0,
    mkr_model_off   = 0x14,       # u16 at offset 0x14
    mkr_model_shift = 0,          # whole u16, no bit extraction
    mkr_model_mask  = 0xFFFF,
    mkr_read_size   = 0x20,       # only need up to 0x16 + some slack
)

# The XBLA port's Actor struct is 0x184 bytes, 4 more than the N64's 0x180, and
# the extra word sits mid-struct rather than at the end: marker_ptr, pos and the
# state word all read correctly, while yaw/pitch/roll/scale do not.
#
# Whether the word was inserted at 0x40 or at 0x44 cannot be told apart from the
# data, but it does not matter — no field sits at 0x40, so under either reading
# everything from 0x44 onward moves +4 and everything below it stays put.
#
# Offsets are therefore listed explicitly rather than derived, so a future edit
# to _BK_LAYOUT can't silently half-apply here.
_XENIA_BK_LAYOUT = dict(
    _BK_LAYOUT,
    actor_size       = 0x184,
    # unchanged (below the inserted word):
    #   off_marker_ptr 0x00, off_pos 0x04, off_state_word 0x10
    off_despawn_word = None, #0x48,      # N64 0x44
    off_yaw          = 0x54,      # N64 0x50
    off_pitch        = 0x6C,      # N64 0x68
    off_init_word    = None, #0xF8,      # N64 0xF4
    off_roll         = 0x114,     # N64 0x110
    off_scale        = 0x12C,     # N64 0x128
)

GAME_LAYOUTS = {
    "bk":       _BK_LAYOUT,
    "bt":       _BT_LAYOUT,
    "xenia_bk": _XENIA_BK_LAYOUT,
    "xenia_bt": _BT_LAYOUT,
}

# ── Column definitions ─────────────────────────────────────────────────────────
# (id, label, width, anchor, stretch)
COLS = [
    ("#",           "#",          38,  "center", False),
    ("Addr",        "Addr",       90,  "center", False),
    ("MarkerPtr",   "Marker",     90,  "center", False),
    ("MarkerID",    "MarkerID",   80,  "center", False),
    ("MarkerName",  "MarkerName", 200, "w",      False),
    ("ModelID",     "ModelID",    62,  "center", False),
    ("ModelName",   "ModelName",  200, "w",      False),
    ("State",       "State",      44,  "center", False),
    ("PosX",        "Pos X",      80,  "center", False),
    ("PosY",        "Pos Y",      80,  "center", False),
    ("PosZ",        "Pos Z",      80,  "center", False),
    ("Yaw",         "Yaw",        56,  "center", False),
    ("Pitch",       "Pitch",      56,  "center", False),
    ("Roll",        "Roll",       56,  "center", False),
    ("Scale",       "Scale",      60,  "center", False),
]
COL_IDS = [c[0] for c in COLS]


def _f32(data, off):
    """Read a big-endian IEEE-754 float from bytes at offset."""
    if off + 4 > len(data):
        return 0.0
    return struct.unpack_from(">f", data, off)[0]


def _u32(data, off):
    if off + 4 > len(data):
        return 0
    return struct.unpack_from(">I", data, off)[0]


def _u16(data, off):
    if off + 2 > len(data):
        return 0
    return struct.unpack_from(">H", data, off)[0]


def _parse_actor(raw, base_addr, layout):
    """
    Parse one Actor slot using the given game layout dict.
    Returns a dict of decoded fields, or None if the slice is too short.
    """
    if len(raw) < layout["actor_size"]:
        return None

    marker_ptr = _u32(raw, layout["off_marker_ptr"])
    pos_x = _f32(raw, layout["off_pos"])
    pos_y = _f32(raw, layout["off_pos"] + 4)
    pos_z = _f32(raw, layout["off_pos"] + 8)
    yaw   = _f32(raw, layout["off_yaw"])   if layout["off_yaw"]   is not None else 0.0
    pitch = _f32(raw, layout["off_pitch"]) if layout["off_pitch"] is not None else 0.0
    roll  = _f32(raw, layout["off_roll"])  if layout["off_roll"]  is not None else 0.0
    scale = _f32(raw, layout["off_scale"])

    if layout["off_state_word"] is not None:
        state = (_u32(raw, layout["off_state_word"]) & layout["state_mask"]) >> layout["state_shift"]
    else:
        state = 0

    if layout["off_init_word"] is not None:
        initialized = (_u32(raw, layout["off_init_word"]) & layout["init_mask"]) >> layout["init_shift"]
    else:
        # Use marker_ptr != 0 as a proxy for "slot is live"
        #initialized = 1 if marker_ptr else 0
        initialized = 0
        
    if layout["off_despawn_word"] is not None:
        despawned = (_u32(raw, layout["off_despawn_word"]) & layout["despawn_mask"]) >> layout["despawn_shift"]
    else:
        # Use marker_ptr != 0 as a proxy for "slot is live"
        #despawned = 0 if marker_ptr else 1
        despawned = 0
    
    return {
        "addr":        base_addr,
        "marker_ptr":  marker_ptr,
        "pos_x":       pos_x,
        "pos_y":       pos_y,
        "pos_z":       pos_z,
        "state":       state,
        "initialized": initialized,
        "despawned":   despawned,
        "yaw":         yaw,
        "pitch":       pitch,
        "roll":        roll,
        "scale":       scale,
        "marker_id":    0,
        "model_id":    0,
    }


def _parse_marker_fields(marker_raw, layout):
    """Extract marker_id and model_id from a raw ActorMarker blob."""
    mkr_id_off = layout["mkr_id_off"]
    if len(marker_raw) < mkr_id_off + 4:
        return 0, 0
    packed_id  = _u32(marker_raw, mkr_id_off)
    marker_id   = (packed_id & layout["mkr_id_mask"]) >> layout["mkr_id_shift"]

    mkr_mod_off = layout["mkr_model_off"]
    if len(marker_raw) < mkr_mod_off + 2:
        return marker_id, 0
    packed_mod = _u16(marker_raw, mkr_mod_off)
    model_id   = (packed_mod & layout["mkr_model_mask"]) >> layout["mkr_model_shift"]

    return marker_id, model_id


class _LockedScrollbar(ttk.Scrollbar):
    """Keep the thumb a stable size while the user drags it, without adding
    input lag.

    Background (found through a lot of isolated testing, see
    scrollbar_repro_test*.py): with a wide, many-column Treeview (Actors has
    17 columns), rapidly re-scrolling it during a fast thumb-drag makes the
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


class ActorsView(tk.Frame):
    """
    Tab that reads one or more ActorArray structs discovered dynamically
    from the heap view, and displays each Actor's key fields in a
    sortable, filterable Treeview.

    Public API
    ----------
    set_profile(profile)              — called on game switch
    notify_actor_arrays(arrays)       — {label: heap_node_addr} from heap view
    update_actors(reader)             — read + refresh (call every poll tick)
    set_no_data(msg)                  — show a placeholder message
    set_array_addr(addr)              — manual address override
    """

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C_BG, **kw)
        self._array_addr     = None   # resolved struct address (changes per level)
        self._array_pointers = {}     # label → static ptr addr (from profile)
        self._array_addrs    = {}     # kept for compat
        self._layout         = _BK_LAYOUT  # active game layout
        self._actors      = []     # list of parsed actor dicts
        self._profile     = None
        self._profile_id  = "bk"

        # Enum name lookups (populated from enums.h and bt_assets)
        self._marker_enum_names: dict = self._load_enum_names(resource_path("enums.h"), "marker_e")
        self._asset_enum_names:  dict = self._load_enum_names(resource_path("enums.h"), "asset_e")

        # Marker read cache: marker_ptr → (marker_id, model_id).
        # Avoids a separate ReadProcessMemory call per actor per frame once
        # the level is stable.  Cleared on profile/level switch.
        self._marker_cache: dict = {}

        # Self-driven poll loop (mirrors WatchesView) so this view refreshes
        # at its own faster cadence instead of only whenever the shared
        # ~5 fps app poll tick happens to land while this tab is visible.
        self._reader        = None
        self._polling       = False
        self._poll_interval_ms = 33   # ~30 fps
        self._is_visible    = lambda: True
        # Tracks whether this tab was visible on the previous poll tick, so
        # the very first update after it *becomes* visible can be deferred
        # by one Tk idle pass - see _poll_loop().
        self._was_visible   = False

        # Incremental tree state
        self._addr_to_iid: dict = {}
        self._render_cache: dict = {}
        # Ordered list of addrs from the previous render pass, used to detect
        # whether any tree.move() calls are needed without an O(n) Tk round
        # trip (tree.get_children()) every frame.
        self._last_iid_order: list = []
        # Fingerprint of the last rendered visible set: (count, (addr, hash(vals)), ...).
        # If unchanged since last frame, skip all Tk work entirely.
        self._last_fp: tuple = ()

        self._sort_col  = "#"
        self._sort_asc  = True
        self._filter_text = ""

        # Right-click context menu target iid
        self._ctx_menu_iid = None

        self._build_ui()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_profile(self, profile):
        """Switch game profile; loads static pointer addresses and updates title."""
        if profile is self._profile:
            return
        self._profile = profile
        self._profile_id = getattr(profile, "id", "bk")
        # Static pointer addresses from the profile — each holds a u32 that is
        # the current heap address of an ActorArray.  Dereferenced each tick.
        self._array_pointers = dict(getattr(profile, "actor_array_pointers", {}))
        self._array_addrs = {}
        self._array_addr = None
        labels = list(self._array_pointers.keys())
        self._array_combo.configure(values=labels)
        if labels:
            self._array_combo_var.set(labels[0])
        else:
            self._array_combo_var.set("")
        self._layout = GAME_LAYOUTS.get(getattr(profile, "id", "bk"), _BK_LAYOUT)
        self._marker_cache.clear()
        self._last_iid_order = []
        self._last_fp = ()
        self._clear()
        self._update_addr_hint()

    def set_array_addr(self, addr):
        """Override the ActorArray N64 address at runtime."""
        self._array_addr = addr
        self._addr_var.set(f"0x{addr:08X}")
        self._update_addr_hint()
        self._clear()

    def _on_array_select(self, _=None):
        label = self._array_combo_var.get()
        if label in self._array_addrs:
            self._array_addr = self._array_addrs[label]
            self._update_addr_hint()
            self._clear()

    def _update_addr_hint(self):
        label = self._array_combo_var.get()
        ptr_addr = self._array_pointers.get(label)
        if ptr_addr and self._array_addr:
            self._addr_hint_var.set(
                f"ptr @ 0x{ptr_addr:X}  →  struct @ 0x{self._array_addr:X}")
        elif ptr_addr:
            self._addr_hint_var.set(f"ptr @ 0x{ptr_addr:X}  →  (not yet resolved)")
        else:
            self._addr_hint_var.set("No ActorArray configured for this profile")

    def update_actors(self, reader):
        """Read the ActorArray from N64 RAM and refresh the table."""
        # Resolve the selected array name to a static pointer address,
        # then dereference it to get the current (dynamic) heap address.
        label = self._array_combo_var.get()
        ptr_addr = self._array_pointers.get(label)
        if ptr_addr is None:
            self.set_no_data("No ActorArray pointer configured for this profile.")
            return

        is_xenia = self._profile_id.startswith("xenia_")

        raw_ptr = reader.read_u32_be(ptr_addr)

        if is_xenia:
            # Xenia BT: the static pointer holds a raw 32-bit physical address
            # (e.g. 0x00420040 — already past the 0x40 heap node header).
            # Add XENIA_PHYS_BASE (0x100000000) to get the host address of the
            # ActorArray struct.  The value 0 means the array isn't loaded yet.
            if not raw_ptr:
                self.set_no_data(f"ActorArray pointer at 0x{ptr_addr:X} is null.")
                return
            heap_node_addr = raw_ptr + 0x100000000
        else:
            # BizHawk / N64: the pointer holds an N64 virtual address
            # in the RDRAM window 0x80000000–0x807FFFFF.
            heap_node_addr = raw_ptr
            if not heap_node_addr or not (0x80000000 <= heap_node_addr <= 0x807FFFFF):
                self.set_no_data(f"ActorArray pointer at 0x{ptr_addr:08X} is null or invalid.")
                return

        # If the ActorArray has moved (level reload), flush the marker cache so
        # stale labels from the previous level don't bleed onto new actors that
        # happen to share the same marker_ptr values.
        if heap_node_addr != self._array_addr:
            self._marker_cache.clear()

        self._array_addr = heap_node_addr
        self._update_addr_hint()

        # The pointer stored at ptr_addr points directly to the ActorArray
        # struct data (already past the heap node header).
        array_base = heap_node_addr

        layout     = self._layout
        actor_size = layout["actor_size"]

        if layout["array_data_off"] is None:
            # ── BT pointer-based layout ───────────────────────────────────────
            # Header: actor_size(4) | first*(4) | first_free*(4) | end*(4)
            hdr = reader.read_n64(array_base, 16)
            if not hdr or len(hdr) < 16:
                self.set_no_data("Could not read ActorArray header.")
                return
            first_ptr      = struct.unpack_from(">I", hdr, 4)[0]
            first_free_ptr = struct.unpack_from(">I", hdr, 8)[0]
            end_ptr        = struct.unpack_from(">I", hdr, 12)[0]

            if is_xenia:
                # Xenia BT: header pointers are raw 32-bit physical addresses;
                # add XENIA_PHYS_BASE to convert them to host addresses.
                if not first_ptr or not end_ptr or end_ptr < first_ptr:
                    self.set_no_data(
                        f"Xenia BT ActorArray pointers look invalid "
                        f"(first=0x{first_ptr:08X} end=0x{end_ptr:08X}).")
                    return
                first_ptr      += 0x100000000
                first_free_ptr += 0x100000000
                end_ptr        += 0x100000000
            else:
                if not (0x80000000 <= first_ptr <= 0x807FFFFF) or \
                   not (0x80000000 <= end_ptr   <= 0x807FFFFF) or \
                   end_ptr < first_ptr:
                    self.set_no_data(
                        f"BT ActorArray pointers look invalid "
                        f"(first=0x{first_ptr:08X} end=0x{end_ptr:08X}).")
                    return

            # first_free may equal end (all slots used) or first (none used).
            # Clamp it to [first_ptr, end_ptr] defensively.
            if not (first_ptr <= first_free_ptr <= end_ptr):
                first_free_ptr = end_ptr
            data_addr  = first_ptr
            max_cnt    = (end_ptr        - first_ptr) // actor_size
            cnt        = (first_free_ptr - first_ptr) // actor_size
            # Byte offset within the slot array at which free/garbage slots begin
            free_start_off = first_free_ptr - first_ptr
        else:
            # ── BK fixed-offset layout ────────────────────────────────────────
            # Header: cnt(4) + max_cnt(4); data[] immediately follows.
            hdr = reader.read_n64(array_base, 8)
            if not hdr or len(hdr) < 8:
                self.set_no_data("Could not read ActorArray header.")
                return
            cnt     = struct.unpack_from(">i", hdr, 0)[0]
            max_cnt = struct.unpack_from(">i", hdr, 4)[0]
            if cnt < 0 or cnt > 4096 or max_cnt < 0 or max_cnt > 4096:
                self.set_no_data(f"ActorArray header looks invalid (cnt={cnt}, max={max_cnt}).")
                return
            data_addr      = array_base + layout["array_data_off"]
            free_start_off = max_cnt * layout["actor_size"]  # BK: all slots valid

        if max_cnt <= 0 or max_cnt > 4096:
            self.set_no_data(f"ActorArray slot count looks invalid ({max_cnt}).")
            return

        # Read entire actor data block in one call where possible
        total_bytes = max_cnt * actor_size
        bulk = reader.read_n64(data_addr, total_bytes) if max_cnt > 0 else b""

        # ── Parse all actor slots ─────────────────────────────────────────────
        actors = []
        # Marker read cache: only read uncached pointers (new/changed ones).
        # This eliminates N separate ReadProcessMemory calls per frame once
        # the level is stable — typically zero extra reads after the first tick.
        pending_markers = {}   # marker_ptr → list of actor dicts that need it

        for i in range(max_cnt):
            off = i * actor_size

            # BT: slots at or beyond first_free contain garbage data.
            # We still include them so "show despawned" can display them,
            # but we must not dereference their (garbage) marker pointers.
            is_free_slot = (off >= free_start_off)

            if bulk and off + actor_size <= len(bulk):
                raw = bulk[off: off + actor_size]
            else:
                raw = reader.read_n64(data_addr + off, actor_size)
                if not raw:
                    continue

            actor = _parse_actor(raw, data_addr + off, layout)
            if actor is None:
                continue
            actor["slot"] = i

            if is_free_slot:
                # Override any garbage values so the row renders cleanly.
                actor["initialized"] = 0
                actor["despawned"]   = 1
                actor["marker_ptr"]  = 0
                actors.append(actor)
                continue

            if layout["read_marker"]:
                marker_ptr = actor["marker_ptr"]
                if is_xenia:
                    # Xenia BT: marker_ptr is a raw 32-bit physical address.
                    # Any non-zero value is potentially valid; convert to host addr.
                    if marker_ptr:
                        host_marker_ptr = marker_ptr + 0x100000000
                        if host_marker_ptr in self._marker_cache:
                            actor["marker_id"], actor["model_id"] = self._marker_cache[host_marker_ptr]
                        else:
                            pending_markers.setdefault(host_marker_ptr, []).append(actor)
                        actor["marker_ptr"] = host_marker_ptr
                else:
                    if marker_ptr and 0x80000000 <= marker_ptr <= 0x807FFFFF:
                        if marker_ptr in self._marker_cache:
                            actor["marker_id"], actor["model_id"] = self._marker_cache[marker_ptr]
                        else:
                            pending_markers.setdefault(marker_ptr, []).append(actor)

            actors.append(actor)

        # Read only uncached marker pointers — zero per frame on a stable level.
        for marker_ptr, actor_list in pending_markers.items():
            mkr_raw = reader.read_n64(marker_ptr, layout["mkr_read_size"])
            if mkr_raw:
                mid, modid = _parse_marker_fields(mkr_raw, layout)
                self._marker_cache[marker_ptr] = (mid, modid)
                for a in actor_list:
                    a["marker_id"], a["model_id"] = mid, modid

        # Evict stale cache entries (pointers no longer held by any live actor).
        live_ptrs = {a["marker_ptr"] for a in actors if a["marker_ptr"]}
        for p in [p for p in self._marker_cache if p not in live_ptrs]:
            del self._marker_cache[p]

        self._actors = actors
        self._summary_var.set(
            f"{cnt} live / {max_cnt} slots   "
            f"struct @ 0x{array_base:08X}"
        )
        self._refresh_table(actors)

    def set_no_data(self, msg="Waiting for data..."):
        self._summary_var.set(msg)
        self._clear()

    # ── Self-driven poll loop ────────────────────────────────────────────────
    # Same pattern as WatchesView.start_polling: once started this reschedules
    # itself on its own timer, independent of the shared ~200ms app poll tick,
    # so the tab feels as responsive as Watches instead of only refreshing
    # when the slower shared tick happens to fire.  is_visible lets the
    # caller skip the (heavier, bulk-read) actual work while the tab is
    # hidden, without stopping/restarting the loop.

    def start_polling(self, reader, is_visible=None):
        self._reader = reader
        if is_visible is not None:
            self._is_visible = is_visible
        if not self._polling:
            self._polling = True
            self._poll_loop()

    def stop_polling(self):
        self._polling = False

    def _poll_loop(self):
        if not self._polling:
            return
        visible = self._is_visible()
        # This tab just became the visible one this tick. Its header
        # (buttons/checkboxes/labels) already exists and was built once at
        # startup, but Tk hasn't had an idle moment to paint the newly
        # raised tab yet. Doing the bulk read + parse synchronously right
        # here would eat that idle moment first, making the header appear
        # to "pop in" late. Defer just this first refresh via after_idle so
        # the raised tab paints first.
        just_shown = visible and not self._was_visible
        self._was_visible = visible
        # Skip the whole read+parse (not just the tree redraw) while the
        # user is actively dragging/wheel-scrolling this tab's scrollbar -
        # that upstream work (a bulk memory read plus a Python parse loop
        # over every actor slot) is real main-thread work that competes
        # with Tk for scroll responsiveness if it isn't skipped here too.
        if (self._reader and getattr(self._reader, "connected", False)
                and visible
                and not (self._vsb.dragging and self._pause_while_scrolling_var.get())):
            if just_shown:
                self.after_idle(lambda r=self._reader: self.update_actors(r))
            else:
                self.update_actors(self._reader)
        self.after(self._poll_interval_ms, self._poll_loop)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C_BG)
        hdr.pack(fill=tk.X, padx=8, pady=(8, 4))
        tk.Label(hdr, text="ACTOR VIEWER",
                 font=("Courier New", 11, "bold"),
                 fg=C_HEADER, bg=C_BG).pack(side=tk.LEFT)

        self._summary_var = tk.StringVar(value="")
        tk.Label(hdr, textvariable=self._summary_var,
                 font=FONT, fg=C_DIM, bg=C_BG).pack(side=tk.RIGHT)

        # ── Address / array selector bar ──────────────────────────────────────
        abar = tk.Frame(self, bg=C_PANEL)
        abar.pack(fill=tk.X, padx=8, pady=(0, 2))

        tk.Label(abar, text="Array:", font=FONT, fg=C_DIM, bg=C_PANEL
                 ).pack(side=tk.LEFT, pady=2)
        self._array_combo_var = tk.StringVar(value="")
        self._array_combo = ttk.Combobox(abar, textvariable=self._array_combo_var,
                                         font=FONT, width=24, state="readonly")
        self._array_combo.pack(side=tk.LEFT, padx=(4, 12))
        self._array_combo.bind("<<ComboboxSelected>>", self._on_array_select)

        self._addr_hint_var = tk.StringVar(value="  No profile loaded")
        tk.Label(abar, textvariable=self._addr_hint_var,
                 font=FONT, fg=C_DIM, bg=C_PANEL).pack(side=tk.LEFT, pady=2)

        tk.Label(abar, text="Override addr:",
                 font=FONT, fg=C_DIM, bg=C_PANEL).pack(side=tk.LEFT, padx=(16, 4))
        self._addr_var = tk.StringVar(value="")
        addr_entry = tk.Entry(abar, textvariable=self._addr_var,
                              font=FONT, bg="#0D1117", fg=C_ADDR,
                              insertbackground=C_ADDR, relief=tk.FLAT,
                              bd=4, width=12)
        addr_entry.pack(side=tk.LEFT)
        addr_entry.bind("<Return>", self._on_addr_override)

        # ── Toolbar ───────────────────────────────────────────────────────────
        tb = tk.Frame(self, bg=C_PANEL)
        tb.pack(fill=tk.X, padx=8, pady=(0, 2))

        # Every slot is always shown.  The init/despawned flags this used to
        # filter on are not reliably located in every profile, so hiding rows
        # based on them risked hiding real actors.  Kept as an always-true var
        # so the existing filter expressions still read naturally.
        self._show_all_var = tk.BooleanVar(value=True)

        tk.Label(tb, text="Filter:", font=FONT, fg=C_DIM,
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

        # ── PanedWindow  (table + detail pane) ────────────────────────────────
        paned = tk.PanedWindow(self, orient=tk.VERTICAL, bg=C_BORDER,
                               sashwidth=5, sashrelief=tk.FLAT, handlesize=0)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        tree_frame = tk.Frame(paned, bg=C_BG)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Actors.Treeview",
                        background=C_BG, foreground=C_TEXT,
                        fieldbackground=C_BG, font=FONT, rowheight=18)
        style.configure("Actors.Treeview.Heading",
                        background=C_PANEL, foreground=C_HEADER, font=FONT_B)
        style.map("Actors.Treeview",
                  background=[("selected", C_SEL_BG)],
                  foreground=[("selected", C_TEXT)])

        self._tree = ttk.Treeview(tree_frame, columns=COL_IDS,
                                  show="headings", selectmode="browse",
                                  style="Actors.Treeview")
        for col_id, label, w, anchor, stretch in COLS:
            self._tree.heading(col_id, text=label,
                               command=lambda c=col_id: self._sort_by(c))
            self._tree.column(col_id, width=w, anchor=anchor, stretch=stretch,
                              minwidth=w)

        vsb = _LockedScrollbar(tree_frame, orient=tk.VERTICAL,
                               command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL,
                            command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._vsb = vsb

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        paned.add(tree_frame, stretch="always", minsize=80)

        # Tag colours
        self._tree.tag_configure("live", foreground=C_LIVE)
        self._tree.tag_configure("dead", foreground=C_DEAD)

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.bind("<Button-2>", self._on_right_click)  # macOS two-finger click
        # Mousewheel on the treeview also changes scroll position; lock the
        # scrollbar briefly so the thumb doesn't jump during live updates.
        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._tree.bind(_seq, lambda _e: self._vsb._bump(), add=True)

        # ── Context menu ──────────────────────────────────────────────────────
        self._ctx_menu = tk.Menu(self, tearoff=0,
                                 bg=C_PANEL, fg=C_TEXT,
                                 activebackground="#1F6FEB", activeforeground="white",
                                 font=FONT, bd=0, relief=tk.FLAT)
        # Column indices match COL_IDS order:
        # 0=#  1=Addr  2=MarkerPtr  3=MarkerID  4=MarkerName  5=ModelID  6=ModelName
        # 7=State  8=PosX  9=PosY  10=PosZ  11=Yaw  12=Pitch  13=Roll  14=Scale
        self._ctx_menu.add_command(label="Copy Addr",        command=lambda: self._copy_col(1))
        self._ctx_menu.add_command(label="Copy Marker Ptr",  command=lambda: self._copy_col(2))
        self._ctx_menu.add_command(label="Copy Marker ID",   command=lambda: self._copy_col(3))
        self._ctx_menu.add_command(label="Copy Marker Name", command=lambda: self._copy_col(4))
        self._ctx_menu.add_command(label="Copy Model ID",    command=lambda: self._copy_col(5))
        self._ctx_menu.add_command(label="Copy Model Name",  command=lambda: self._copy_col(6))
        self._ctx_menu.add_command(label="Copy Position",    command=self._copy_position)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Copy Row (TSV)",   command=self._copy_row_tsv)
        self._ctx_menu_iid = None

        # ── Detail pane ───────────────────────────────────────────────────────
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

        # ── Tag for address colour in detail pane ─────────────────────────────
        self._detail_text.tag_configure("addr",  foreground=C_ADDR)
        self._detail_text.tag_configure("label", foreground=C_HEADER)
        self._detail_text.tag_configure("live",  foreground=C_LIVE)
        self._detail_text.tag_configure("dead",  foreground=C_DEAD)

    # ── Filtering / Sorting ───────────────────────────────────────────────────

    def _on_filter_change(self, *_):
        self._filter_text = self._filter_var.get().lower()
        self._refresh_table(self._actors)

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._refresh_table(self._actors)

    def _actor_row_values(self, a):
        """Build the tuple of display values for one actor."""
        marker_name = self._marker_enum_names.get(a["marker_id"], "")
        if self._profile_id in ("bt", "xenia_bt"):
            mid = a["model_id"]
            if mid in BT_ANIM_ASSETS:
                model_name = BT_ANIM_ASSETS.get(mid, "")
            elif mid in BT_ASSETS:
                model_name = BT_ASSETS[mid].get("name", "")
            else:
                model_name = ""
        else:
            model_name = self._asset_enum_names.get(a["model_id"], "")
        return (
            str(a["slot"]),
            f"0x{a['addr']:X}",
            f"0x{a['marker_ptr']:X}" if a["marker_ptr"] else "NULL",
            f"0x{a['marker_id']:03X}",
            marker_name,
            f"0x{a['model_id']:04X}",
            model_name,
            str(a["state"]),
            f"{a['pos_x']:.1f}",
            f"{a['pos_y']:.1f}",
            f"{a['pos_z']:.1f}",
            f"{a['yaw']:.1f}",
            f"{a['pitch']:.1f}",
            f"{a['roll']:.1f}",
            f"{a['scale']:.3f}",
        )

    def _sort_key(self, a):
        col = self._sort_col
        try:
            if col == "#":          return a["slot"]
            if col == "Addr":       return a["addr"]
            if col == "MarkerPtr":  return a["marker_ptr"]
            if col == "MarkerID":   return a["marker_id"]
            if col == "MarkerName": return self._marker_enum_names.get(a["marker_id"], "").lower()
            if col == "ModelID":    return a["model_id"]
            if col == "ModelName":
                mid = a["model_id"]
                if self._profile_id in ("bt", "xenia_bt"):
                    if mid in BT_ANIM_ASSETS:
                        return BT_ANIM_ASSETS.get(mid, "").lower()
                    return BT_ASSETS[mid].get("name", "").lower() if mid in BT_ASSETS else ""
                return self._asset_enum_names.get(mid, "").lower()
            if col == "State":      return a["state"]
            if col == "PosX":       return a["pos_x"]
            if col == "PosY":       return a["pos_y"]
            if col == "PosZ":       return a["pos_z"]
            if col == "Yaw":        return a["yaw"]
            if col == "Pitch":      return a["pitch"]
            if col == "Roll":       return a["roll"]
            if col == "Scale":      return a["scale"]
        except Exception:
            pass
        return 0

    def _matches_filter(self, a):
        ft = self._filter_text
        if not ft:
            return True
        vals = self._actor_row_values(a)
        return any(ft in v.lower() for v in vals)

    # ── Table rendering ───────────────────────────────────────────────────────

    def _refresh_table(self, actors):
        # Don't mutate the tree while the user is dragging the scrollbar thumb
        # (or just flicked the mousewheel) — row insert/delete changes the
        # treeview's virtual height, which changes the first/last fractions,
        # which resizes the thumb mid-scroll.
        if self._vsb.dragging:
            return

        show_all = self._show_all_var.get()

        # Filter
        visible = [a for a in actors
                   if (show_all or a["despawned"] == False)
                   and self._matches_filter(a)]

        # Sort
        visible.sort(key=self._sort_key, reverse=not self._sort_asc)

        # Build row values once per actor and fingerprint the whole visible
        # set. If nothing changed since last frame (common on a static
        # scene), skip all Tk work entirely - this is the single biggest
        # win since Tk calls (even no-op ones like tree.get_children()) are
        # an IPC round trip, not a cheap Python call.
        row_vals = [self._actor_row_values(a) for a in visible]
        fp = (len(visible), *((a["addr"], hash(vals))
                               for a, vals in zip(visible, row_vals)))
        if fp == self._last_fp:
            return
        self._last_fp = fp

        tree = self._tree

        # Compute new IID key set (use addr as unique key)
        new_keys = {a["addr"] for a in visible}
        old_keys = set(self._addr_to_iid.keys())

        # Remove stale rows
        for key in old_keys - new_keys:
            iid = self._addr_to_iid.pop(key)
            self._render_cache.pop(iid, None)
            try:
                tree.delete(iid)
            except tk.TclError:
                pass

        # Insert or update
        for a, vals in zip(visible, row_vals):
            key  = a["addr"]
            #tag  = "live" if a["despawned"]==False else "dead"
            tag = "live"

            if key in self._addr_to_iid:
                iid = self._addr_to_iid[key]
                if self._render_cache.get(iid) != vals:
                    tree.item(iid, values=vals, tags=(tag,))
                    self._render_cache[iid] = vals
            else:
                iid = tree.insert("", tk.END, values=vals, tags=(tag,))
                self._addr_to_iid[key] = iid
                self._render_cache[iid] = vals

        # Reorder rows to match sorted order - only touch Tk (tree.move) when
        # the order actually changed, tracked here in Python to avoid an
        # O(n) tree.get_children() round trip every frame.
        new_addr_order = [a["addr"] for a in visible if a["addr"] in self._addr_to_iid]
        if new_addr_order != self._last_iid_order:
            for pos, addr_key in enumerate(new_addr_order):
                tree.move(self._addr_to_iid[addr_key], "", pos)
            self._last_iid_order = new_addr_order

    def _clear(self):
        self._actors = []
        self._addr_to_iid.clear()
        self._render_cache.clear()
        self._marker_cache.clear()
        self._last_iid_order = []
        self._last_fp = ()
        self._tree.delete(*self._tree.get_children())
        self._set_detail("")

    # ── Detail pane ───────────────────────────────────────────────────────────

    def _on_select(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        iid = sel[0]
        # find the actor by matching iid
        for a in self._actors:
            if self._addr_to_iid.get(a["addr"]) == iid:
                self._show_detail(a)
                return

    def _show_detail(self, a):
        marker_name = self._marker_enum_names.get(a["marker_id"], "—")
        if self._profile_id in ("bt", "xenia_bt"):
            mid = a["model_id"]
            if mid in BT_ANIM_ASSETS:
                model_name = BT_ANIM_ASSETS.get(mid, "—")
            elif mid in BT_ASSETS:
                model_name = BT_ASSETS[mid].get("name", "—")
            else:
                model_name = "—"
        else:
            model_name = self._asset_enum_names.get(a["model_id"], "—")

        lines = []
        lines.append(("label", "── Actor ─────────────────────────────────────\n"))
        lines.append(("addr",  f"  Slot       : {a['slot']}\n"))
        lines.append(("addr",  f"  Addr       : 0x{a['addr']:X}\n"))
        lines.append(("addr",  f"  MarkerPtr  : 0x{a['marker_ptr']:X}\n"))
        lines.append(("",      f"  Marker ID  : 0x{a['marker_id']:03X}  ({a['marker_id']})\n"))
        lines.append(("",      f"  Marker Name: {marker_name}\n"))
        lines.append(("",      f"  Model ID   : 0x{a['model_id']:04X}  ({a['model_id']})\n"))
        lines.append(("",      f"  Model Name : {model_name}\n"))
        lines.append(("",      "\n"))
        lines.append(("label", "── Flags ─────────────────────────────────────\n"))
        lines.append(("",      f"  state      : {a['state']}\n"))
        lines.append(("",      "\n"))
        lines.append(("label", "── Transform ─────────────────────────────────\n"))
        lines.append(("",      f"  Position   : ({a['pos_x']:.3f},  {a['pos_y']:.3f},  {a['pos_z']:.3f})\n"))
        lines.append(("",      f"  Yaw        : {a['yaw']:.3f}°\n"))
        lines.append(("",      f"  Pitch      : {a['pitch']:.3f}°\n"))
        lines.append(("",      f"  Roll       : {a['roll']:.3f}°\n"))
        lines.append(("",      f"  Scale      : {a['scale']:.6f}\n"))

        t = self._detail_text
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)
        for tag, text in lines:
            t.insert(tk.END, text, tag if tag else ())
        t.configure(state=tk.DISABLED)

    def _set_detail(self, text):
        t = self._detail_text
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)
        if text:
            t.insert(tk.END, text)
        t.configure(state=tk.DISABLED)

    # ── Address override ──────────────────────────────────────────────────────

    def _on_addr_override(self, _=None):
        try:
            addr = int(self._addr_var.get().strip(), 16)
            addr &= 0xFFFFFFFF
            self._array_addr = addr
            self._addr_hint_var.set(f"@ 0x{addr:08X}  (overridden)")
            self._clear()
        except ValueError:
            pass

    # ── Enum name loading ─────────────────────────────────────────────────────

    @staticmethod
    def _load_enum_names(path, enum_name):
        """Parse a C enum from a header file and return {int_value: name} dict.

        Works identically to HeapView.load_asset_enum_names but is generalised
        to accept any enum name (e.g. 'marker_e', 'asset_e').
        """
        names, current = {}, -1
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                in_enum = False
                for line in f:
                    stripped = line.strip()

                    if f"enum {enum_name}" in line:
                        in_enum = True
                        continue
                    if in_enum and "};" in line:
                        break
                    if not in_enum:
                        continue

                    # Commented-out entry: // 2fb Some Name
                    comment_match = re.search(r"//\s*([0-9A-Fa-f]+)\s+(.+)", stripped)

                    # Strip inline comments before parsing enum tokens
                    code = line.split("//")[0]

                    # Explicit value:  ENUM_NAME = 0xVAL  or  ENUM_NAME = decimal
                    m = re.search(r"([A-Za-z_]\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", code)
                    if m:
                        name, val = m.groups()
                        current = int(val, 0)
                        names[current] = name
                        continue

                    # Implicit value:  ENUM_NAME  (auto-increment)
                    m = re.search(r"([A-Za-z_]\w+)", code)
                    if m:
                        current += 1
                        names[current] = m.group(1)
                        continue

                    # Commented-out hex entry: // 2fb Some Label
                    if comment_match:
                        hex_val = int(comment_match.group(1), 16)
                        label = comment_match.group(2).strip()
                        current = hex_val
                        names[current] = label
                        continue

                    # Commented-out "// Unused" — advance counter only
                    if re.search(r"//\s*[Uu]nused", stripped):
                        current += 1

        except FileNotFoundError:
            pass
        return names

    # ── Right-click / copy ────────────────────────────────────────────────────

    def _on_right_click(self, event):
        if self._tree.identify_region(event.x, event.y) == "heading":
            self._show_column_menu(event)
            return
        iid = self._tree.identify_row(event.y)
        if iid:
            self._tree.selection_set(iid)
            self._ctx_menu_iid = iid
            for i in range(self._ctx_menu.index(tk.END) + 1):
                try:
                    self._ctx_menu.entryconfig(i, state=tk.NORMAL)
                except tk.TclError:
                    pass
        else:
            self._ctx_menu_iid = None
            for i in range(self._ctx_menu.index(tk.END) + 1):
                try:
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

    def _copy_position(self):
        """Copy X, Y, Z as a comma-separated triple."""
        iid = self._ctx_menu_iid
        if iid is None:
            return
        vals = self._tree.item(iid, "values")
        if not vals or len(vals) < 13:
            return
        # PosX=10, PosY=11, PosZ=12  (0-indexed in the tuple)
        self._clip(f"{vals[10]}, {vals[11]}, {vals[12]}")

    def _copy_row_tsv(self):
        iid = self._ctx_menu_iid
        if iid is None:
            return
        vals = self._tree.item(iid, "values")
        if not vals:
            return
        self._clip("\t".join(str(v) for v in vals))

    def _clip(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        prev = self._summary_var.get()
        self._summary_var.set(f"  ✓ Copied: {text[:60]}")
        self.after(1500, lambda: self._summary_var.set(prev))

    # ── CSV dump ──────────────────────────────────────────────────────────────

    def _dump_csv(self):
        ts       = time.strftime("%Y%m%d_%H%M%S")
        filename = f"actors_dump_{ts}.csv"
        out_path = os.path.join(app_dir(), filename)

        show_all = self._show_all_var.get()
        visible  = [a for a in self._actors
                    if (show_all or not a["despawned"])
                    and self._matches_filter(a)]
        visible.sort(key=self._sort_key, reverse=not self._sort_asc)

        headers = ["#", "Addr", "MarkerPtr", "MarkerID", "MarkerName",
                   "ModelID", "ModelName", "State",
                   "PosX", "PosY", "PosZ", "Yaw", "Pitch", "Roll", "Scale"]
        try:
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(headers)
                for a in visible:
                    w.writerow(self._actor_row_values(a))
            prev = self._summary_var.get()
            self._summary_var.set(f"  ✓ Saved → {filename}")
            self.after(2500, lambda: self._summary_var.set(prev))
        except OSError as e:
            prev = self._summary_var.get()
            self._summary_var.set(f"  ✗ Save failed: {e}")
            self.after(2500, lambda: self._summary_var.set(prev))
