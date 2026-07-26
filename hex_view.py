"""
hex_view.py - Live hex dump viewer for N64 RDRAM regions in BizHawk.

Shows 16 bytes per row with ASCII sidebar.
Highlights changed bytes in red since last frame.
Addresses shown as N64 virtual addresses (0x80xxxxxx).
Click or click-drag to select bytes; Ctrl+C or right-click to copy.
"""

import tkinter as tk
from tkinter import ttk

C_BG      = "#0D1117"
C_ADDR    = "#58A6FF"
C_HEX     = "#E6EDF3"
C_ASCII   = "#7EE787"
C_CHANGED = "#FF6B6B"
C_ZERO    = "#30363D"
C_HEADER  = "#00FF88"
C_SEL_BG  = "#1F4070"   # selection highlight background
C_SEL_FG  = "#FFD700"   # selection highlight text - yellow, distinct from non-zero bytes

BYTES_PER_ROW = 16
FONT          = ("Courier New", 9)
OVERSCAN      = 6

BASE_ADDR  = 0x80000000
RDRAM_SIZE = 0x00800000

# Each line layout (chars):
#   "  XXXXXXXX  "  = 12   (2 indent + 8 addr + 2 space)
#   "BB BB ... "     = BPR*3 - 1 + 1 padding = 48+2 = 50 total with trailing "  "
#   "  "             = 2    (separator before ASCII)
#   ASCII            = BPR
#
# Hex byte i starts at col: ADDR_COLS + i*3
# ASCII byte i starts at col: ADDR_COLS + BPR*3 + 2 + i
ADDR_COLS    = 12          # "  XXXXXXXX  "
HEX_START    = ADDR_COLS   # first hex nibble
HEX_END      = ADDR_COLS + BYTES_PER_ROW * 3 - 1   # last nibble of last byte
ASCII_START  = ADDR_COLS + BYTES_PER_ROW * 3 + 2

# Preset regions: label → (n64_start_addr, size)
REGIONS = {
    "RDRAM 0x80000000":  (0x80000000, 0x10000),
    "Heap  0x8002D500":  (0x8002D500, 0x10000),
    "WRAM  0x80276000":  (0x80276000, 0x2000),
    "Stack 0x8027C000":  (0x8027C000, 0x2000),
    "OvMgr 0x80282000":  (0x80282000, 0x1000),
    "Misc  0x80380000":  (0x80380000, 0x2000),
}


def _col_to_byte_index(col):
    """
    Given a character column within a line, return the byte index (0-15)
    that was clicked, or None if the click is outside both hex and ASCII zones.
    """
    # ── Hex zone ─────────────────────────────────────────────────────────────
    # Byte i occupies cols HEX_START+i*3 and HEX_START+i*3+1 (space at +2)
    if HEX_START <= col <= HEX_END:
        idx = (col - HEX_START) // 3
        if idx < BYTES_PER_ROW:
            return idx
    # ── ASCII zone ────────────────────────────────────────────────────────────
    if ASCII_START <= col < ASCII_START + BYTES_PER_ROW:
        return col - ASCII_START
    return None


