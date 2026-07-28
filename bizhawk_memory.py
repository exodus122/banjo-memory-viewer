"""
bizhawk_memory.py - Reads N64 RDRAM from a running BizHawk emulator process.

BizHawk stores N64 RDRAM as a flat 8MB buffer somewhere in its process memory.
We locate it by scanning for a game-specific boot signature.

N64 memory addressing:
  In-game virtual addr: 0x80000000 - 0x807FFFFF  (RDRAM, 8MB)
  BizHawk RDRAM offset:  game_addr - 0x80000000
  N64 is big-endian — all multi-byte reads are big-endian.

Supported games
---------------
  Banjo-Kazooie (USA v1.0)  — BK_PROFILE
  Banjo-Tooie   (USA)       — BT_PROFILE
"""

import array
import ctypes
import ctypes.wintypes
import struct
import sys
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

# Windows constants
PROCESS_VM_READ           = 0x0010
PROCESS_VM_WRITE          = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS        = 0x00000002
MEM_COMMIT                = 0x1000
PAGE_READWRITE            = 0x04
PAGE_EXECUTE_READWRITE    = 0x40

# N64 constants (shared)
BASE_ADDR    = 0x80000000
RDRAM_SIZE   = 0x00800000   # 8 MB

# Heap block state constants (same encoding in both games)
HEAP_HEADER_SIZE   = 0x10
HEAP_STATE_EMPTY   = 0
HEAP_STATE_USED    = 1
HEAP_STATE_PERM    = 2

# Xenia-canary constants
# Xenia maps Xbox 360 physical memory at a fixed offset inside its process.
# The game's address 0x00000000 maps to host process address XENIA_PHYS_BASE.
XENIA_PHYS_BASE     = 0x100000000   # Host address of Xbox 360 physical byte 0
XENIA_MEM_SIZE      = 0x20000000   # 512 MB physical address space scanned

# Xenia BK heap: fixed start address, contiguous node layout.
# Header layout (big-endian, 0x60 bytes):
#   word[8]  (offset 0x20): self-pointer low32  == host_node_addr - XENIA_PHYS_BASE
#   word[9]  (offset 0x24): used size (bytes of payload data; chunk_size is
#                            used_size rounded up to the next 0x10 boundary)
# Nodes are contiguous:
#   next_node_host = current_node_host + 0x60 + chunk_size
XENIA_BK_HEAP_START    = 0x141B48190   # host address of first node header
XENIA_BK_HDR_SIZE      = 0x60          # bytes before payload in each node
XENIA_BK_SELF_OFF      = 0x20          # byte offset of self-ptr word in header
XENIA_BK_SIZE_OFF      = 0x24          # byte offset of used-size word in header
XENIA_BK_MAX_NODES     = 8192

# Xenia BK heap: fixed start address, contiguous node layout.
# Header layout (big-endian, 0x60 bytes):
#   word[8]  (offset 0x20): self-pointer low32  == host_node_addr - XENIA_PHYS_BASE
#   word[9]  (offset 0x24): used size (bytes of payload data; chunk_size is
#                            used_size rounded up to the next 0x10 boundary)
# Nodes are contiguous:
#   next_node_host = current_node_host + 0x60 + chunk_size
XENIA_BK_HEAP_START    = 0x141A90040   # host address of first node header
XENIA_BK_HDR_SIZE      = 0x60          # bytes before payload in each node
XENIA_BK_SELF_OFF      = 0x20          # byte offset of self-ptr word in header
XENIA_BK_SIZE_OFF      = 0x24          # byte offset of used-size word in header
XENIA_BK_MAX_NODES     = 8192

# Xenia BT heap: fixed-stride slab allocator
# Each slab starts on a 0x10000-byte boundary.  The payload (data_length) can
# exceed 0x10000 bytes — in that case the node occupies multiple consecutive
# 0x10000 slots, and the NEXT node begins at the next 0x10000 boundary after
# the end of this node's content.
# Layout of one node:
#   [0x00] u32  self_addr_low  — low 32 bits of this node's host address
#                                (i.e. host_addr - XENIA_PHYS_BASE)
#   [0x04] u32  data_length    — payload bytes (may be > 0x10000)
#   [0x08..0x3F] reserved header bytes
#   [0x40..0x40+data_length-1] payload
#   after payload: 0x40 bytes of 0xDEDEDEDE sentinel footer
# The heap end is not known statically; the walker stops when the self-pointer
# check fails (indicating the end of valid nodes).
XENIA_BT_HEAP_START  = 0x100070000  # first node host address (observed)
XENIA_BT_HEAP_END    = 0x100640000  # last observed end — kept as a soft hint only
XENIA_BT_SLAB_STRIDE = 0x10000     # alignment of node starts
XENIA_BT_HDR_SIZE    = 0x40        # header bytes before payload
XENIA_BT_FOOTER_SIZE = 0x40        # 0x10 × 0xDEDEDEDE sentinel words after payload
XENIA_BT_FOOTER_MARK = 0xDEDEDEDE
# Maximum number of nodes to walk before giving up (safety cap).
XENIA_BT_MAX_NODES   = 4096


# ── Game Profile ──────────────────────────────────────────────────────────────

@dataclass
class GameProfile:
    """All game-specific constants needed by the reader and UI."""
    name: str                       # Short display name, e.g. "Banjo-Kazooie"
    id: str                         # Short key: "bk" | "bt" | "xenia_bt" | "xenia_bk"
    emulator: str = "bizhawk"       # "bizhawk" | "xenia"

    # RDRAM scan: list of (rdram_offset, magic_bytes) pairs to try
    # magic_bytes are the raw N64 big-endian bytes as stored in RDRAM
    # For Xenia profiles, scan_signatures is unused (process name is used instead).
    scan_signatures: List[tuple] = field(default_factory=list)
    title_sig: bytes = b""          # ASCII title fragment found in ROM header area

    # Heap
    heap_start: int = 0
    heap_size:  int = 0

    # Key runtime addresses
    overlay_mgr_addr:      int = 0  # u32 overlay/level index
    framebuffer_width_addr:  int = 0
    framebuffer_height_addr: int = 0

    # Watches JSON filename (relative to script dir)
    watches_file: str = ""

    # Overlay ID → name map
    overlay_names: dict = field(default_factory=dict)

    # Hex viewer preset regions: label → (n64_start, size)
    hex_regions: dict = field(default_factory=dict)

    # ActorArray pointer addresses: label → static_n64_addr
    # Each address is a static pointer (u32) that holds the current
    # heap address of an ActorArray.  Dereference to get the live addr.
    actor_array_pointers: dict = field(default_factory=dict)


