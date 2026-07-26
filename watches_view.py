"""
watches_view.py  –  Memory watch list for Banjo Memory Viewer (Banjo-Kazooie / Banjo-Tooie).

Data types
----------
  u8   unsigned byte          (1 B)
  s8   signed byte            (1 B)
  u16  unsigned short         (2 B)
  s16  signed short           (2 B)
  u32  unsigned long          (4 B)
  s32  signed long            (4 B)
  f32  float 32               (4 B)
  u64  unsigned long long     (8 B)
  s64  signed long long       (8 B)

Per-watch display
-----------------
  display_hex  True -> show value in hex  |  False -> decimal / float

Address expressions (pointer chains)
--------------------------------------
  0x80123456              plain address
  [0x80000000]            single dereference
  [[0x80000000]]          double dereference
  [[0x80000000]+0x4]+0x16 chain with offsets
  [0x80000000+0x4]        offset before dereference

All multi-byte reads are big-endian (N64 MIPS).
"""

import json
import os
import re
import struct
import tkinter as tk
from tkinter import filedialog, ttk
from app_paths import watches_dir

# ── Colours / fonts ───────────────────────────────────────────────────────────
C_BG    = "#0D1117"
C_PANEL = "#161B22"
C_GREEN = "#00FF88"
C_RED   = "#FF4444"
C_YELLOW= "#FFD700"
C_TEXT  = "#C9D1D9"
C_DIM   = "#667788"
C_FROZEN= "#FF8800"
C_PTR   = "#58A6FF"
FONT    = ("Courier New", 9)
FONT_B  = ("Courier New", 9, "bold")

WATCHES_FILE = os.path.join(watches_dir(), "bk_watches.json")

# Hideable data columns (the "#0" Label column is always shown - it's the
# primary identifier for each row, not really an optional field).
WATCH_COL_IDS = ("Address", "Type", "Value", "Frozen")

# ── Data-type registry ────────────────────────────────────────────────────────
# key -> (display label, byte_width, is_signed, is_float)
DTYPES = {
    "u8":  ("Unsigned Byte (u8)",       1, False, False),
    "s8":  ("Signed Byte (s8)",         1, True,  False),
    "u16": ("Unsigned Short (u16)",     2, False, False),
    "s16": ("Signed Short (s16)",       2, True,  False),
    "u32": ("Unsigned Long (u32)",      4, False, False),
    "s32": ("Signed Long (s32)",        4, True,  False),
    "f32": ("Float 32 (f32)",           4, False, True ),
    "u64": ("Unsigned Long Long (u64)", 8, False, False),
    "s64": ("Signed Long Long (s64)",   8, True,  False),
}
DTYPE_LABELS   = [v[0] for v in DTYPES.values()]
DTYPE_KEYS     = list(DTYPES.keys())
DTYPE_BY_LABEL = {v[0]: k for k, v in DTYPES.items()}

def dtype_bytes(dt):  return DTYPES[dt][1]
def dtype_signed(dt): return DTYPES[dt][2]
def dtype_float(dt):  return DTYPES[dt][3]

# Legacy size int -> default dtype
_SIZE_TO_DTYPE = {1: "u8", 2: "u16", 4: "u32", 8: "u64"}


# ── Value formatting / parsing ────────────────────────────────────────────────

def format_value(raw, dtype, display_hex):
    """Format raw unsigned integer for display in the tree."""
    if raw is None:
        return "?"
    nbytes = dtype_bytes(dtype)
    if dtype_float(dtype):
        bits = raw & 0xFFFFFFFF
        try:
            fval = struct.unpack(">f", struct.pack(">I", bits))[0]
        except Exception:
            fval = float("nan")
        return f"{fval:.6g}"
    if dtype_signed(dtype):
        bits = nbytes * 8
        if raw >= (1 << (bits - 1)):
            raw = raw - (1 << bits)
    if display_hex:
        mask = (1 << (nbytes * 8)) - 1
        nhex = nbytes * 2
        return f"0x{(raw & mask):0{nhex}X}"
    return str(raw)


def format_freeze_val(raw, dtype):
    """Format freeze value for tree column (hex for ints, float notation for f32)."""
    if raw is None:
        return ""
    if dtype_float(dtype):
        bits = raw & 0xFFFFFFFF
        try:
            fval = struct.unpack(">f", struct.pack(">I", bits))[0]
        except Exception:
            fval = float("nan")
        return f"{fval:.6g}"
    nbytes = dtype_bytes(dtype)
    mask   = (1 << (nbytes * 8)) - 1
    return f"0x{(raw & mask):0{nbytes*2}X}"


def raw_to_entry_str(raw, dtype):
    """Convert raw int to an editable string for the freeze-val entry field."""
    if dtype_float(dtype):
        bits = raw & 0xFFFFFFFF
        try:
            fval = struct.unpack(">f", struct.pack(">I", bits))[0]
            return str(fval)
        except Exception:
            return "0.0"
    nbytes = dtype_bytes(dtype)
    mask   = (1 << (nbytes * 8)) - 1
    return f"0x{(raw & mask):0{nbytes*2}X}"


def parse_value_input(text, dtype):
    """
    Parse user-entered text to a raw unsigned integer suitable for writing.

    f32:      float literal  OR  0x... raw bits
    integers: 0x... -> hex,  else -> decimal (negative decimal allowed for signed types)

    Returns (int, None) on success, (None, error_str) on failure.
    """
    text   = text.strip()
    nbytes = dtype_bytes(dtype)
    mask   = (1 << (nbytes * 8)) - 1

    if dtype_float(dtype):
        if text.lower().startswith("0x"):
            try:
                return int(text, 16) & 0xFFFFFFFF, None
            except ValueError:
                return None, "Invalid hex"
        try:
            fval = float(text)
            raw  = struct.unpack(">I", struct.pack(">f", fval))[0]
            return raw, None
        except (ValueError, struct.error) as e:
            return None, f"Invalid float: {e}"

    if text.lower().startswith("0x"):
        try:
            raw = int(text, 16)
        except ValueError:
            return None, "Invalid hex (bad digits after 0x)"
    else:
        try:
            raw = int(text, 10)
        except ValueError:
            return None, "Enter decimal or 0x... hex"

    if dtype_signed(dtype):
        bits = nbytes * 8
        lo   = -(1 << (bits - 1))
        hi   =  (1 << (bits - 1)) - 1
        if not (lo <= raw <= (mask)):  # accept full unsigned range too
            if not (lo <= raw <= hi):
                return None, f"Out of range [{lo} ... {hi}]"
        raw = raw & mask
    else:
        if not (0 <= raw <= mask):
            return None, f"Out of range [0 ... {mask}]"

    return raw, None


# ── Pointer expression parser / evaluator ─────────────────────────────────────

class _Tokenizer:
    _TOKEN_RE = re.compile(r'0[xX][0-9A-Fa-f]+|[0-9]+|[+\-*/\[\]]|\s+')

    def __init__(self, text):
        self._tokens = [t for t in self._TOKEN_RE.findall(text) if not t.isspace()]
        self._pos    = 0

    def peek(self):
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def consume(self):
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def expect(self, val):
        if self._pos >= len(self._tokens):
            raise ValueError(f"Expected '{val}', got end of expression")
        tok = self.consume()
        if tok != val:
            raise ValueError(f"Expected '{val}', got '{tok}'")

    def done(self):
        return self._pos >= len(self._tokens)