class HexView(tk.Frame):

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C_BG, **kw)
        self._base_addr   = 0x80000000
        self._region_size = 0x10000
        self._data        = None
        self._prev_data   = None
        self._total_rows  = 0
        self._last_tagged_range = (-1, -1)
        self._regions     = dict(REGIONS)   # mutable per-game copy

        # Selection state: (start_byte_offset, end_byte_offset) inclusive,
        # both as absolute offsets into self._data.  None = no selection.
        self._sel_start: int | None = None
        self._sel_end:   int | None = None
        self._drag_anchor: int | None = None   # byte offset where drag began

        self._build_ui()

    # ── Layout helpers ────────────────────────────────────────────────────────

    def _text_index_to_byte(self, text_index):
        """
        Convert a Tk text widget index string (e.g. "3.27") to an absolute
        byte offset into self._data, or None if not over a data byte.
        """
        if not self._data:
            return None
        try:
            line, col = map(int, self._text.index(text_index).split("."))
        except (ValueError, tk.TclError):
            return None
        row = line - 1          # text lines are 1-based
        if row < 0 or row >= self._total_rows:
            return None
        byte_in_row = _col_to_byte_index(col)
        if byte_in_row is None:
            return None
        off = row * BYTES_PER_ROW + byte_in_row
        if off >= len(self._data):
            return None
        return off

    def _event_to_byte(self, event):
        """Convert a mouse event to an absolute byte offset, or None."""
        idx = self._text.index(f"@{event.x},{event.y}")
        return self._text_index_to_byte(idx)

    def _sel_range(self):
        """Return (lo, hi) inclusive byte offsets, or (None, None)."""
        if self._sel_start is None or self._sel_end is None:
            return None, None
        return min(self._sel_start, self._sel_end), max(self._sel_start, self._sel_end)

    def _build_ui(self):
        header = tk.Frame(self, bg=C_BG)
        header.pack(fill=tk.X, padx=8, pady=(8, 4))
        tk.Label(header, text="MEMORY VIEWER  (N64 RDRAM)",
                 font=("Courier New", 11, "bold"),
                 fg=C_HEADER, bg=C_BG).pack(side=tk.LEFT)

        # Status / copy feedback label
        self._status_var = tk.StringVar(value="")
        tk.Label(header, textvariable=self._status_var,
                 font=FONT, fg=C_HEADER, bg=C_BG).pack(side=tk.LEFT, padx=(16, 0))

        # Jump-to-address entry
        addr_frame = tk.Frame(header, bg=C_BG)
        addr_frame.pack(side=tk.RIGHT)
        tk.Label(addr_frame, text="Jump:", font=FONT, fg="#667788", bg=C_BG
                 ).pack(side=tk.LEFT, padx=(0, 4))
        self._jump_var = tk.StringVar(value="0x80000000")
        jump_entry = tk.Entry(addr_frame, textvariable=self._jump_var,
                              font=FONT, bg="#161B22", fg=C_HEX,
                              insertbackground=C_HEX, relief=tk.FLAT,
                              bd=4, width=12)
        jump_entry.pack(side=tk.LEFT)
        jump_entry.bind("<Return>", self._on_jump)

        self._region_var = tk.StringVar(value="Heap  0x8002D500")
        region_names = list(REGIONS.keys())
        sel = ttk.Combobox(addr_frame, textvariable=self._region_var,
                           width=20, font=FONT, values=region_names)
        sel.pack(side=tk.LEFT, padx=(8, 0))
        sel.bind("<<ComboboxSelected>>", self._on_region_change)
        self._region_combo = sel

        col_hdr = tk.Frame(self, bg="#161B22")
        col_hdr.pack(fill=tk.X, padx=8)
        tk.Label(col_hdr,
                 text="  ADDR      " + " ".join(f"{i:02X}" for i in range(16)) + "  ASCII",
                 font=FONT, fg=C_ADDR, bg="#161B22", anchor="w").pack(fill=tk.X)

        txt_frame = tk.Frame(self, bg=C_BG)
        txt_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self._text = tk.Text(txt_frame, font=FONT, bg=C_BG, fg=C_HEX,
                             state=tk.DISABLED, wrap=tk.NONE, cursor="arrow",
                             borderwidth=0, highlightthickness=0,
                             selectbackground=C_BG, selectforeground=C_HEX,
                             inactiveselectbackground=C_BG)
        vsb = tk.Scrollbar(txt_frame, orient=tk.VERTICAL,  command=self._text.yview)
        hsb = tk.Scrollbar(txt_frame, orient=tk.HORIZONTAL, command=self._text.xview)
        self._text.configure(yscrollcommand=self._on_yscroll, xscrollcommand=hsb.set)
        self._vsb = vsb

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._text.pack(fill=tk.BOTH, expand=True)

        # Colour tags (sel must be highest priority — added last)
        self._text.tag_config("addr",    foreground=C_ADDR)
        self._text.tag_config("zero",    foreground=C_ZERO)
        self._text.tag_config("changed", foreground=C_CHANGED)
        self._text.tag_config("ascii",   foreground=C_ASCII)
        self._text.tag_config("sel",
                              background=C_SEL_BG, foreground=C_SEL_FG)
        # sel must override all colour tags
        self._text.tag_raise("sel")

        # ── Mouse bindings ────────────────────────────────────────────────────
        self._text.bind("<ButtonPress-1>",   self._on_press)
        self._text.bind("<B1-Motion>",       self._on_drag)
        self._text.bind("<ButtonRelease-1>", self._on_release)
        self._text.bind("<MouseWheel>",      self._on_mousewheel)
        self._text.bind("<Button-4>",        self._on_mousewheel)
        self._text.bind("<Button-5>",        self._on_mousewheel)

        # Keyboard copy
        self._text.bind("<Control-c>",       self._on_copy)
        self._text.bind("<Control-C>",       self._on_copy)

        # Right-click context menu
        self._ctx_menu = tk.Menu(self, tearoff=0,
                                 bg="#161B22", fg=C_HEX,
                                 activebackground="#1F6FEB", activeforeground="white",
                                 font=FONT, bd=0, relief=tk.FLAT)
        self._ctx_menu.add_command(label="Copy hex bytes",
                                   command=lambda: self._copy("hex"))
        self._ctx_menu.add_command(label="Copy hex bytes (no spaces)",
                                   command=lambda: self._copy("hex_compact"))
        self._ctx_menu.add_command(label="Copy as C array",
                                   command=lambda: self._copy("c_array"))
        self._ctx_menu.add_command(label="Copy ASCII",
                                   command=lambda: self._copy("ascii"))
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Copy address",
                                   command=self._copy_address)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Select all",
                                   command=self._select_all)
        self._text.bind("<Button-3>", self._on_right_click)
        self._text.bind("<Button-2>", self._on_right_click)  # macOS

    # ── Public API ────────────────────────────────────────────────────────────

    def update_regions(self, regions: dict):
        """
        Replace the preset region list with a new game-profile dict.
        regions: label → (n64_start_addr, size)
        """
        self._regions = dict(regions)
        names = list(self._regions.keys())
        self._region_combo.configure(values=names)
        if names:
            first_key = names[0]
            self._region_var.set(first_key)
            self._base_addr, self._region_size = self._regions[first_key]
        self._data = None
        self._prev_data = None

    def update_data(self, data, base_addr=None):
        if base_addr is not None:
            self._base_addr = base_addr
        changed = (data != self._data)
        self._prev_data = self._data
        self._data      = data
        if data:
            self._total_rows = (len(data) + BYTES_PER_ROW - 1) // BYTES_PER_ROW
        if changed:
            self._full_redraw()
        else:
            self._tag_visible()

    def get_selected_region_addr(self):
        return self._base_addr

    def get_selected_region_size(self):
        return self._region_size

    def set_no_data(self, msg="Waiting for data..."):
        t = self._text
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)
        t.insert(tk.END, f"\n  {msg}", "addr")
        t.configure(state=tk.DISABLED)

    # ── Selection ─────────────────────────────────────────────────────────────

    def _apply_sel_tag(self):
        """Remove old sel highlights and apply new ones to both hex and ASCII."""
        t = self._text
        t.tag_remove("sel", "1.0", tk.END)
        lo, hi = self._sel_range()
        if lo is None:
            return
        for off in range(lo, hi + 1):
            row = off // BYTES_PER_ROW
            col = off  % BYTES_PER_ROW
            line_no = row + 1
            # Hex columns
            hc = HEX_START + col * 3
            t.tag_add("sel", f"{line_no}.{hc}", f"{line_no}.{hc+2}")
            # ASCII column
            ac = ASCII_START + col
            t.tag_add("sel", f"{line_no}.{ac}", f"{line_no}.{ac+1}")

    def _select_all(self):
        if not self._data:
            return
        self._sel_start = 0
        self._sel_end   = len(self._data) - 1
        self._apply_sel_tag()

    def _clear_selection(self):
        self._sel_start = None
        self._sel_end   = None
        self._drag_anchor = None
        self._text.tag_remove("sel", "1.0", tk.END)

    # ── Mouse handlers ────────────────────────────────────────────────────────

    def _on_press(self, event):
        off = self._event_to_byte(event)
        if off is None:
            self._clear_selection()
            return "break"
        self._drag_anchor = off
        self._sel_start   = off
        self._sel_end     = off
        self._apply_sel_tag()
        return "break"

    def _on_drag(self, event):
        if self._drag_anchor is None:
            return "break"
        off = self._event_to_byte(event)
        if off is None:
            return "break"
        self._sel_start = self._drag_anchor
        self._sel_end   = off
        self._apply_sel_tag()
        return "break"

    def _on_release(self, event):
        # Nothing extra needed; selection is already set by press/drag.
        return "break"

    # ── Copy ──────────────────────────────────────────────────────────────────

    def _selected_bytes(self):
        """Return the selected bytes as a list, or [] if nothing selected."""
        lo, hi = self._sel_range()
        if lo is None or not self._data:
            return []
        return list(self._data[lo: hi + 1])

    def _copy(self, fmt="hex"):
        """Copy selection in the requested format to the clipboard."""
        bs = self._selected_bytes()
        if not bs:
            return
        if fmt == "hex":
            text = " ".join(f"{b:02X}" for b in bs)
        elif fmt == "hex_compact":
            text = "".join(f"{b:02X}" for b in bs)
        elif fmt == "c_array":
            text = "{ " + ", ".join(f"0x{b:02X}" for b in bs) + " }"
        elif fmt == "ascii":
            text = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in bs)
        else:
            text = " ".join(f"{b:02X}" for b in bs)
        self.clipboard_clear()
        self.clipboard_append(text)
        preview = text[:48] + ("…" if len(text) > 48 else "")
        self._flash_status(f"✓ Copied {len(bs)} byte{'s' if len(bs) != 1 else ''}: {preview}")

    def _on_copy(self, _=None):
        self._copy("hex")
        return "break"

    def _copy_address(self):
        """Copy the N64 address of the first selected byte."""
        lo, _ = self._sel_range()
        if lo is None:
            return
        addr = self._base_addr + lo
        self.clipboard_clear()
        self.clipboard_append(f"0x{addr:08X}")
        self._flash_status(f"✓ Copied address: 0x{addr:08X}")

    def _flash_status(self, msg, ms=2000):
        self._status_var.set(msg)
        self.after(ms, lambda: self._status_var.set(""))

    def _on_right_click(self, event):
        # If clicking on a byte that isn't in the current selection, start a
        # new single-byte selection there before showing the menu.
        off = self._event_to_byte(event)
        lo, hi = self._sel_range()
        if off is not None and (lo is None or not (lo <= off <= hi)):
            self._sel_start = off
            self._sel_end   = off
            self._drag_anchor = off
            self._apply_sel_tag()
        # Enable/disable menu items based on whether there is a selection
        has_sel = bool(self._selected_bytes())
        for i in range(self._ctx_menu.index(tk.END) + 1):
            try:
                label = self._ctx_menu.entrycget(i, "label")
                if label != "Select all":
                    self._ctx_menu.entryconfig(
                        i, state=tk.NORMAL if has_sel else tk.DISABLED)
            except tk.TclError:
                pass
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_region_change(self, _=None):
        key = self._region_var.get()
        if key in self._regions:
            self._base_addr, self._region_size = self._regions[key]

    def _on_jump(self, _=None):
        try:
            addr = int(self._jump_var.get().strip(), 16)
            addr &= 0xFFFFFFFF
            self._base_addr   = addr
            self._region_size = 0x1000
            self._data        = None
            self._prev_data   = None
        except ValueError:
            pass

    def _on_yscroll(self, *args):
        self._vsb.set(*args)
        self._tag_visible()

    def _on_mousewheel(self, event):
        delta = getattr(event, "delta", 0)
        if event.num == 4 or delta > 0:
            self._text.yview_scroll(-3, "units")
        else:
            self._text.yview_scroll(3, "units")
        self._tag_visible()
        return "break"

    def _visible_row_range(self):
        if not self._total_rows:
            return 0, 0
        top, bot = self._text.yview()
        first = max(0, int(top * self._total_rows) - OVERSCAN)
        last  = min(self._total_rows, int(bot * self._total_rows) + 1 + OVERSCAN)
        return first, last

    def _full_redraw(self):
        if not self._data:
            return
        t = self._text
        scroll_pos = t.yview()[0]
        lines = []
        for row_idx in range(self._total_rows):
            rs   = row_idx * BYTES_PER_ROW
            row  = self._data[rs: rs + BYTES_PER_ROW]
            addr = self._base_addr + rs
            hex_part   = " ".join(f"{b:02X}" for b in row)
            pad        = "   " * (BYTES_PER_ROW - len(row))
            ascii_part = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in row)
            lines.append(f"  {addr:08X}  {hex_part}{pad}  {ascii_part}")
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)
        t.insert("1.0", "\n".join(lines))
        t.configure(state=tk.DISABLED)
        t.yview_moveto(scroll_pos)
        self._last_tagged_range = (-1, -1)
        self._tag_visible()
        # Re-apply selection highlight after full redraw
        self._apply_sel_tag()

    def _tag_visible(self):
        if not self._data:
            return
        first, last = self._visible_row_range()
        if (first, last) == self._last_tagged_range:
            return
        self._last_tagged_range = (first, last)
        t   = self._text
        bpr = BYTES_PER_ROW
        t.configure(state=tk.NORMAL)
        for row_idx in range(first, last):
            rs   = row_idx * BYTES_PER_ROW
            row  = self._data[rs: rs + bpr]
            if not row:
                continue
            line_no = row_idx + 1
            for tag in ("addr", "zero", "changed", "ascii"):
                t.tag_remove(tag, f"{line_no}.0", f"{line_no}.end")
            # Address: "  XXXXXXXX  " = 12 chars
            t.tag_add("addr", f"{line_no}.0", f"{line_no}.12")
            col = 12
            for i, byte in enumerate(row):
                prev = (self._prev_data[rs + i]
                        if self._prev_data and rs + i < len(self._prev_data)
                        else byte)
                tag = "changed" if byte != prev else ("zero" if byte == 0 else None)
                if tag:
                    t.tag_add(tag, f"{line_no}.{col}", f"{line_no}.{col+2}")
                col += 3
            hex_cols  = bpr * 3
            asc_start = 12 + hex_cols + 2
            asc_end   = asc_start + len(row)
            t.tag_add("ascii", f"{line_no}.{asc_start}", f"{line_no}.{asc_end}")
        t.configure(state=tk.DISABLED)
        # Re-apply selection on top (sel tag is raised above all others)
        self._apply_sel_tag()