# ── Banjo-Kazooie ─────────────────────────────────────────────────────────────

BK_PROFILE = GameProfile(
    name="Banjo-Kazooie",
    id="bk",
    emulator="bizhawk",

    # BK ROM magic at RDRAM offset 0x920 (N64 BE)
    scan_signatures=[
        (0x920, bytes([0x27, 0x80, 0x1A, 0x3C])),
        (0x8E0, bytes([0x27, 0x80, 0x1A, 0x3C])),
    ],
    title_sig=b"BANJO-KAZOOIE",

    heap_start=0x8002D500,
    heap_size =0x00210520,

    overlay_mgr_addr       =0x80282800,
    framebuffer_width_addr =0x80276588,
    framebuffer_height_addr=0x8027658C,

    watches_file="bk_watches.json",

    overlay_names={
        0:"core2",      1:"emptyLvl",    2:"CC/whale",   3:"MMM/haunted",
        4:"GV/desert",  5:"TTC/beach",   6:"MM/jungle",  7:"BGS/swamp",
        8:"RBB/ship",   9:"FP/snow",    10:"CCW/tree",  11:"SM/training",
        12:"cutscenes", 13:"lair/hub",  14:"fight/boss",
    },

    hex_regions={
        "RDRAM 0x80000000": (0x80000000, 0x10000),
        "Heap  0x8002D500": (0x8002D500, 0x10000),
        "WRAM  0x80276000": (0x80276000, 0x2000),
        "Stack 0x8027C000": (0x8027C000, 0x2000),
        "OvMgr 0x80282000": (0x80282000, 0x1000),
        "Misc  0x80380000": (0x80380000, 0x2000),
    },

    # Static pointer at this address holds the current heap addr of the ActorArray
    actor_array_pointers={
        "suBaddieActorArray": 0x8036E560,
    },
)


# ── Banjo-Tooie ───────────────────────────────────────────────────────────────
# BT ROM header magic at RDRAM offset 0x920 (N64 BE).
# BT's N64 ROM ID is "NB7E" (USA).  The ROM header magic word at 0x00 is
# 0x80371240 for most Rare N64 titles using the same boot code.
# BT's RDRAM boot signature differs from BK's — we key off the title string.

BT_PROFILE = GameProfile(
    name="Banjo-Tooie",
    id="bt",
    emulator="bizhawk",

    scan_signatures=[
        (0x920, bytes([0x03, 0x80, 0x1A, 0x3C])),   # Common Rare N64 magic
        (0x8E0, bytes([0x03, 0x80, 0x1A, 0x3C])),
    ],
    title_sig=b"BANJO-TOOIE",

    # BT heap lives at a different address; ScriptHawk references 0x807E9900
    # as the heap pointer.  For the heap walker we use these boundaries.
    heap_start=0x80137800,
    heap_size =0x002C8800,

    # BT runtime addresses (from ScriptHawk bt.lua and .wch watch file)
    # Overlay/level ID stored at 0x80127640 (map ID halfword)
    overlay_mgr_addr       =0x80127640,
    framebuffer_width_addr =0x80000000,   # not used in BT — placeholder
    framebuffer_height_addr=0x80000000,

    watches_file="bt_watches.json",

    overlay_names={
        0x01B4:"IoH",       # Isle o' Hags
        0x01B5:"MT",        # Mayahem Temple
        0x01B6:"GGM",       # Glitter Gulch Mine
        0x01B7:"WW",        # Witchyworld
        0x01B8:"JRL",       # Jolly Roger's Lagoon
        0x01B9:"TDL",       # Terrydactyland
        0x01BA:"GI",        # Grunty Industries
        0x01BB:"HFP",       # Hailfire Peaks
        0x01BC:"CCL",       # Cloud Cuckooland
        0x01C6:"CK",        # Cauldron Keep
        0x0000:"hub/title",
    },

    hex_regions={
        "RDRAM 0x80000000": (0x80000000, 0x10000),
        "Stack 0x80070000": (0x80070000, 0x4000),
        "Heap  0x8007E990": (0x8007E990, 0x10000),
        "Player0x80135490": (0x80135490, 0x1000),
        "Saves 0x8011AB40": (0x8011AB40, 0x1000),
        "HUD   0x8011B000": (0x8011B000, 0x1000),
    },

    actor_array_pointers={
        "Actor Array": 0x80136EE0,
    },
)

ALL_N64_PROFILES = [BK_PROFILE, BT_PROFILE]


# ── Xenia-canary Banjo-Tooie ──────────────────────────────────────────────────
# Banjo-Tooie (Xbox 360 / XBLA) running under Xenia-canary.
# Heap layout: fixed 0x10000-byte slab stride, header is 0x40 bytes.
# Addresses shown in UI use the full host address (0x100xxxxxx).

XENIA_BT_PROFILE = GameProfile(
    name="Banjo-Tooie (Xenia)",
    id="xenia_bt",
    emulator="xenia",

    # heap_start is the first observed node address.
    # heap_size is set to a large cap — the walker self-terminates when the
    # self-pointer check fails, so the actual end is discovered dynamically.
    heap_start=XENIA_BT_HEAP_START,
    heap_size =XENIA_MEM_SIZE,   # 512 MB cap; walker stops at first invalid node

    watches_file="bt_xenia_watches.json",

    overlay_names={},   # TODO: map Xbox 360 level IDs
    hex_regions={},
    actor_array_pointers={
        "Actor Array": 0x1826A2BCC,
    },
)

# ── Xenia-canary Banjo-Kazooie ────────────────────────────────────────────────
# Heap layout: contiguous nodes, each with a 0x60-byte header.
# The heap start is discovered dynamically at walk time by reading two root
# pointers and taking the lower value.  heap_start/heap_size are set to 0 here
# since walk_heap() does not use them for this profile.

XENIA_BK_PROFILE = GameProfile(
    name="Banjo-Kazooie (Xenia)",
    id="xenia_bk",
    emulator="xenia",

    heap_start=0,   # unknown — placeholder
    heap_size =0,

    watches_file="bk_xenia_watches.json",

    overlay_names={},
    hex_regions={},
    actor_array_pointers={},
)