def _parse_expr(tok):
    left = _parse_term(tok)
    while tok.peek() in ('+', '-'):
        op  = tok.consume()
        rhs = _parse_term(tok)
        left = (op, left, rhs)
    return left


def _parse_term(tok):
    left = _parse_atom(tok)
    while tok.peek() in ('*', '/'):
        op  = tok.consume()
        rhs = _parse_atom(tok)
        left = (op, left, rhs)
    return left


def _parse_atom(tok):
    if tok.peek() == '[':
        tok.consume()
        inner = _parse_expr(tok)
        tok.expect(']')
        return ('deref', inner)
    return _parse_literal(tok)


def _parse_literal(tok):
    t = tok.peek()
    if t is None:
        raise ValueError("Unexpected end of expression")
    tok.consume()
    try:
        return int(t, 0)
    except ValueError:
        raise ValueError(f"Expected number, got '{t}'")


def parse_addr_expr(text):
    text = text.strip()
    if not text:
        raise ValueError("Empty expression")
    if not re.match(r'^[0-9A-Fa-fx+\-*/\[\]\s]*$', text):
        bad = next((c for c in text if not re.match(r'[0-9A-Fa-fx+\-*/\[\]\s]', c)), '?')
        raise ValueError(f"Unexpected character '{bad}'")
    tok = _Tokenizer(text)
    ast = _parse_expr(tok)
    if not tok.done():
        raise ValueError(f"Unexpected token '{tok.peek()}' after expression")
    return ast


def eval_addr_expr(ast, reader):
    if isinstance(ast, int):
        return ast
    op = ast[0]
    if op == 'deref':
        inner = eval_addr_expr(ast[1], reader)
        if inner is None:
            return None
        return reader.read_u32_be(inner)
    if op in ('+', '-', '*', '/'):
        left  = eval_addr_expr(ast[1], reader)
        right = eval_addr_expr(ast[2], reader)
        if left is None or right is None:
            return None
        if op == '+': return left + right
        if op == '-': return left - right
        if op == '*': return left * right
        if op == '/':
            if right == 0:
                raise ValueError("Division by zero in address expression")
            return left // right
    raise ValueError(f"Unknown AST node: {ast}")


def is_pointer_expr(text):
    return '[' in text


def addr_expr_display(text):
    text = text.strip()
    if is_pointer_expr(text):
        return text
    try:
        return f"0x{int(text, 0):08X}"
    except ValueError:
        return text


# ── Default watches ───────────────────────────────────────────────────────────

# Banjo-Kazooie (BizHawk / N64) default watches
BK_DEFAULT_WATCHES = [
    '''{"type": "group", "id": "g1", "label": "Banjo", "collapsed": False},
    {"type": "watch", "label": "Lives",  "addr_expr": "0x8027EDB5",
     "dtype": "u8",  "display_hex": False, "frozen": False, "freeze_val": 3,   "group_id": "g1"},
    {"type": "watch", "label": "Health", "addr_expr": "0x8027EDB3",
     "dtype": "u8",  "display_hex": False, "frozen": False, "freeze_val": 8,   "group_id": "g1"},
    {"type": "watch", "label": "Notes",  "addr_expr": "0x8027EDB7",
     "dtype": "u8",  "display_hex": False, "frozen": False, "freeze_val": 100, "group_id": "g1"},
    {"type": "group", "id": "g2", "label": "World", "collapsed": False},
    {"type": "watch", "label": "World ID",    "addr_expr": "0x8027EE6C",
     "dtype": "u8",  "display_hex": False, "frozen": False, "freeze_val": 0,   "group_id": "g2"},
    {"type": "watch", "label": "Level Flags", "addr_expr": "0x8027EE70",
     "dtype": "u32", "display_hex": True,  "frozen": False, "freeze_val": 0,   "group_id": "g2"},'''
]

# Banjo-Tooie (BizHawk / N64) default watches
BT_DEFAULT_WATCHES = [
    '''{"type": "group", "id": "g1", "label": "Banjo", "collapsed": False},
    {"type": "watch", "label": "Health",     "addr_expr": "0x80135490",
     "dtype": "u8",  "display_hex": False, "frozen": False, "freeze_val": 8,   "group_id": "g1"},
    {"type": "watch", "label": "Air",        "addr_expr": "0x80135491",
     "dtype": "u8",  "display_hex": False, "frozen": False, "freeze_val": 5,   "group_id": "g1"},
    {"type": "watch", "label": "Feathers",   "addr_expr": "0x80135494",
     "dtype": "u16", "display_hex": False, "frozen": False, "freeze_val": 100, "group_id": "g1"},
    {"type": "watch", "label": "Eggs",       "addr_expr": "0x80135496",
     "dtype": "u16", "display_hex": False, "frozen": False, "freeze_val": 100, "group_id": "g1"},
    {"type": "group", "id": "g2", "label": "World", "collapsed": False},
    {"type": "watch", "label": "Map ID",     "addr_expr": "0x80127640",
     "dtype": "u16", "display_hex": True,  "frozen": False, "freeze_val": 0,   "group_id": "g2"},'''
]

# Banjo-Tooie (Xenia / Xbox 360) default watches
# Xbox 360 BT uses 0x8xxxxxxx kernel-space virtual addresses.
# XeniaMemoryReader._addr_to_host() strips the top bits and maps to physical.
XENIA_BT_DEFAULT_WATCHES = [
    '''{"type": "group", "id": "g1", "label": "Banjo", "collapsed": False},
    {"type": "watch", "label": "Health",     "addr_expr": "0x82C58C30",
     "dtype": "u8",  "display_hex": False, "frozen": False, "freeze_val": 8,   "group_id": "g1"},
    {"type": "watch", "label": "Air",        "addr_expr": "0x82C58C31",
     "dtype": "u8",  "display_hex": False, "frozen": False, "freeze_val": 5,   "group_id": "g1"},
    {"type": "watch", "label": "Feathers",   "addr_expr": "0x82C58C34",
     "dtype": "u16", "display_hex": False, "frozen": False, "freeze_val": 100, "group_id": "g1"},
    {"type": "watch", "label": "Eggs",       "addr_expr": "0x82C58C36",
     "dtype": "u16", "display_hex": False, "frozen": False, "freeze_val": 100, "group_id": "g1"},
    {"type": "group", "id": "g2", "label": "World", "collapsed": False},
    {"type": "watch", "label": "Map ID",     "addr_expr": "0x82B84640",
     "dtype": "u16", "display_hex": True,  "frozen": False, "freeze_val": 0,   "group_id": "g2"},'''
]

# Fall-back used when the path doesn't match any known profile file.
DEFAULT_WATCHES = BK_DEFAULT_WATCHES

# Map watches filename → default watch list for that profile.
_DEFAULTS_BY_FILE = {
    "bk_watches.json":       BK_DEFAULT_WATCHES,
    "bt_watches.json":       BT_DEFAULT_WATCHES,
    "bt_xenia_watches.json": XENIA_BT_DEFAULT_WATCHES,
    # No curated defaults yet for BK-on-Xenia - fall back to an empty list
    # (not BK_DEFAULT_WATCHES, whose addresses are N64-specific and wrong here).
    "bk_xenia_watches.json": [],
}


# ── WatchesView ───────────────────────────────────────────────────────────────

class WatchesView(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C_BG, **kw)
        self._entries       = []
        self._reader        = None
        self._next_group_id = 3
        self._sort_key      = None
        self._sort_reverse  = False
        self._tree_to_entry = {}
        self._polling = False
        self._poll_interval_ms = 16  # ~60 FPS
        # Tracks the last path used for load/save so Save As can default to it.
        # None means no file has been loaded yet (defaults will be used).
        self._current_save_path = None
        self._build_ui()
        self._load()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self, bg=C_BG)
        hdr.pack(fill=tk.X, padx=8, pady=(8, 4))
        tk.Label(hdr, text="WATCHES",
                 font=("Courier New", 11, "bold"),
                 fg=C_GREEN, bg=C_BG).pack(side=tk.LEFT)

        def btn(parent, text, cmd, bg, fg):
            return tk.Button(parent, text=text, command=cmd, font=FONT_B,
                             relief=tk.FLAT, padx=8, pady=3, cursor="hand2",
                             bg=bg, fg=fg)

        btn(hdr, "+ ADD",   self._add_dialog,       "#1F3A1F", C_GREEN ).pack(side=tk.RIGHT, padx=(4, 0))
        btn(hdr, "+ GROUP", self._add_group_dialog,  "#2D2A1A", C_YELLOW).pack(side=tk.RIGHT, padx=(4, 0))
        btn(hdr, "SAVE",    self._save_dialog,        "#1A2A3A", "#58A6FF").pack(side=tk.RIGHT, padx=(4, 0))
        btn(hdr, "LOAD",    self._load_dialog,       "#1A2A3A", "#58A6FF").pack(side=tk.RIGHT, padx=(4, 0))

        self._tree = ttk.Treeview(self, columns=WATCH_COL_IDS, show="tree headings",
                                  selectmode="browse")
        self._tree.heading("#0", text="Label",
                           command=lambda: self._sort_by("label"))
        # stretch=False on every column (including "#0" below) so resizing
        # one column just resizes that column - it doesn't grab space back
        # from, or give space to, its neighbours. A horizontal scrollbar
        # picks up the slack if the total width ends up wider than the pane.
        self._tree.column("#0", width=230, anchor="w", stretch=False)

        col_cfg = {
            "Address":              ("addr",      140, "center"),
            "Type":                 ("dtype",      90, "center"),
            "Value":                ("value",     120, "center"),
            "Frozen":               ("frozen",     55, "center"),
        }
        for col, (sort_k, w, anch) in col_cfg.items():
            self._tree.heading(col, text=col,
                               command=lambda k=sort_k: self._sort_by(k))
            self._tree.column(col, width=w, anchor=anch, stretch=False)

        vsb = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._tree.yview)
        hsb = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=(0, 8))
        hsb.pack(side=tk.BOTTOM, fill=tk.X, padx=(8, 8))
        self._tree.pack(fill=tk.BOTH, expand=True, padx=(8, 0), pady=(0, 8))

        # Thin line drawn over the tree during drag to show insert position
        self._drop_line = tk.Frame(self._tree, bg=C_GREEN, height=2)
        self._drop_line.place_forget()

        style = ttk.Style()
        style.configure("Treeview",
                        background=C_BG, foreground=C_TEXT,
                        fieldbackground=C_BG, font=FONT, rowheight=20)
        style.configure("Treeview.Heading",
                        background=C_PANEL, foreground=C_GREEN, font=FONT_B)
        self._tree.tag_configure("frozen",     foreground=C_FROZEN)
        self._tree.tag_configure("normal",     foreground=C_TEXT)
        self._tree.tag_configure("group",      foreground=C_YELLOW)
        self._tree.tag_configure("ptr_normal", foreground=C_PTR)
        self._tree.tag_configure("ptr_frozen", foreground=C_FROZEN)
        self._tree.tag_configure("ptr_err",    foreground=C_RED)

        self._tree.bind("<Double-1>",        self._on_double_click)
        self._tree.bind("<Button-3>",        self._on_right_click)
        self._tree.bind("<Delete>",          self._on_delete_key)
        self._tree.bind("<<TreeviewOpen>>",  self._on_tree_open)
        self._tree.bind("<<TreeviewClose>>", self._on_tree_close)
        self._tree.bind("<ButtonPress-1>",   self._on_drag_start)
        self._tree.bind("<B1-Motion>",       self._on_drag_motion)
        self._tree.bind("<ButtonRelease-1>", self._on_drag_release)
        # Right-click on empty area below rows
        self.bind("<Button-3>",              self._on_right_click_empty)

        self._drag_item   = None   # iid being dragged
        self._drag_active = False

        self._menu = tk.Menu(self._root(), tearoff=0, bg=C_PANEL, fg=C_TEXT,
                             activebackground="#1F6FEB", activeforeground="white",
                             font=FONT)

    def _root(self):
        w = self
        while w.master:
            w = w.master
        return w

    # ── Address helpers ───────────────────────────────────────────────────────

    def _resolve_addr(self, entry):
        expr_text = entry.get("addr_expr", "")
        is_ptr    = is_pointer_expr(expr_text)
        try:
            ast = entry.get("_ast")
            if ast is None:
                ast = parse_addr_expr(expr_text)
                entry["_ast"] = ast
        except ValueError as e:
            return None, is_ptr, str(e)

        if not is_ptr:
            try:
                return eval_addr_expr(ast, None), False, None
            except Exception as e:
                return None, False, str(e)

        if self._reader is None or not self._reader.connected:
            return None, True, "not connected"

        try:
            addr = eval_addr_expr(ast, self._reader)
            if addr is None:
                return None, True, "bad ptr read"
            return addr, True, None
        except Exception as e:
            return None, True, str(e)

    # ── Poll ──────────────────────────────────────────────────────────────────
    def start_polling(self, reader):
        self._reader = reader
        if not self._polling:
            self._polling = True
            self._poll_loop()

    def stop_polling(self):
        self._polling = False
    
    def _poll_loop(self):
        if not self._polling:
            return

        if self._reader and self._reader.connected:
            self.poll(self._reader)

        self.after(self._poll_interval_ms, self._poll_loop)
    
    def poll(self, reader):
        if not reader or not reader.connected:
            return
        self._reader = reader
        for entry in self._entries:
            if entry["type"] != "watch":
                continue
            addr, is_ptr, err = self._resolve_addr(entry)
            entry["_resolved_addr"] = addr
            entry["_ptr_err"]       = err
            entry["_is_ptr"]        = is_ptr

            if addr is None:
                entry["value"] = 0
                continue

            dtype = entry.get("dtype", "u8")
            if entry.get("frozen"):
                fv = entry.get("freeze_val")
                raw = fv if fv is not None else 0
                self._write_raw(reader, addr, dtype, raw)
                entry["value"] = raw
            else:
                raw = self._read_raw(reader, addr, dtype)
                entry["value"] = raw if raw is not None else 0

        self._refresh_tree()

    def _read_raw(self, reader, addr, dtype):
        nb = dtype_bytes(dtype)
        if nb == 1:
            return reader.read_u8(addr)
        if nb == 2:
            return reader.read_u16_be(addr)
        if nb == 4:
            return reader.read_u32_be(addr)
        if nb == 8:
            hi = reader.read_u32_be(addr)
            lo = reader.read_u32_be(addr + 4)
            if hi is None or lo is None:
                return None
            return (hi << 32) | lo
        return None

    def _write_raw(self, reader, addr, dtype, raw):
        nb = dtype_bytes(dtype)
        if nb == 1:
            reader.write_u8(addr, raw & 0xFF)
        elif nb == 2:
            reader.write_u16_be(addr, raw & 0xFFFF)
        elif nb == 4:
            reader.write_u32_be(addr, raw & 0xFFFFFFFF)
        elif nb == 8:
            reader.write_u64_be(addr, raw & 0xFFFFFFFFFFFFFFFF)

    # ── Tree refresh ──────────────────────────────────────────────────────────

    def _refresh_tree(self):
        selected_key = self._selected_entry_key()
        open_ids = {e["id"] for e in self._entries
                    if e["type"] == "group" and not e.get("collapsed")}

        self._tree_to_entry = {}
        self._tree.delete(*self._tree.get_children())

        # Build lookup: group_id -> list of (idx, entry) children
        children_by_group = {}
        root_entries = []
        for idx, entry in enumerate(self._entries):
            parent_gid = entry.get("group_id")
            if parent_gid:
                children_by_group.setdefault(parent_gid, []).append((idx, entry))
            else:
                root_entries.append((idx, entry))

        def insert_group(tree_parent, idx, entry):
            iid = self._tree_id(entry)
            self._tree_to_entry[iid] = idx
            self._tree.insert(tree_parent, tk.END, iid=iid, text=entry["label"],
                              open=(entry["id"] in open_ids),
                              tags=("group",), values=("", "", "", ""))
            for cidx, child in self._sorted_pairs(
                    children_by_group.get(entry["id"], [])):
                if child["type"] == "group":
                    insert_group(iid, cidx, child)
                else:
                    self._insert_watch(iid, cidx, child)

        for idx, entry in self._sorted_pairs(root_entries):
            if entry["type"] == "group":
                insert_group("", idx, entry)
            else:
                self._insert_watch("", idx, entry)

        if selected_key:
            iid = self._key_to_iid(selected_key)
            if self._tree.exists(iid):
                self._tree.selection_set(iid)

    def _insert_watch(self, parent_iid, idx, watch):
        iid = self._tree_id(watch, idx)
        self._tree_to_entry[iid] = idx

        dtype       = watch.get("dtype", "u8")
        raw         = watch.get("value", 0) or 0
        display_hex = watch.get("display_hex", False)
        fv = watch.get("freeze_val")
        fval_raw    = fv if fv is not None else 0
        frozen      = watch.get("frozen", False)

        val_str  = format_value(raw, dtype, display_hex)
        fval_str = format_freeze_val(fval_raw, dtype) if frozen else ""
        type_lbl = dtype.upper()

        # Address column — show resolved address; expression is visible in edit dialog
        expr_text = watch.get("addr_expr", "0x00000000")
        is_ptr    = watch.get("_is_ptr",   is_pointer_expr(expr_text))
        resolved  = watch.get("_resolved_addr")
        ptr_err   = watch.get("_ptr_err")

        if is_ptr:
            if ptr_err:
                addr_col = "[ERR]"
                tag = "ptr_err"
            elif resolved is not None:
                addr_col = f"0x{resolved:08X}"
                tag = "ptr_frozen" if frozen else "ptr_normal"
            else:
                addr_col = "[?]"
                tag = "ptr_err"
        else:
            addr_col = addr_expr_display(expr_text)
            tag = "frozen" if frozen else "normal"

        self._tree.insert(parent_iid, tk.END, iid=iid, text=watch["label"],
                          tags=(tag,), values=(
                              addr_col,
                              type_lbl,
                              val_str,
                              "✓" if frozen else "",
                          ))

    def _sorted_pairs(self, pairs):
        if not self._sort_key:
            return pairs
        return sorted(pairs,
                      key=lambda p: self._sort_val(p[1], self._sort_key),
                      reverse=self._sort_reverse)

    def _sort_val(self, entry, key):
        if entry["type"] == "group":
            return entry.get("label", "") if key == "label" else ""
        if key == "label":  return entry.get("label", "")
        if key == "addr":   return entry.get("addr_expr", "")
        if key == "dtype":  return entry.get("dtype", "")
        if key == "value":  return entry.get("value", 0)
        if key == "frozen": return int(entry.get("frozen", False))
        return ""

    def _sort_by(self, key):
        if self._sort_key == key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_key     = key
            self._sort_reverse = False
        self._refresh_tree()

    def _tree_id(self, entry, idx=None):
        if entry["type"] == "group":
            return f"group_{entry['id']}"
        safe = re.sub(r'[^0-9A-Za-z]', '_', entry.get("addr_expr", "0"))
        return f"watch_{idx}_{safe}"

    def _key_to_iid(self, key):
        return key

    def _selected_entry_key(self):
        sel = self._tree.selection()
        return sel[0] if sel else None

    def _selected_entry(self):
        sel = self._tree.selection()
        if not sel:
            return None
        idx = self._tree_to_entry.get(sel[0])
        return self._entries[idx] if idx is not None else None

    def _selected_index(self):
        sel = self._tree.selection()
        if not sel:
            return None
        return self._tree_to_entry.get(sel[0])

    # ── Add / Edit watch dialog ───────────────────────────────────────────────

    def _add_dialog(self, prefill=None, edit_idx=None, default_group_id=None):
        is_edit = prefill is not None
        dlg = tk.Toplevel(self._root())
        dlg.title("Edit Watch" if is_edit else "Add Watch")
        dlg.configure(bg=C_BG)
        dlg.geometry("490x530")
        dlg.resizable(False, False)
        dlg.transient(self._root())
        dlg.update_idletasks()
        dlg.grab_set()

        LBL_W = 15  # label column width in chars

        def row_frame(pady=3):
            f = tk.Frame(dlg, bg=C_BG)
            f.pack(fill=tk.X, padx=14, pady=pady)
            return f

        def field_label(parent, text):
            tk.Label(parent, text=text, font=FONT, fg=C_TEXT, bg=C_BG,
                     width=LBL_W, anchor="w").pack(side=tk.LEFT)

        def entry_widget(parent, default="", fg=C_TEXT, width=None):
            var = tk.StringVar(value=default)
            kw  = dict(textvariable=var, font=FONT, bg=C_PANEL, fg=fg,
                       insertbackground=fg, relief=tk.FLAT, bd=4)
            if width:
                kw["width"] = width
            e = tk.Entry(parent, **kw)
            e.pack(side=tk.LEFT, fill=tk.X, expand=(width is None))
            return var, e

        # ── Title ──────────────────────────────────────────────────────────
        tk.Label(dlg, text="Edit Watch" if is_edit else "Add Watch",
                 font=FONT_B, fg=C_GREEN, bg=C_BG).pack(pady=(12, 4))

        # ── Current value display + quick-set (edit mode only) ────────────────
        if is_edit and prefill:
            cur_raw_disp = prefill.get("value", 0) or 0
            cur_dt_disp  = prefill.get("dtype", "u8")
            cur_val_str  = format_value(cur_raw_disp, cur_dt_disp,
                                        display_hex=prefill.get("display_hex", False))
            frz_note = "  ✓ frozen" if prefill.get("frozen") else ""

            cv_frame = tk.Frame(dlg, bg=C_BG)
            cv_frame.pack(fill=tk.X, padx=14, pady=(0, 4))
            tk.Label(cv_frame, text="Current value:", font=FONT, fg=C_TEXT,
                     bg=C_BG, width=LBL_W, anchor="w").pack(side=tk.LEFT)
            v_setval = tk.StringVar(value=cur_val_str)
            setval_e = tk.Entry(cv_frame, textvariable=v_setval, font=FONT,
                                bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                                relief=tk.FLAT, bd=4, width=14)
            setval_e.pack(side=tk.LEFT)
            if frz_note:
                tk.Label(cv_frame, text=frz_note, font=("Courier New", 8),
                         fg=C_FROZEN, bg=C_BG).pack(side=tk.LEFT, padx=(6, 0))

            setval_status = tk.StringVar()
            setval_status_lbl = tk.Label(cv_frame, textvariable=setval_status,
                                         font=("Courier New", 8), fg=C_RED, bg=C_BG)
            setval_status_lbl.pack(side=tk.LEFT, padx=(6, 0))

            tk.Button(cv_frame, text="Set now", font=FONT, bg="#1F3A1F",
                      fg=C_GREEN, relief=tk.FLAT, padx=6, pady=1,
                      command=lambda: _do_set_now()).pack(side=tk.RIGHT, padx=(0, 0))
        else:
            tk.Frame(dlg, bg=C_BG, height=6).pack()

        # ── Label field ────────────────────────────────────────────────────
        rf = row_frame(); field_label(rf, "Label:")
        v_label, first_e = entry_widget(rf, prefill["label"] if prefill else "")
        first_e.focus_set()

        # ── Address field ──────────────────────────────────────────────────
        addr_default = (prefill.get("addr_expr", "0x80000000") if prefill else "0x80000000")
        rf = row_frame(); field_label(rf, "Address:")
        v_addr, _ = entry_widget(rf, addr_default)

        # live hint
        hint_var = tk.StringVar()
        hint_lbl = tk.Label(dlg, textvariable=hint_var,
                            font=("Courier New", 8), fg=C_DIM, bg=C_BG, anchor="w")
        hint_lbl.pack(fill=tk.X, padx=(14 + LBL_W * 7 + 4, 14), pady=(0, 1))

        def _addr_changed(*_):
            txt = v_addr.get().strip()
            if not txt:
                hint_var.set(""); return
            if is_pointer_expr(txt):
                try:
                    parse_addr_expr(txt)
                    hint_var.set("checkmark pointer expression")
                    hint_lbl.configure(fg=C_PTR)
                except ValueError as e:
                    hint_var.set(f"x {e}"); hint_lbl.configure(fg=C_RED)
            else:
                try:
                    hint_var.set(f"-> 0x{int(txt, 0):08X}"); hint_lbl.configure(fg=C_DIM)
                except ValueError:
                    hint_var.set("x invalid"); hint_lbl.configure(fg=C_RED)

        v_addr.trace_add("write", _addr_changed)
        dlg.after(0, _addr_changed)

        # ── Data type dropdown ─────────────────────────────────────────────
        cur_dtype = prefill.get("dtype", "u8") if prefill else "u8"
        cur_lbl   = DTYPES[cur_dtype][0]
        rf = row_frame(); field_label(rf, "Data Type:")
        v_dtype_lbl = tk.StringVar(value=cur_lbl)
        dtype_cb = ttk.Combobox(rf, textvariable=v_dtype_lbl, values=DTYPE_LABELS,
                                font=FONT, width=30, state="readonly")
        dtype_cb.pack(side=tk.LEFT)

        # ── Display as hex ─────────────────────────────────────────────────
        rf = row_frame(pady=2)
        tk.Label(rf, text="", font=FONT, bg=C_BG, width=LBL_W).pack(side=tk.LEFT)
        v_hex = tk.BooleanVar(value=prefill.get("display_hex", False) if prefill else False)
        tk.Checkbutton(rf, text="Display value as hexadecimal",
                       variable=v_hex, font=FONT, fg=C_TEXT, bg=C_BG,
                       selectcolor=C_PANEL, activebackground=C_BG,
                       activeforeground=C_TEXT).pack(side=tk.LEFT)

        # ── Freeze checkbox + freeze value on same row ────────────────────
        rf_frz = row_frame()
        field_label(rf_frz, "Freeze:")
        v_frozen = tk.BooleanVar(value=prefill.get("frozen", False) if prefill else False)
        tk.Checkbutton(rf_frz, variable=v_frozen, font=FONT,
                       fg=C_TEXT, bg=C_BG, selectcolor=C_PANEL,
                       activebackground=C_BG, activeforeground=C_TEXT
                       ).pack(side=tk.LEFT)

        tk.Label(rf_frz, text="  Freeze Val:", font=FONT, fg=C_TEXT, bg=C_BG
                 ).pack(side=tk.LEFT)

        fv_raw_init  = prefill.get("freeze_val", 0) if prefill else 0
        fv_dtype_init = prefill.get("dtype", "u8") if prefill else "u8"
        v_fval = tk.StringVar(value=raw_to_entry_str(fv_raw_init, fv_dtype_init))
        fval_e = tk.Entry(rf_frz, textvariable=v_fval, font=FONT,
                          bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                          relief=tk.FLAT, bd=4, width=16)
        fval_e.pack(side=tk.LEFT, padx=(4, 0))

        # Reformat freeze val when dtype changes
        def _dtype_changed(*_):
            new_dt  = DTYPE_BY_LABEL.get(v_dtype_lbl.get(), "u8")
            raw, _e = parse_value_input(v_fval.get().strip(), new_dt)
            v_fval.set(raw_to_entry_str(raw if raw is not None else 0, new_dt))

        v_dtype_lbl.trace_add("write", _dtype_changed)

        # Closure for set-now button (defined here so it can reference v_dtype_lbl)
        def _do_set_now():
            if not self._reader or not self._reader.connected:
                setval_status.set("not connected"); return
            addr = prefill.get("_resolved_addr") if prefill else None
            if addr is None:
                setval_status.set("addr unresolved"); return
            cur_dt = DTYPE_BY_LABEL.get(v_dtype_lbl.get(), "u8")
            raw, err = parse_value_input(v_setval.get().strip(), cur_dt)
            if raw is None:
                setval_status.set(err or "bad value"); return
            self._write_raw(self._reader, addr, cur_dt, raw)
            if edit_idx is not None:
                self._entries[edit_idx]["value"]      = raw
                self._entries[edit_idx]["freeze_val"] = raw
            if prefill:
                prefill["value"]      = raw
                prefill["freeze_val"] = raw
            setval_status.set("✓ written")
            setval_status_lbl.configure(fg=C_GREEN)

        # ── Description / notes ───────────────────────────────────────────
        rf_desc = tk.Frame(dlg, bg=C_BG)
        rf_desc.pack(fill=tk.X, padx=14, pady=(4, 2))
        tk.Label(rf_desc, text="Notes:", font=FONT, fg=C_TEXT, bg=C_BG,
                 width=LBL_W, anchor="nw").pack(side=tk.LEFT, anchor="n")
        desc_text = tk.Text(rf_desc, font=FONT, bg=C_PANEL, fg=C_TEXT,
                            insertbackground=C_TEXT, relief=tk.FLAT, bd=4,
                            height=12, wrap=tk.WORD)
        desc_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        desc_init = prefill.get("description", "") if prefill else ""
        if desc_init:
            desc_text.insert("1.0", desc_init)
        # Let Enter insert newlines in the notes box without triggering commit
        desc_text.bind("<Return>", lambda e: "break")

        # ── Status + OK / Cancel ───────────────────────────────────────────
        status_var = tk.StringVar()
        tk.Label(dlg, textvariable=status_var, font=FONT, fg=C_RED, bg=C_BG
                 ).pack(pady=(6, 0))

        # Preserve group membership from prefill or default; not editable via UI
        _gid = (prefill.get("group_id") if prefill else None) or default_group_id

        def commit():
            label = v_label.get().strip()
            if not label:
                status_var.set("Label required"); return

            expr = v_addr.get().strip()
            if not expr:
                status_var.set("Address required"); return
            try:
                parse_addr_expr(expr)
            except ValueError as e:
                status_var.set(f"Bad address: {e}"); return

            new_dtype = DTYPE_BY_LABEL.get(v_dtype_lbl.get(), "u8")

            fv_raw, fv_err = parse_value_input(v_fval.get().strip(), new_dtype)
            if fv_raw is None:
                status_var.set(f"Bad freeze value: {fv_err}"); return

            grp_name = None
            gid = _gid

            new_entry = {
                "type":        "watch",
                "label":       label,
                "addr_expr":   expr,
                "dtype":       new_dtype,
                "display_hex": bool(v_hex.get()),
                "frozen":      bool(v_frozen.get()),
                "freeze_val":  fv_raw,
                "group_id":    gid,
                "value":       prefill.get("value", 0) if prefill else 0,
                "description": desc_text.get("1.0", tk.END).strip(),
            }
            if edit_idx is not None:
                self._entries[edit_idx] = new_entry
            else:
                self._entries.append(new_entry)
            self._refresh_tree()
            dlg.destroy()

        bf = tk.Frame(dlg, bg=C_BG)
        bf.pack(pady=8)
        tk.Button(bf, text="OK", font=FONT_B, bg="#1F6FEB", fg="white",
                  relief=tk.FLAT, padx=12, pady=3, command=commit).pack(side=tk.LEFT, padx=4)
        tk.Button(bf, text="Cancel", font=FONT, bg=C_PANEL, fg=C_DIM,
                  relief=tk.FLAT, padx=12, pady=3, command=dlg.destroy).pack(side=tk.LEFT)
        dlg.bind("<Return>", lambda e: commit())

    # ── Add group dialog ──────────────────────────────────────────────────────

    def _add_group_dialog(self, prefill=None, edit_idx=None, default_group_id=None):
        dlg = tk.Toplevel(self._root())
        dlg.title("Add Group" if prefill is None else "Edit Group")
        dlg.configure(bg=C_BG)
        dlg.geometry("300x130")
        dlg.resizable(False, False)
        dlg.transient(self._root())
        dlg.grab_set()

        tk.Label(dlg, text="Group Name:", font=FONT, fg=C_TEXT, bg=C_BG
                 ).pack(padx=12, pady=(12, 4), anchor="w")
        v_label = tk.StringVar(value=prefill["label"] if prefill else "")
        e = tk.Entry(dlg, textvariable=v_label, font=FONT, bg=C_PANEL, fg=C_TEXT,
                     insertbackground=C_TEXT, relief=tk.FLAT, bd=4)
        e.pack(fill=tk.X, padx=12)
        e.focus_set()

        status_var = tk.StringVar()
        tk.Label(dlg, textvariable=status_var, font=FONT, fg=C_RED, bg=C_BG).pack()

        # Preserve parent group from prefill or caller
        _parent_gid = (prefill.get("group_id") if prefill else None) or default_group_id

        def commit():
            label = v_label.get().strip()
            if not label:
                status_var.set("Name required"); return
            if edit_idx is not None:
                self._entries[edit_idx]["label"] = label
            else:
                gid = self._new_group_id()
                self._entries.append({"type": "group", "id": gid,
                                      "label": label, "group_id": _parent_gid,
                                      "collapsed": False})
            self._refresh_tree()
            dlg.destroy()

        bf = tk.Frame(dlg, bg=C_BG)
        bf.pack(pady=4)
        tk.Button(bf, text="OK", font=FONT_B, bg="#1F6FEB", fg="white",
                  relief=tk.FLAT, padx=12, pady=3, command=commit).pack(side=tk.LEFT, padx=4)
        tk.Button(bf, text="Cancel", font=FONT, bg=C_PANEL, fg=C_DIM,
                  relief=tk.FLAT, padx=12, pady=3, command=dlg.destroy).pack(side=tk.LEFT)
        dlg.bind("<Return>", lambda ev: commit())

    def _new_group_id(self):
        gid = f"g{self._next_group_id}"
        self._next_group_id += 1
        return gid

    # ── Drag-and-drop reorder ─────────────────────────────────────────────────
    # Drop semantics:
    #   TOP half of a watch row     -> insert BEFORE it, inherit its group
    #   BOTTOM half of a watch row  -> insert AFTER it, inherit its group
    #   TOP half of a group header  -> insert BEFORE the group (no group adoption)
    #   BOTTOM half of a group header -> adopt that group, insert as first child
    #   Empty space below all rows  -> append to root, clear group

    def _on_drag_start(self, event):
        row = self._tree.identify_row(event.y)
        if row:
            self._drag_item   = row
            self._drag_active = False
        else:
            self._drag_item = None

    def _on_drag_motion(self, event):
        if not self._drag_item:
            return
        self._drag_active = True
        self._tree.config(cursor="fleur")

        target_iid = self._tree.identify_row(event.y)
        if target_iid and target_iid != self._drag_item:
            bbox = self._tree.bbox(target_iid)
            if bbox:
                x, y, w, h = bbox
                tgt_idx   = self._tree_to_entry.get(target_iid)
                tgt_entry = self._entries[tgt_idx] if tgt_idx is not None else None
                in_lower  = event.y >= y + h // 2
                if tgt_entry and tgt_entry["type"] == "group":
                    # lower half = adopt; upper half = insert before group
                    line_y = (y + h) if in_lower else y
                else:
                    line_y = (y + h) if in_lower else y
                self._drop_line.place(x=0, y=line_y - 1,
                                      width=self._tree.winfo_width() - 4)
                self._drop_line.lift()
                return
        # Empty area or same item
        if not target_iid:
            tree_h = self._tree.winfo_height()
            self._drop_line.place(x=0, y=tree_h - 3,
                                  width=self._tree.winfo_width() - 4)
            self._drop_line.lift()
        else:
            self._drop_line.place_forget()

    def _on_drag_release(self, event):
        self._tree.config(cursor="")
        self._drop_line.place_forget()
        src_iid = self._drag_item
        self._drag_item   = None
        if not self._drag_active:
            return
        self._drag_active = False

        target_iid = self._tree.identify_row(event.y)
        src_idx = self._tree_to_entry.get(src_iid)
        if src_idx is None:
            return
        src_entry = self._entries[src_idx]

        # ── Drop on empty space → move to root, append at end ────────────────
        if not target_iid or target_iid == src_iid:
            if not target_iid:
                src_entry["group_id"] = None
                self._entries.pop(src_idx)
                self._entries.append(src_entry)
                self._sort_key = None
                self._refresh_tree()
            return

        tgt_idx = self._tree_to_entry.get(target_iid)
        if tgt_idx is None:
            return
        tgt_entry = self._entries[tgt_idx]

        bbox = self._tree.bbox(target_iid)
        in_lower = True
        if bbox:
            _, y, _, h = bbox
            in_lower = event.y >= y + h // 2

        # ── Dropping onto a group header ──────────────────────────────────────
        if tgt_entry["type"] == "group":
            if in_lower:
                # Lower half → adopt that group, insert as first child after header
                src_entry["group_id"] = tgt_entry["id"]
                self._entries.pop(src_idx)
                new_tgt = self._entries.index(tgt_entry)
                self._entries.insert(new_tgt + 1, src_entry)
            else:
                # Upper half → insert BEFORE the group, into its parent's group (or root)
                src_entry["group_id"] = tgt_entry.get("group_id")
                self._entries.pop(src_idx)
                new_tgt = self._entries.index(tgt_entry)
                self._entries.insert(new_tgt, src_entry)

        # ── Dropping onto a watch ─────────────────────────────────────────────
        else:
            if in_lower:
                # Lower half → insert after target, inherit its group
                src_entry["group_id"] = tgt_entry.get("group_id")
                self._entries.pop(src_idx)
                new_tgt = self._entries.index(tgt_entry)
                self._entries.insert(new_tgt + 1, src_entry)
            else:
                # Upper half → insert before target, inherit its group
                src_entry["group_id"] = tgt_entry.get("group_id")
                self._entries.pop(src_idx)
                new_tgt = self._entries.index(tgt_entry)
                self._entries.insert(new_tgt, src_entry)

        self._sort_key = None
        self._refresh_tree()

    # ── Context menu ──────────────────────────────────────────────────────────

    def _on_double_click(self, event):
        entry = self._selected_entry()
        idx   = self._selected_index()
        if entry is None or idx is None:
            return
        if entry["type"] == "group":
            self._add_group_dialog(prefill=entry, edit_idx=idx)
        else:
            self._add_dialog(prefill=entry, edit_idx=idx)

    def _on_right_click(self, event):
        if self._tree.identify_region(event.x, event.y) == "heading":
            self._show_column_menu(event)
            return
        row = self._tree.identify_row(event.y)
        if not row:
            # Clicked below all rows — show root-level add menu
            self._show_empty_menu(event, parent_gid=None)
            return
        self._tree.selection_set(row)
        entry = self._selected_entry()
        if entry is None:
            return
        self._menu.delete(0, tk.END)
        if entry["type"] == "group":
            gid = entry["id"]
            parent_gid = entry.get("group_id")
            self._menu.add_command(label="Add watch here",
                                   command=lambda g=gid: self._add_dialog(default_group_id=g))
            self._menu.add_command(label="Add group here",
                                   command=lambda g=gid: self._add_group_dialog(default_group_id=g))
            self._menu.add_separator()
            self._menu.add_command(label="Edit group",   command=lambda: self._on_double_click(None))
            self._menu.add_command(label="Delete group", command=self._delete_selected)
        else:
            gid = entry.get("group_id")
            if gid:
                self._menu.add_command(label="Add watch to group",
                                       command=lambda g=gid: self._add_dialog(default_group_id=g))
                self._menu.add_command(label="Add group here",
                                       command=lambda g=gid: self._add_group_dialog(default_group_id=g))
            else:
                self._menu.add_command(label="Add watch",
                                       command=self._add_dialog)
                self._menu.add_command(label="Add group",
                                       command=self._add_group_dialog)
            self._menu.add_separator()
            self._menu.add_command(label="Edit",          command=lambda: self._on_double_click(None))
            self._menu.add_command(label="Toggle freeze",  command=self._toggle_freeze)
            self._menu.add_command(label="Set value now",  command=self._set_value_dialog)
            self._menu.add_separator()
            self._menu.add_command(label="Copy address",   command=self._copy_resolved_addr)
            self._menu.add_separator()
            self._menu.add_command(label="Delete",         command=self._delete_selected)
        self._menu.post(event.x_root, event.y_root)

    def _on_right_click_empty(self, event):
        # Only fire if the click is actually in the empty area (not on the tree widget)
        self._show_empty_menu(event, parent_gid=None)

    # ── Column show/hide (right-click a header) ──────────────────────────────

    def _visible_columns(self):
        """Current displaycolumns as a real list, resolving ttk's "#all"
        placeholder (meaning "everything, in declared order") to WATCH_COL_IDS."""
        cur = list(self._tree["displaycolumns"])
        return list(WATCH_COL_IDS) if cur == ["#all"] else cur

    def _show_column_menu(self, event):
        self._menu.delete(0, tk.END)
        visible = self._visible_columns()
        for col_id in WATCH_COL_IDS:
            shown = col_id in visible
            # add_command with our own mark instead of add_checkbutton - the
            # native checkbutton indicator can render invisibly against this
            # menu's dark custom colours on some platforms/themes.
            self._menu.add_command(
                label=f"{'✓' if shown else '  '}  {col_id}",
                command=lambda c=col_id, s=shown: self._toggle_column(c, not s))
        self._menu.post(event.x_root, event.y_root)

    def _toggle_column(self, col_id, show):
        visible = self._visible_columns()
        if show:
            if col_id not in visible:
                visible.append(col_id)
        else:
            if col_id in visible and len(visible) > 1:
                visible.remove(col_id)
            # else: refuse to hide the last remaining column
        ordered = [c for c in WATCH_COL_IDS if c in visible]
        self._tree.configure(displaycolumns=ordered)

    def _show_empty_menu(self, event, parent_gid):
        self._menu.delete(0, tk.END)
        self._menu.add_command(label="Add watch",
                               command=lambda: self._add_dialog(default_group_id=parent_gid))
        self._menu.add_command(label="Add group",
                               command=lambda: self._add_group_dialog(default_group_id=parent_gid))
        self._menu.post(event.x_root, event.y_root)

    def _on_delete_key(self, event):
        self._delete_selected()

    def _on_tree_open(self, event):
        entry = self._selected_entry()
        if entry and entry["type"] == "group":
            entry["collapsed"] = False

    def _on_tree_close(self, event):
        entry = self._selected_entry()
        if entry and entry["type"] == "group":
            entry["collapsed"] = True

    def _toggle_freeze(self):
        entry = self._selected_entry()
        if entry is None or entry["type"] != "watch":
            return
        entry["frozen"] = not entry["frozen"]
        if entry["frozen"] and entry.get("freeze_val") is None:
            entry["freeze_val"] = entry.get("value", 0)
        self._refresh_tree()

    def _copy_resolved_addr(self):
        """Copy the resolved (final) address of the selected watch to the clipboard."""
        entry = self._selected_entry()
        if entry is None or entry["type"] != "watch":
            return
        addr = entry.get("_resolved_addr")
        if addr is None:
            # Attempt a fresh evaluation — works for plain literals even offline;
            # pointer expressions require an active reader.
            try:
                ast = parse_addr_expr(entry.get("addr_expr", "0"))
                reader = self._reader if (self._reader and self._reader.connected) else None
                addr   = eval_addr_expr(ast, reader)
            except Exception:
                addr = None
        if addr is None:
            return
        text = f"0x{addr:08X}"
        root = self._root()
        root.clipboard_clear()
        root.clipboard_append(text)

    # ── Set-value dialog ──────────────────────────────────────────────────────

    def _set_value_dialog(self):
        idx   = self._selected_index()
        entry = self._selected_entry()
        if entry is None or entry["type"] != "watch" or idx is None:
            return

        dtype = entry.get("dtype", "u8")
        is_f  = dtype_float(dtype)

        dlg = tk.Toplevel(self._root())
        dlg.title("Set Value")
        dlg.configure(bg=C_BG)
        dlg.geometry("340x165")
        dlg.resizable(False, False)
        dlg.transient(self._root())
        dlg.grab_set()

        if is_f:
            fmt_hint = "float  e.g.  3.14   or  0x... raw bits"
        else:
            fmt_hint = "decimal  e.g.  255   or  0x... hex"

        tk.Label(dlg, text=f"Set value:  {entry['label']}  ({dtype.upper()})",
                 font=FONT_B, fg=C_GREEN, bg=C_BG).pack(padx=12, pady=(12, 2))
        tk.Label(dlg, text=fmt_hint,
                 font=("Courier New", 8), fg=C_DIM, bg=C_BG).pack()

        cur_raw = entry.get("value", 0) or 0
        cur_str = format_value(cur_raw, dtype, display_hex=not is_f)
        e_val = tk.Entry(dlg, font=FONT, bg=C_PANEL, fg=C_TEXT,
                         insertbackground=C_TEXT, relief=tk.FLAT, bd=4)
        e_val.insert(0, cur_str)
        e_val.pack(fill=tk.X, padx=12, pady=6)
        e_val.focus_set()
        e_val.select_range(0, tk.END)

        status = tk.StringVar()
        tk.Label(dlg, textvariable=status, font=FONT, fg=C_RED, bg=C_BG).pack()

        def commit():
            raw, err = parse_value_input(e_val.get(), dtype)
            if raw is None:
                status.set(err); return
            if not self._reader or not self._reader.connected:
                status.set("Not connected"); return
            addr = entry.get("_resolved_addr")
            if addr is None:
                status.set("Address not resolved"); return
            self._write_raw(self._reader, addr, dtype, raw)
            self._entries[idx]["value"]      = raw
            self._entries[idx]["freeze_val"] = raw
            self._refresh_tree()
            dlg.destroy()

        bf = tk.Frame(dlg, bg=C_BG)
        bf.pack(pady=4)
        tk.Button(bf, text="Set", font=FONT_B, bg="#1F6FEB", fg="white",
                  relief=tk.FLAT, padx=12, pady=3, command=commit).pack(side=tk.LEFT, padx=4)
        tk.Button(bf, text="Cancel", font=FONT, bg=C_PANEL, fg=C_DIM,
                  relief=tk.FLAT, padx=12, pady=3, command=dlg.destroy).pack(side=tk.LEFT)
        dlg.bind("<Return>", lambda ev: commit())

    def _delete_selected(self):
        idx   = self._selected_index()
        entry = self._selected_entry()
        if idx is None or entry is None:
            return
        if entry["type"] == "group":
            gid = entry["id"]
            for child in self._entries:
                if child.get("group_id") == gid:
                    # Re-parent to the deleted group's own parent (or root)
                    child["group_id"] = entry.get("group_id")
        del self._entries[idx]
        self._refresh_tree()

    # ── Persist ───────────────────────────────────────────────────────────────

    def _save(self, path):
        """Write watches to the given path. Must be called with an explicit path."""
        data = []
        for e in self._entries:
            if e["type"] == "group":
                data.append({"type": "group", "id": e["id"],
                             "label": e["label"],
                             "group_id": e.get("group_id"),
                             "collapsed": e.get("collapsed", False)})
            else:
                data.append({
                    "type":        "watch",
                    "label":       e["label"],
                    "addr_expr":   e.get("addr_expr", "0x00000000"),
                    "dtype":       e.get("dtype", "u8"),
                    "display_hex": e.get("display_hex", False),
                    "frozen":      e.get("frozen", False),
                    "freeze_val":  e.get("freeze_val", 0),
                    "group_id":    e.get("group_id"),
                    "description": e.get("description", ""),
                })
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _save_dialog(self):
        """Open a Save As dialog and write watches to the chosen path."""
        initial_dir  = os.path.dirname(self._current_save_path) if self._current_save_path else os.path.dirname(WATCHES_FILE)
        initial_file = os.path.basename(self._current_save_path) if self._current_save_path else "bk_watches.json"
        path = filedialog.asksaveasfilename(
            title="Save watches as…",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
            initialdir=initial_dir,
            initialfile=initial_file,
        )
        if path:
            self._save(path)
            self._current_save_path = path

    def _load(self, path=None):
        path = path or WATCHES_FILE
        if not os.path.exists(path):
            # Pick defaults matching the profile watches file, fall back to BK.
            fname    = os.path.basename(path)
            defaults = _DEFAULTS_BY_FILE.get(fname, DEFAULT_WATCHES)
            self._entries = []
            for item in defaults:
                e = dict(item)
                if e["type"] == "watch":
                    e["value"] = 0
                self._entries.append(e)
            self._refresh_tree()
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as exc:
            print(f"[watches] load failed: {exc}")
            return

        entries = []
        max_num = 0
        for item in data:
            if item.get("type") == "group":
                gid = item.get("id") or self._new_group_id()
                if gid.startswith("g") and gid[1:].isdigit():
                    max_num = max(max_num, int(gid[1:]))
                entries.append({"type": "group", "id": gid,
                                "label": item.get("label", "Group"),
                                "group_id": item.get("group_id"),
                                "collapsed": item.get("collapsed", False)})
            else:
                # Back-compat: old "addr" int -> addr_expr string
                addr_expr = item.get("addr_expr")
                if addr_expr is None:
                    raw_addr  = item.get("addr", 0)
                    addr_expr = f"0x{raw_addr:08X}" if isinstance(raw_addr, int) else str(raw_addr)
                # Back-compat: old "size" int -> dtype string
                dtype = item.get("dtype")
                if dtype is None:
                    dtype = _SIZE_TO_DTYPE.get(item.get("size", 1), "u8")

                entries.append({
                    "type":        "watch",
                    "label":       item.get("label", "Watch"),
                    "addr_expr":   addr_expr,
                    "dtype":       dtype,
                    "display_hex": item.get("display_hex", False),
                    "frozen":      item.get("frozen", False),
                    "freeze_val":  item.get("freeze_val", 0),
                    "group_id":    item.get("group_id"),
                    "description": item.get("description", ""),
                    "value":       0,
                })
        self._entries = entries
        self._next_group_id = max(self._next_group_id, max_num + 1)
        # Track the path so Save As can default to the loaded file.
        self._current_save_path = path
        self._refresh_tree()

    def _load_dialog(self):
        path = filedialog.askopenfilename(
            title="Load watches",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
            initialdir=os.path.dirname(self._current_save_path) if self._current_save_path else os.path.dirname(WATCHES_FILE))
        if path:
            self._load(path)