ALL_PROFILES = [BK_PROFILE, BT_PROFILE, XENIA_BT_PROFILE, XENIA_BK_PROFILE]


# ── Convenience aliases so trainer_app.py imports still work ──────────────────
HEAP_START                = BK_PROFILE.heap_start
HEAP_SIZE                 = BK_PROFILE.heap_size
OVERLAY_MGR_LOADED_ID_ADDR = BK_PROFILE.overlay_mgr_addr
FRAMEBUFFER_WIDTH_ADDR     = BK_PROFILE.framebuffer_width_addr


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize",              ctypes.wintypes.DWORD),
        ("cntUsage",            ctypes.wintypes.DWORD),
        ("th32ProcessID",       ctypes.wintypes.DWORD),
        ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID",        ctypes.wintypes.DWORD),
        ("cntThreads",          ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase",      ctypes.c_long),
        ("dwFlags",             ctypes.wintypes.DWORD),
        ("szExeFile",           ctypes.c_char * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress",       ctypes.c_void_p),
        ("AllocationBase",    ctypes.c_void_p),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("RegionSize",        ctypes.c_size_t),
        ("State",             ctypes.wintypes.DWORD),
        ("Protect",           ctypes.wintypes.DWORD),
        ("Type",              ctypes.wintypes.DWORD),
    ]


class BizHawkMemoryReader:
    BIZHAWK_PROCESS_NAMES = [b"EmuHawk.exe"]

    def __init__(self, profile: GameProfile = BK_PROFILE):
        self.profile    = profile
        self.pid        = None
        self.handle     = None
        self.rdram_base = None   # process address of RDRAM byte 0
        self._k32 = ctypes.windll.kernel32 if sys.platform == "win32" else None

    def set_profile(self, profile: GameProfile, clear_rdram: bool = True):
        """Switch game profile.

        clear_rdram=True  (default): wipe rdram_base so the next connect()
                          re-scans.  Use this for a manual game switch where
                          a different ROM may be loaded in BizHawk.

        clear_rdram=False: keep the existing rdram_base.  Use this when
                          connect() itself detected the new game — the RDRAM
                          location is already correct and clearing it would
                          break all reads until the next reconnect.
        """
        self.profile = profile
        if clear_rdram:
            self.rdram_base = None

    @property
    def connected(self):
        return self.handle is not None and self.rdram_base is not None

    # ── Connection ────────────────────────────────────────────────────────────────

    def find_bizhawk_pid(self):
        if not self._k32:
            return None
        snap = self._k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == ctypes.wintypes.HANDLE(-1).value:
            return None
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        try:
            if not self._k32.Process32First(snap, ctypes.byref(entry)):
                return None
            while True:
                name_lower = entry.szExeFile.lower()
                for pname in self.BIZHAWK_PROCESS_NAMES:
                    if name_lower == pname.lower():
                        return entry.th32ProcessID
                if not self._k32.Process32Next(snap, ctypes.byref(entry)):
                    break
        finally:
            self._k32.CloseHandle(snap)
        return None

    def connect(self):
        """
        Connect to BizHawk and auto-detect the running N64 game.

        Returns (ok, message, detected_profile_or_None).
        Only handles BizHawk (EmuHawk.exe).  Xenia is handled separately by
        XeniaMemoryReader.connect(); trainer_app tries both in sequence.
        """
        if sys.platform != "win32":
            return False, "BizHawk memory access is Windows-only.", None

        self.pid = self.find_bizhawk_pid()
        if not self.pid:
            return False, (
                "BizHawk (EmuHawk.exe) not found."
            ), None

        self.handle = self._k32.OpenProcess(
            PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION,
            False, self.pid)
        if not self.handle:
            return False, f"Can't open BizHawk (PID {self.pid}). Run as Administrator.", None

        rdram_base, detected_profile = self._scan_for_rdram_auto()
        if rdram_base is None:
            self._k32.CloseHandle(self.handle)
            self.handle = None
            return False, (
                "BizHawk found but could not locate N64 RDRAM. "
                "Is a Banjo game loaded and running?"
            ), None

        self.rdram_base = rdram_base
        if detected_profile is not None:
            self.profile = detected_profile

        return True, (
            f"Connected (BizHawk)  PID={self.pid}  "
            f"RDRAM_BASE=0x{self.rdram_base:016X}  "
            f"Game={self.profile.name}"
        ), self.profile

    def disconnect(self):
        if self.handle and self._k32:
            self._k32.CloseHandle(self.handle)
        self.handle = None
        self.pid = None
        self.rdram_base = None

    # ── RDRAM scanning ────────────────────────────────────────────────────────────

    def _scan_for_rdram_auto(self):
        """
        Walk process virtual memory for committed regions >= 8MB, then try
        every profile's scan signatures (and title fallback) against each
        candidate.  Returns (rdram_base, matched_profile) or (None, None).

        The active profile is tried first; if that misses we iterate ALL_PROFILES
        so the caller can auto-switch to whichever game is actually running.
        """
        if not self._k32 or not self.handle:
            return None, None

        mbi  = MEMORY_BASIC_INFORMATION()
        addr = 0
        candidates = []

        while True:
            ret = self._k32.VirtualQueryEx(
                self.handle, ctypes.c_void_p(addr),
                ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not ret:
                break
            if (mbi.State == MEM_COMMIT and
                    mbi.RegionSize >= RDRAM_SIZE and
                    mbi.Protect in (PAGE_READWRITE, PAGE_EXECUTE_READWRITE)):
                candidates.append((mbi.BaseAddress, mbi.RegionSize))
            if mbi.RegionSize == 0:
                break
            addr = (mbi.BaseAddress or 0) + mbi.RegionSize
            if addr > 0x7FFFFFFFFFFF:
                break

        # Try every N64 profile; active profile goes first so it wins ties.
        active_is_n64 = self.profile in ALL_N64_PROFILES
        ordered_profiles = ([self.profile] if active_is_n64 else []) + [
            p for p in ALL_N64_PROFILES if p is not self.profile
        ]

        # Primary: magic-byte signatures
        for profile in ordered_profiles:
            for base, size in candidates:
                for (offset, magic) in profile.scan_signatures:
                    data = self._read_raw(base + offset, len(magic))
                    if data and data[:len(magic)] == magic:
                        return base + offset, profile

        # Fallback: title string sweep
        for profile in ordered_profiles:
            title = profile.title_sig
            for base, size in candidates:
                chunk = self._read_raw(base, min(0x2000, size))
                if chunk and title in chunk:
                    idx = chunk.find(title)
                    return base + (idx & ~0x3F), profile

        return None, None

    # Keep the old name as an alias for any external callers.
    def _scan_for_rdram(self):
        base, _ = self._scan_for_rdram_auto()
        return base

    def _scan_for_rdram_old(self):
        """
        Walk process virtual memory for a committed region >= 8MB, then check
        each candidate against the active profile's scan signatures.
        Falls back to the title string if magic bytes miss.
        """
        if not self._k32 or not self.handle:
            return None

        mbi = MEMORY_BASIC_INFORMATION()
        addr = 0
        candidates = []

        while True:
            ret = self._k32.VirtualQueryEx(
                self.handle, ctypes.c_void_p(addr),
                ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not ret:
                break
            if (mbi.State == MEM_COMMIT and
                    mbi.RegionSize >= RDRAM_SIZE and
                    mbi.Protect in (PAGE_READWRITE, PAGE_EXECUTE_READWRITE)):
                candidates.append((mbi.BaseAddress, mbi.RegionSize))
            if mbi.RegionSize == 0:
                break
            addr = (mbi.BaseAddress or 0) + mbi.RegionSize
            if addr > 0x7FFFFFFFFFFF:
                break

        # Primary: check each signature for this profile
        for base, size in candidates:
            for (offset, magic) in self.profile.scan_signatures:
                data = self._read_raw(base + offset, len(magic))
                if data and data[:len(magic)] == magic:
                    # Quick sanity: try to verify title string nearby
                    # Title is at +0x20 from the start of the N64 ROM header.
                    # In RDRAM, the ROM header begins at offset 0x0 or 0x1000
                    # depending on the game; we just check that the region looks sane.
                    return base + offset

        # Fallback: scan for title signature in a wider sweep of each candidate
        title = self.profile.title_sig
        for base, size in candidates:
            chunk = self._read_raw(base, min(0x2000, size))
            if chunk and title in chunk:
                # Find byte offset of title within chunk
                idx = chunk.find(title)
                # Round down to nearest 0x40 boundary (rough ROM header start)
                return base + (idx & ~0x3F)

        return None

    # ── Raw process memory read ───────────────────────────────────────────────

    def _read_raw(self, proc_addr, size):
        if not self.handle or not self._k32:
            return None
        buf = ctypes.create_string_buffer(size)
        n   = ctypes.c_size_t(0)
        self._k32.ReadProcessMemory.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        ok = self._k32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(proc_addr),
            buf, size, ctypes.byref(n))
        if ok and n.value > 0:
            return bytes(buf.raw[:n.value])
        return None

    def _write_raw(self, proc_addr, data):
        if not self.handle or not self._k32:
            return False
        buf = ctypes.create_string_buffer(bytes(data))
        n   = ctypes.c_size_t(0)
        ok = self._k32.WriteProcessMemory(
            self.handle, ctypes.c_void_p(proc_addr),
            buf, len(data), ctypes.byref(n))
        return bool(ok and n.value == len(data))

    # ── N64 address → process address ─────────────────────────────────────────

    def _n64_to_proc(self, n64_addr):
        if self.rdram_base is None:
            return None
        if n64_addr >= BASE_ADDR and n64_addr < (BASE_ADDR + RDRAM_SIZE):
            return self.rdram_base + (n64_addr - BASE_ADDR)
        return None

    # ── Core aligned word read ─────────────────────────────────────────────────

    def _read_u32_aligned(self, n64_addr):
        aligned   = n64_addr & ~3
        proc_addr = self._n64_to_proc(aligned)
        if proc_addr is None:
            return None
        raw = self._read_raw(proc_addr, 4)
        if not raw or len(raw) < 4:
            return None
        b0, b1, b2, b3 = raw
        return (b3 << 24) | (b2 << 16) | (b1 << 8) | b0

    # ── Public read API ────────────────────────────────────────────────────────

    def read_u8(self, n64_addr):
        word = self._read_u32_aligned(n64_addr)
        if word is None:
            return None
        shift = (3 - (n64_addr & 3)) * 8
        return (word >> shift) & 0xFF

    def read_u16_be(self, n64_addr):
        b0 = self.read_u8(n64_addr)
        b1 = self.read_u8(n64_addr + 1)
        if b0 is None or b1 is None:
            return None
        return (b0 << 8) | b1

    def read_u32_be(self, n64_addr):
        return self._read_u32_aligned(n64_addr)

    def read_s32_be(self, n64_addr):
        word = self._read_u32_aligned(n64_addr)
        if word is None:
            return None
        return struct.unpack(">i", struct.pack(">I", word))[0]

    def read_n64(self, n64_addr, size):
        """
        Read *size* bytes from N64 virtual address n64_addr.

        Uses a single ReadProcessMemory call (padded to a 4-byte-aligned window)
        then unswizzles the N64 byte-swap in Python.  This is ~1000x faster than
        the old per-byte loop for large regions like the hex viewer (64 KB) or
        the heap block headers.

        BizHawk stores N64 RDRAM as 32-bit little-endian words, so the byte at
        N64 offset i lives at raw offset (i ^ 3) within each aligned 4-byte word.
        """
        if size <= 0:
            return b""
        proc_base = self._n64_to_proc(n64_addr & ~3)
        if proc_base is None:
            return None
        # Pad to cover the first and last aligned words
        lead   = n64_addr & 3
        padded = (lead + size + 3) & ~3
        raw = self._read_raw(proc_base, padded)
        if not raw or len(raw) < lead + size:
            return None
        # Unswizzle: N64 byte at position p within raw lives at p ^ 3
        out = bytearray(size)
        for i in range(size):
            out[i] = raw[(lead + i) ^ 3]
        return bytes(out)

    def read_rdram(self, offset, size):
        return self.read_n64(BASE_ADDR + offset, size)

    # ── Public write API ──────────────────────────────────────────────────────

    def _write_n64_bytes(self, n64_addr, be_bytes):
        for i, byte_val in enumerate(be_bytes):
            addr = n64_addr + i
            aligned = addr & ~3
            proc_addr = self._n64_to_proc(aligned)
            if proc_addr is None:
                return False
            raw = self._read_raw(proc_addr, 4)
            if not raw or len(raw) < 4:
                return False
            buf = bytearray(raw)
            byte_offset_in_word = addr & 3
            buf_index = 3 - byte_offset_in_word
            buf[buf_index] = byte_val & 0xFF
            if not self._write_raw(proc_addr, bytes(buf)):
                return False
        return True

    def write_u8(self, n64_addr, value):
        return self._write_n64_bytes(n64_addr, [value & 0xFF])

    def write_u16_be(self, n64_addr, value):
        v = value & 0xFFFF
        return self._write_n64_bytes(n64_addr, [(v >> 8) & 0xFF, v & 0xFF])

    def write_u32_be(self, n64_addr, value):
        v = value & 0xFFFFFFFF
        return self._write_n64_bytes(n64_addr, [
            (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF
        ])

    def write_u64_be(self, n64_addr, value):
        v = value & 0xFFFFFFFFFFFFFFFF
        return self._write_n64_bytes(n64_addr, [
            (v >> 56) & 0xFF, (v >> 48) & 0xFF, (v >> 40) & 0xFF, (v >> 32) & 0xFF,
            (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >>  8) & 0xFF,  v         & 0xFF,
        ])

    # ── Heap walker ───────────────────────────────────────────────────────────────

    # Bulk-prefetch the whole heap range in one ReadProcessMemory call when
    # it's boxed to a sane size (BK is ~2.1MB, BT ~2.9MB) instead of doing a
    # separate small read per block - walking a heap with a few hundred
    # blocks previously meant a few hundred separate syscalls every single
    # refresh, which was the single biggest cost in the app and the main
    # thing left competing with Tk for scroll responsiveness. Capped well
    # above BK/BT's actual size but far below the Xenia profiles' huge/
    # unknown bounds (heap_size 0 or 512MB), which still use the old
    # one-read-per-block path unchanged below.
    _HEAP_BULK_READ_CAP = 8 * 1024 * 1024

    def _read_n64_bulk_fast(self, n64_addr, size):
        """Like read_n64(), but for large 4-byte-aligned reads.

        read_n64()'s per-byte Python loop to undo BizHawk's word-swizzling
        is fine for the handful of bytes a single field read needs, but
        would be a real bottleneck at the multi-megabyte size this is for
        (a plain Python loop over a few million bytes). array.array's
        byteswap() reverses every 4-byte element in C instead, with no
        per-element Python overhead, so this stays fast at bulk sizes.
        Caller must pass a 4-byte-aligned n64_addr and size.
        """
        if size <= 0:
            return b""
        proc_base = self._n64_to_proc(n64_addr)
        if proc_base is None:
            return None
        padded = (size + 3) & ~3
        raw = self._read_raw(proc_base, padded)
        if not raw or len(raw) < padded:
            return None
        if array.array("I").itemsize != 4:
            # Extremely unlikely (would need a C `unsigned int` that isn't
            # 4 bytes), but fall back to the safe byte-at-a-time path
            # rather than risk silently mis-swizzling data.
            return self.read_n64(n64_addr, size)
        # Interpret the buffer as 32-bit words (native/little-endian on
        # Windows) and byteswap in place: reinterpreting the same word in
        # the opposite endianness reverses its 4 bytes, identical to
        # read_n64's byte^3 swizzle, just done for the whole buffer in C.
        arr = array.array("I")
        arr.frombytes(raw)
        arr.byteswap()
        return arr.tobytes()[:size]

    def walk_heap(self):
        """
        Walk the heap linked list for the active game profile.
        Returns a list of block dicts.
        """
        p = self.profile
        blocks  = []
        visited = set()
        addr    = p.heap_start
        max_blocks = 4096
        heap_end   = p.heap_start + p.heap_size

        bulk = None
        if 0 < p.heap_size <= self._HEAP_BULK_READ_CAP and (p.heap_start & 3) == 0:
            # + 0x1000 slack mirrors the walk loop's own boundary check below.
            bulk = self._read_n64_bulk_fast(p.heap_start, p.heap_size + 0x1000)

        while addr >= p.heap_start and addr < heap_end + 0x1000 and len(blocks) < max_blocks:
            if addr in visited:
                break
            visited.add(addr)

            block = None
            if bulk is not None:
                off = addr - p.heap_start
                # _parse_heap_header itself handles the case where off is
                # near the end of the buffer (enough for the header but not
                # the optional free-list fields) by falling back to one
                # small live read just for those.
                if 0 <= off and off + 0x10 <= len(bulk):
                    block = self._parse_heap_header(bulk, off, addr)
            if block is None:
                # Outside the bulk buffer (or no bulk buffer at all, e.g.
                # Xenia's unbounded/huge heap_size) - fall back to the
                # original single-block live read.
                block = self._read_heap_header(addr)
            if block is None:
                break

            blocks.append(block)

            if block["next"] <= addr or block["next"] > heap_end + 0x1000:
                break
            if block["next"] >= heap_end:
                break

            addr = block["next"]

        return blocks

    def _parse_heap_header(self, data, off, addr):
        """Parse one heap block header out of an already-fetched buffer.

        `off` is the byte offset within `data` corresponding to `addr`.
        If `data` doesn't extend far enough past the header to cover the
        free-list fields (prev_free/next_free, only present for free
        blocks), those are topped up with one small live read - same as
        the original single-read implementation did unconditionally.
        """
        if off < 0 or off + 0x10 > len(data):
            return None

        prev_ptr = struct.unpack_from(">I", data, off + 0x0)[0]
        next_ptr = struct.unpack_from(">I", data, off + 0x4)[0]
        b0 = data[off + 0xC]
        b1 = data[off + 0xD]
        b2 = data[off + 0xE]
        b3 = data[off + 0xF]

        unused_bytes = b0 * 0x10000 + b1 * 0x100 + b2
        state = b3 >> 6

        total_span = next_ptr - addr
        chunk_size = total_span - HEAP_HEADER_SIZE
        used_size  = chunk_size - unused_bytes

        block = {
            "addr":       addr,
            "prev":       prev_ptr,
            "next":       next_ptr,
            "state":      state,
            "unused":     unused_bytes,
            "chunk_size": max(chunk_size, 0),
            "used_size":  max(used_size, 0),
            "end_addr":   next_ptr - 1,
        }

        if state == HEAP_STATE_EMPTY:
            if off + 0x18 <= len(data):
                block["prev_free"] = struct.unpack_from(">I", data, off + 0x10)[0]
                block["next_free"] = struct.unpack_from(">I", data, off + 0x14)[0]
            else:
                extra = self.read_n64(addr + 0x10, 8)
                if extra and len(extra) >= 8:
                    block["prev_free"] = struct.unpack_from(">I", extra, 0)[0]
                    block["next_free"] = struct.unpack_from(">I", extra, 4)[0]

        return block

    def _read_heap_header(self, addr):
        """Live single-block read (original implementation) - used as a
        fallback when no bulk buffer applies (Xenia's huge/unknown-size
        heaps) or a block falls outside the prefetched range."""
        data = self.read_n64(addr, 0x10)
        if not data or len(data) < 0x10:
            return None
        block = self._parse_heap_header(data, 0, addr)
        return block

    def heap_summary(self, blocks):
        s = {
            "block_count": len(blocks),
            "free_count": 0, "used_count": 0, "perm_count": 0,
            "computed_free": 0, "computed_occupied": 0, "largest_free": 0,
        }
        for b in blocks:
            if b["state"] == HEAP_STATE_EMPTY:
                s["free_count"] += 1
                s["computed_free"] += b["chunk_size"] + HEAP_HEADER_SIZE
                if b["chunk_size"] > s["largest_free"]:
                    s["largest_free"] = b["chunk_size"]
            elif b["state"] == HEAP_STATE_USED:
                s["used_count"] += 1
                s["computed_occupied"] += b["chunk_size"] + HEAP_HEADER_SIZE
            elif b["state"] == HEAP_STATE_PERM:
                s["perm_count"] += 1
        return s

    def dump_region_info(self):
        base = f"0x{self.rdram_base:016X}" if self.rdram_base else "unknown"
        return f"RDRAM_BASE={base}  PID={self.pid}  Game={self.profile.name}"


# ── Xenia-canary reader ───────────────────────────────────────────────────────

class XeniaMemoryReader:
    """
    Reads Xbox 360 memory from a running Xenia-canary process.

    Xenia maps the guest physical address space at a fixed host base address
    (XENIA_PHYS_BASE = 0x100000000).  Unlike BizHawk/N64 there is no byte-
    swizzle: Xenia stores memory in the same big-endian order as the Xbox 360.

    For the heap walker, the Xenia BT heap uses a slab allocator with a fixed
    0x10000-byte stride between slab starts (regardless of how much data each
    slab holds).  The walker reads slabs directly from host process memory.
    """

    XENIA_PROCESS_NAMES = [b"xenia_canary.exe", b"xenia-canary.exe", b"xenia.exe"]

    def __init__(self, profile: GameProfile = None):
        self.profile    = profile or XENIA_BT_PROFILE
        self.pid        = None
        self.handle     = None
        # Host address of Xenia's guest physical base (detected at connect time).
        # For Xenia-canary this is almost always XENIA_PHYS_BASE, but we verify.
        self._phys_base: Optional[int] = None
        self._k32 = ctypes.windll.kernel32 if sys.platform == "win32" else None

    @property
    def connected(self):
        return self.handle is not None and self._phys_base is not None

    def set_profile(self, profile: GameProfile, clear_rdram: bool = True):
        self.profile = profile
        # clear_rdram semantics re-used: when True we forget the phys base so
        # the next connect() re-scans.
        if clear_rdram:
            self._phys_base = None

    # ── Connection ────────────────────────────────────────────────────────────

    def find_xenia_pid(self):
        if not self._k32:
            return None
        snap = self._k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == ctypes.wintypes.HANDLE(-1).value:
            return None
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        try:
            if not self._k32.Process32First(snap, ctypes.byref(entry)):
                return None
            while True:
                name_lower = entry.szExeFile.lower()
                for pname in self.XENIA_PROCESS_NAMES:
                    if name_lower == pname.lower():
                        return entry.th32ProcessID
                if not self._k32.Process32Next(snap, ctypes.byref(entry)):
                    break
        finally:
            self._k32.CloseHandle(snap)
        return None

    def connect(self):
        """
        Connect to Xenia-canary and verify the guest physical mapping.
        Returns (ok, message, detected_profile_or_None).
        """
        if sys.platform != "win32":
            return False, "Xenia is Windows-only.", None

        self.pid = self.find_xenia_pid()
        if not self.pid:
            return False, (
                "Xenia-canary (xenia_canary.exe) not found. "
                "Start Xenia and load a Banjo game."
            ), None

        self.handle = self._k32.OpenProcess(
            PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION,
            False, self.pid)
        if not self.handle:
            return False, f"Can't open Xenia (PID {self.pid}). Run as Administrator.", None

        phys_base = self._find_phys_base()
        if phys_base is None:
            self._k32.CloseHandle(self.handle)
            self.handle = None
            return False, (
                "Could not locate Xenia guest memory. "
                "Is a Banjo game loaded and running?"
            ), None

        self._phys_base = phys_base

        # Auto-detect which game is running by trying to identify the title.
        detected = self._detect_game()
        if detected is not None and detected is not self.profile:
            self.profile = detected

        return True, (
            f"Connected (Xenia)  PID={self.pid}  "
            f"PHYS_BASE=0x{self._phys_base:016X}  "
            f"Game={self.profile.name}"
        ), self.profile

    def disconnect(self):
        if self.handle and self._k32:
            self._k32.CloseHandle(self.handle)
        self.handle = None
        self.pid    = None
        self._phys_base = None

    # ── Guest physical memory scan ────────────────────────────────────────────

    def _find_phys_base(self):
        """
        Locate the host address where Xenia has mapped guest physical byte 0.

        Xenia-canary consistently maps guest physical memory at host address
        0x100000000 (4 GB mark).  We verify this by checking that the region
        starting there is committed, large enough, and readable.  If not found
        at the canonical address we fall back to a VirtualQueryEx scan for a
        large committed region near 0x100000000.
        """
        if not self._k32 or not self.handle:
            return None

        # Try the canonical Xenia address first — fast path.
        candidate = XENIA_PHYS_BASE
        mbi = MEMORY_BASIC_INFORMATION()
        ret = self._k32.VirtualQueryEx(
            self.handle, ctypes.c_void_p(candidate),
            ctypes.byref(mbi), ctypes.sizeof(mbi))
        if ret and mbi.State == MEM_COMMIT and mbi.RegionSize >= 0x10000000:
            return candidate

        # Slow path: scan near the 4 GB mark.
        addr = 0x0F0000000
        while addr < 0x200000000:
            ret = self._k32.VirtualQueryEx(
                self.handle, ctypes.c_void_p(addr),
                ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not ret:
                break
            if (mbi.State == MEM_COMMIT and
                    mbi.RegionSize >= 0x10000000 and
                    mbi.Protect in (PAGE_READWRITE, PAGE_EXECUTE_READWRITE)):
                return mbi.BaseAddress
            if mbi.RegionSize == 0:
                break
            addr = (mbi.BaseAddress or addr) + mbi.RegionSize

        return None

    def _detect_game(self):
        """
        Detect whether Banjo-Tooie or Banjo-Kazooie is running under Xenia.

        Strategy: try to validate the first BT heap slab.  The BT heap uses a
        fixed start address (XENIA_BT_HEAP_START); its first u32 is the
        self-pointer low32, which must equal
        (XENIA_BT_HEAP_START - XENIA_PHYS_BASE) & 0xFFFFFFFF.
        If that check passes → Tooie.  Otherwise → Kazooie.
        """
        if self._phys_base is None:
            return self.profile

        data = self._read_raw(XENIA_BT_HEAP_START, 4)
        if data and len(data) >= 4:
            self_low = struct.unpack_from(">I", data)[0]
            expected = (XENIA_BT_HEAP_START - XENIA_PHYS_BASE) & 0xFFFFFFFF
            if self_low == expected:
                return XENIA_BT_PROFILE

        return XENIA_BK_PROFILE

    # ── Raw host memory read ──────────────────────────────────────────────────

    def _read_raw(self, host_addr, size):
        if not self.handle or not self._k32:
            return None
        buf = ctypes.create_string_buffer(size)
        n   = ctypes.c_size_t(0)
        self._k32.ReadProcessMemory.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        ok = self._k32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(host_addr),
            buf, size, ctypes.byref(n))
        if ok and n.value > 0:
            return bytes(buf.raw[:n.value])
        return None

    def _write_raw(self, host_addr, data):
        if not self.handle or not self._k32:
            return False
        buf = ctypes.create_string_buffer(bytes(data))
        n   = ctypes.c_size_t(0)
        ok = self._k32.WriteProcessMemory(
            self.handle, ctypes.c_void_p(host_addr),
            buf, len(data), ctypes.byref(n))
        return bool(ok and n.value == len(data))

    # ── Guest address ↔ host address ──────────────────────────────────────────

    def _guest_to_host(self, guest_addr):
        """
        Convert a guest virtual address to a host address.

        Xbox 360 virtual addresses starting with 0x80000000 are in the
        kernel-mapped region that maps directly to guest physical memory,
        so virtual 0x80000000 == physical 0x00000000.
        We apply that mapping and add our detected phys_base.
        """
        if self._phys_base is None:
            return None
        phys = guest_addr & 0x1FFFFFFF   # strip the top segment bits
        return self._phys_base + phys

    # ── Public read/write API (mirrors BizHawkMemoryReader interface) ──────────

    def read_u8(self, addr):
        """addr may be a guest N64-style address (for watch compatibility)."""
        host = self._addr_to_host(addr)
        if host is None:
            return None
        data = self._read_raw(host, 1)
        return data[0] if data else None

    def read_u16_be(self, addr):
        host = self._addr_to_host(addr)
        if host is None:
            return None
        data = self._read_raw(host, 2)
        if not data or len(data) < 2:
            return None
        return struct.unpack_from(">H", data)[0]

    def read_u32_be(self, addr):
        host = self._addr_to_host(addr)
        if host is None:
            return None
        data = self._read_raw(host, 4)
        if not data or len(data) < 4:
            return None
        return struct.unpack_from(">I", data)[0]

    def read_s32_be(self, addr):
        v = self.read_u32_be(addr)
        if v is None:
            return None
        return struct.unpack(">i", struct.pack(">I", v))[0]

    def read_n64(self, addr, size):
        """
        Read *size* bytes from a guest address.
        Unlike BizHawk, Xenia stores memory in big-endian order natively,
        so no byte-swap is needed.
        """
        host = self._addr_to_host(addr)
        if host is None:
            return None
        return self._read_raw(host, size)

    def write_u8(self, addr, value):
        host = self._addr_to_host(addr)
        if host is None:
            return False
        return self._write_raw(host, bytes([value & 0xFF]))

    def write_u32_be(self, addr, value):
        host = self._addr_to_host(addr)
        if host is None:
            return False
        return self._write_raw(host, struct.pack(">I", value & 0xFFFFFFFF))

    # ── Internal address resolution ───────────────────────────────────────────

    def _addr_to_host(self, addr):
        """
        Resolve any address the UI might pass us.

        - If addr >= XENIA_PHYS_BASE (i.e. already a host address), use as-is.
        - If addr looks like an N64/Xbox guest address (0x80xxxxxx), map it.
        - Otherwise return None.
        """
        if self._phys_base is None:
            return None
        if addr >= XENIA_PHYS_BASE:
            # Already a host address (e.g. from the heap walker).
            return addr
        if addr >= 0x80000000:
            # Xbox 360 kernel-space virtual address.
            return self._guest_to_host(addr)
        return None

    # ── Heap walker ───────────────────────────────────────────────────────────

    def walk_heap(self):
        """
        Walk the Xenia-canary BT heap.

        Node layout (starts on a 0x10000-byte boundary):
          +0x00  u32  self_addr_low  (== host_addr - XENIA_PHYS_BASE)
          +0x04  u32  data_length    (payload bytes; may exceed 0x10000)
          +0x08  ...  header padding to byte 0x3F
          +0x40  <payload bytes>
          after payload: XENIA_BT_FOOTER_SIZE bytes of 0xDEDEDEDE (not read)

        The next node starts at the first 0x10000-aligned address AFTER
        (hdr_sz + data_length + footer_sz) bytes from this node's start.
        So for a node at host H with data_length D:
            next_node = H + ceil((HDR + D + FOOTER) / STRIDE) * STRIDE

        The heap end is not known statically.  We stop walking when:
          - The self-pointer word doesn't match the expected value, OR
          - data_length is implausibly large (> XENIA_MEM_SIZE safety cap), OR
          - We've read XENIA_BT_MAX_NODES blocks.

        For the xenia_bk profile (heap not yet found) returns [].
        """
        p = self.profile

        if p.id == "xenia_bk":
            return self._walk_heap_bk()
        if p.id != "xenia_bt":
            return []
        if self._phys_base is None:
            return []

        stride    = XENIA_BT_SLAB_STRIDE
        hdr_sz    = XENIA_BT_HDR_SIZE
        footer_sz = XENIA_BT_FOOTER_SIZE
        blocks    = []
        host      = XENIA_BT_HEAP_START

        while len(blocks) < XENIA_BT_MAX_NODES:
            data = self._read_raw(host, hdr_sz)
            if not data or len(data) < hdr_sz:
                break

            self_low = struct.unpack_from(">I", data, 0x00)[0]
            data_len = struct.unpack_from(">I", data, 0x04)[0]

            # Self-pointer sanity check — mismatch means end of heap.
            expected_low = (host - XENIA_PHYS_BASE) & 0xFFFFFFFF
            if self_low != expected_low:
                break

            # Implausibly large payload — treat as end of heap.
            if data_len > XENIA_MEM_SIZE:
                break

            # State: FREE if data_len == 0, else USED.
            state = HEAP_STATE_EMPTY if data_len == 0 else HEAP_STATE_USED

            # How many 0x10000 slots does this node occupy?
            node_content = hdr_sz + data_len + footer_sz
            slots = (node_content + stride - 1) // stride   # ceil division
            node_span = slots * stride   # total host bytes occupied

            block = {
                "addr":       host,
                "end_addr":   host + node_span - 1,
                "prev":       host,   # no explicit prev pointer in this format
                "next":       host + node_span,
                "state":      state,
                # chunk_size = usable payload capacity for this node's slot(s)
                "chunk_size": slots * stride - hdr_sz - footer_sz,
                "used_size":  data_len,
                "unused":     (slots * stride - hdr_sz - footer_sz) - data_len,
                # Xenia-specific extras for the detail pane
                "xenia_self_low": self_low,
                "xenia_data_len": data_len,
                "xenia_slots":    slots,
            }
            blocks.append(block)
            host += node_span

        return blocks

    def _walk_heap_bk(self):
        """
        Walk the Xenia-canary BK heap.

        The heap starts at the fixed host address XENIA_BK_HEAP_START.

        Node header layout (big-endian, 0x60 bytes total):
          offset 0x20: self-ptr low32  — for USED nodes: (node_host + 0x20 - XENIA_PHYS_BASE)
                                         for FREE nodes: 0x00000000
          offset 0x24: used size       — for USED nodes: bytes of payload data;
                                         chunk_size = (used_size + 0xF) & ~0xF
                                         for FREE nodes: 0x00000000

        For free nodes the span is unknown from the header alone, so we scan
        forward in 0x10-byte steps until we find the next valid self-pointer
        (i.e. the next used or free node boundary).
        """
        if self._phys_base is None:
            return []

        hdr_sz = XENIA_BK_HDR_SIZE

        # ── Walk nodes ────────────────────────────────────────────────────────
        blocks = []
        host   = XENIA_BK_HEAP_START

        while len(blocks) < XENIA_BK_MAX_NODES:
            data = self._read_raw(host, hdr_sz)
            if not data or len(data) < hdr_sz:
                break

            self_low  = struct.unpack_from(">I", data, XENIA_BK_SELF_OFF)[0]
            used_size = struct.unpack_from(">I", data, XENIA_BK_SIZE_OFF)[0]

            # Self-pointer sanity check — mismatch means end of heap.
            expected_low = (host - XENIA_PHYS_BASE + XENIA_BK_SELF_OFF) & 0xFFFFFFFF
            #print(hex(expected_low) + ", " + hex(self_low))
            if self_low != expected_low:
                break

            # Implausibly large value — treat as end of heap.
            if used_size > XENIA_MEM_SIZE:
                break

            # Chunk size is used_size rounded up to the next 0x10 boundary.
            # e.g. used=0x258 → chunk=0x260, used=0x90 → chunk=0x90 (already aligned).
            chunk_size = (used_size + 0xF) & ~0xF
            unused     = chunk_size - used_size
            node_span  = hdr_sz + chunk_size
            state      = HEAP_STATE_EMPTY if used_size == 0 else HEAP_STATE_USED

            block = {
                "addr":       host,
                "end_addr":   host + node_span - 1,
                "prev":       host,         # no explicit prev pointer
                "next":       host + node_span,
                "state":      state,
                "chunk_size": chunk_size,
                "used_size":  used_size,
                "unused":     unused,
                # BK-Xenia-specific extras
                "xenia_self_low": self_low,
                "xenia_data_len": used_size,
            }
            blocks.append(block)
            host += node_span

        return blocks

    def heap_summary(self, blocks):
        """Same interface as BizHawkMemoryReader.heap_summary()."""
        s = {
            "block_count": len(blocks),
            "free_count": 0, "used_count": 0, "perm_count": 0,
            "computed_free": 0, "computed_occupied": 0, "largest_free": 0,
        }
        for b in blocks:
            if b["state"] == HEAP_STATE_EMPTY:
                s["free_count"] += 1
                s["computed_free"] += b["chunk_size"]
                if b["chunk_size"] > s["largest_free"]:
                    s["largest_free"] = b["chunk_size"]
            elif b["state"] == HEAP_STATE_USED:
                s["used_count"] += 1
                s["computed_occupied"] += b["used_size"]
            elif b["state"] == HEAP_STATE_PERM:
                s["perm_count"] += 1
        return s

    def dump_region_info(self):
        base = f"0x{self._phys_base:016X}" if self._phys_base else "unknown"
        return f"PHYS_BASE={base}  PID={self.pid}  Game={self.profile.name}"

    def read_rdram(self, offset, size):
        """Compatibility shim — not meaningful for Xenia."""
        return None
