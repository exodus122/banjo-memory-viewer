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

# Xenia BK heap: contiguous nodes, bounded by a heap descriptor.
#
# Every object in the heap — including the descriptor itself — carries the same
# 0x50-byte big-endian header.  "P" below is the canonical node pointer used by
# the allocator (the descriptor's +0x28 field points at the first node's P):
#
#   P+0x00  u16  size16       total span of this node >> 4
#   P+0x02  u16  prev_size16  previous node's size16 (0 => first node)
#   P+0x04  u32  flags        bit 0x1000 set => real allocation
#   P+0x08  u32  flink        LIST_ENTRY; == &bins[0] (0x40000180) when unlinked
#   P+0x0C  u32  blink
#
# That 0x10 is the ENTIRE base header.  Nodes then come in two shapes, and which
# one you are looking at is decided by the word at P+0x10:
#
#   TRACKED   u32 at P+0x10 == guest(P+0x10)   (a self-pointer)
#     P+0x10  u32  self       its own address — doubles as the "I am tracked"
#                             marker; a false positive is a 1-in-2^32 accident
#     P+0x14  u32  data_size  bytes originally *requested*
#     P+0x18  ...  0x48 bytes that are NEVER initialised.  On fresh memory they
#                  read as zero; on reused memory they hold whatever the
#                  previous occupant left (floats, "SETT", "RANK", ...).  Do not
#                  read anything into them.
#     P+0x60  payload,  capacity == span - 0x60
#     and round_up(data_size, 0x10) == capacity
#
#   UNTRACKED  anything else at P+0x10
#     P+0x10  payload,  capacity == span - 0x10
#     There is no recorded request size for these, so "used" is reported as the
#     full capacity rather than invented.
#
#   FREED     u32 at P+0x10 == 0, and flags bit 0x1000 clear
#     data_size is stale garbage.  The whole span is reclaimable.
#
# A DESCRIPTOR carries 0xFFEEFFEE at P+0x10 and has no payload at all.
#
# The node span is always size16 << 4 — never derived from data_size.
#
# IMPORTANT: a *freed* node is not required to be 0x60 or larger.  Free nodes as
# small as 0x20 total exist (header fields through P+0x18 only), because freeing
# keeps just size16/prev16/flags/flink/blink and zeroes the self-pointer.  Any
# "span must be >= overhead" sanity check will reject real free nodes and
# desynchronise the walk.
#
# Descriptor-only fields (same 0x50 header, no payload, size16 == 5):
#   D+0x18 allocator root    D+0x1C trailing free bytes   D+0x20 heap base
#   D+0x24 alignment         D+0x28 first node            D+0x2C heap end (excl)
#   D+0x30, D+0x34 counters  D+0x40 tail node
#
# Free-list bins live in the allocator root block at guest 0x40000180: 128
# LIST_ENTRYs of 8 bytes.  An unlinked node's flink/blink are poisoned to
# &bins[0], which is why so many nodes carry 0x40000180 at +0x08.
# The allocator root block sits at the base of the Xbox 360 physical mapping and
# is the one address here that is genuinely fixed.  Its header holds pointers to
# the heap descriptors, so the game heap is discovered from it at walk time
# rather than hardcoded — the game heap is created at runtime and its address is
# NOT stable across sessions.  The two guest addresses below are the values
# observed in one session and are used only as first-guess candidates.
XENIA_BK_ROOT_GUEST      = 0x40000000  # allocator root / bin table block
XENIA_BK_DESC_GUEST      = 0x41A90000  # game heap descriptor  (observed; may move)
XENIA_BK_CTRL_DESC_GUEST = 0x40000630  # allocator control heap (0x40000000..0x40100000)

XENIA_BK_BASE_HDR      = 0x10          # base header every node has
XENIA_BK_HDR_SIZE      = 0x60          # tracked node header; payload at P+0x60
XENIA_BK_OVERHEAD      = 0x60          # span - round_up(data_size) when tracked
XENIA_BK_GRANULARITY   = 0x10          # unit of size16 / prev_size16
XENIA_BK_HDR_READ      = 0x20          # bytes we must actually read per node;
                                       # a 0x20 free node is all the memory
                                       # there is, so never read more
XENIA_BK_DESC_READ     = 0x50          # descriptors do extend this far

XENIA_BK_SIZE16_OFF    = 0x00
XENIA_BK_PREVSIZE_OFF  = 0x02
XENIA_BK_FLAGS_OFF     = 0x04
XENIA_BK_FLINK_OFF     = 0x08
XENIA_BK_BLINK_OFF     = 0x0C
XENIA_BK_SELF_OFF      = 0x10          # self-ptr word (was wrongly 0x20)
XENIA_BK_SIZE_OFF      = 0x14          # payload size word (was wrongly 0x24)

XENIA_BK_DESC_MAGIC    = 0xFFEEFFEE
XENIA_BK_DESC_ROOT_OFF    = 0x18
XENIA_BK_DESC_FREE_OFF    = 0x1C
XENIA_BK_DESC_BASE_OFF    = 0x20
XENIA_BK_DESC_ALIGN_OFF   = 0x24
XENIA_BK_DESC_FIRST_OFF   = 0x28
XENIA_BK_DESC_END_OFF     = 0x2C
XENIA_BK_DESC_COUNTA_OFF  = 0x30
XENIA_BK_DESC_COUNTB_OFF  = 0x34
XENIA_BK_DESC_REGIONS_OFF = 0x38   # head of this heap's free-REGION list
XENIA_BK_DESC_TAIL_OFF    = 0x40

# Observed flags: 0x02011000 (game heap allocation), 0x02010000 (game heap
# descriptor), 0x00011000 / 0x00011C00 (control heap allocations), 0x00010000
# (control heap root block and descriptor).  Bit 0x1000 tracks "is a real
# allocation" in all six samples, but no confirmed *free* block has been seen
# yet — so treat EMPTY as provisional and cross-check against the bin lists.
XENIA_BK_FLAG_ALLOCATED = 0x00001000
XENIA_BK_FLAG_FREED     = 0x00002000   # seen set on freed nodes

# Bit 16 means the node is an EXACT fit: capacity == round_up(data_size, 0x10).
# When it is clear the allocator satisfied the request out of a larger block and
# did not split off the remainder, so capacity can exceed the request by a lot
# (observed: a 0x1110 request living in a 0x1DF0 block).  That is normal
# behaviour, not corruption — the same request size appears exact-fitted
# elsewhere in the same heap.
XENIA_BK_FLAG_EXACT     = 0x00010000

# Bit 20 marks a node that ENDS ON A 64KB COMMIT BOUNDARY.
#
# It was initially mistaken for an end-of-chain marker: in four consecutive
# dumps the only node carrying it was the last one, because the trailing
# uncarved block necessarily runs up to the edge of committed memory.  But a
# perfectly ordinary allocation that happens to end on a boundary carries it
# too (0x141B1E360, span 0x1CA0, valid self-pointer, ending at 0x141B20000),
# and treating the bit as "stop here" truncated that heap from ~280 nodes to 27.
#
# So it is a HINT ONLY.  The walk stops when no valid node follows, which is
# verified by reading the next header — never on the strength of this bit.
XENIA_BK_FLAG_PAGE_END  = 0x00100000

# Bits 8-11 hold the ROUNDING padding only: round_up(data_size, 0x10) -
# data_size.  It is NOT the total slack; the two coincide only for exact-fit
# nodes.  Verified across two full walks (0x1000->0, 0x1400->4, 0x1800->8,
# 0x1C00->12, 0x1E00->14, and on every mismatch row it equalled the rounding
# padding rather than capacity - data_size).
XENIA_BK_FLAG_PAD_MASK  = 0x00000F00
XENIA_BK_FLAG_PAD_SHIFT = 8
XENIA_BK_BIN_TABLE      = 0x40000180   # &bins[0]; 128 × 8-byte LIST_ENTRY
XENIA_BK_BIN_COUNT      = 128
XENIA_BK_BIN_TABLE_END  = XENIA_BK_BIN_TABLE + XENIA_BK_BIN_COUNT * 8

XENIA_BK_MAX_NODES     = 8192

# Reported for a stretch of heap that could not be parsed as node headers and
# was skipped by resync.  Deliberately not one of HEAP_STATE_EMPTY/USED/PERM:
# the UI degrades unknown states to "UNK", which is the honest label — calling
# an unparsed gap "free" would be a lie the summary would then total up.
HEAP_STATE_UNPARSED    = 3

# Kept for backwards compatibility: host address of the first *node* (not the
# descriptor, which sits 0x50 earlier at 0x141A90000).
XENIA_BK_HEAP_START    = XENIA_PHYS_BASE + XENIA_BK_DESC_GUEST + XENIA_BK_HDR_SIZE

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
        # Guest address of the BK game-heap descriptor, discovered on first walk
        # and cached (heap descriptors do not move once the heap is created).
        self._bk_desc_guest: Optional[int] = None
        # Every heap descriptor found, cached; the game uses several at once.
        self._bk_desc_list: list = []
        # Host address of guest 0, as confirmed by an actual descriptor read.
        # _find_phys_base() can fall through to a scan and return something
        # other than XENIA_PHYS_BASE, so we don't take its word for it.
        self._bk_host_base: Optional[int] = None

    @property
    def connected(self):
        return self.handle is not None and self._phys_base is not None

    def set_profile(self, profile: GameProfile, clear_rdram: bool = True):
        self.profile = profile
        # clear_rdram semantics re-used: when True we forget the phys base so
        # the next connect() re-scans.
        if clear_rdram:
            self._phys_base = None
            self._bk_desc_guest = None
            self._bk_desc_list = []
            self._bk_host_base = None

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
        #
        # The size threshold used to be 0x10000000 (256MB) in a SINGLE region,
        # but Xenia splits the guest mapping into smaller regions with differing
        # protections, so that test failed and the scan below picked an
        # unrelated allocation (observed: 0x1A0000000, which reads as all
        # zeroes).  A committed region plus a successful read is the honest
        # check — guest physical 0 is what we want, not the largest region.
        candidate = XENIA_PHYS_BASE
        mbi = MEMORY_BASIC_INFORMATION()
        ret = self._k32.VirtualQueryEx(
            self.handle, ctypes.c_void_p(candidate),
            ctypes.byref(mbi), ctypes.sizeof(mbi))
        if ret and mbi.State == MEM_COMMIT and mbi.RegionSize >= 0x100000:
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
            # Game/level heap only.  walk_all_bk_heaps() also exists if the
            # shell and buffer heaps are ever wanted.
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

    def _bk_host_base_candidates(self):
        """
        Host addresses that guest 0 might map to, best guess first.

        _find_phys_base() prefers XENIA_PHYS_BASE but falls back to a
        VirtualQueryEx scan, which can return a different region base if Xenia
        split its mapping.  The rest of this file's Xenia code sidesteps that by
        using the XENIA_PHYS_BASE constant directly, so we try both and let an
        actual descriptor read decide which is right.
        """
        seen, out = set(), []
        for base in (self._bk_host_base, self._phys_base, XENIA_PHYS_BASE):
            if base is not None and base not in seen:
                seen.add(base)
                out.append(base)
        return out

    def confirm_bk_host_base(self):
        """
        Pin down the host base by finding a descriptor, before anything else
        reads guest memory.

        Without this, _bk_host() falls back to self._phys_base, which
        _find_phys_base() can get wrong — and then the allocator root and the
        region records read as zeroes and are silently reported as absent.
        """
        if self._bk_host_base is not None:
            return self._bk_host_base
        for guest in (XENIA_BK_CTRL_DESC_GUEST, XENIA_BK_DESC_GUEST,
                      self._bk_desc_guest):
            if guest is not None and self.read_bk_heap_descriptor(guest):
                break

        # A confirmed descriptor magic is a far better oracle than
        # _find_phys_base()'s region heuristic, which has been seen to return an
        # unrelated mapping (0x1A0000000) that reads as all zeroes.  Correct the
        # process-wide base too — every other view (watches, actors, hex) reads
        # through _phys_base and would otherwise be looking at the wrong memory.
        if self._bk_host_base is not None and self._bk_host_base != self._phys_base:
            self._phys_base = self._bk_host_base
        return self._bk_host_base

    def _bk_host(self, guest):
        """Host address for a guest address, using the confirmed base."""
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base if self._phys_base is not None
                      else XENIA_PHYS_BASE))
        return base + (guest & 0xFFFFFFFF)

    def read_bk_heap_descriptor(self, desc_guest=XENIA_BK_DESC_GUEST):
        """
        Read and validate a BK heap descriptor.  Returns a dict or None.

        The descriptor is itself the first node of the chain: a 0x50-byte header
        with no payload, identified by 0xFFEEFFEE sitting in the self-pointer
        slot.  It carries the heap bounds, so nothing downstream needs a
        hardcoded end address.

        On the first successful read the host base that worked is cached in
        self._bk_host_base and used for every subsequent guest->host mapping.
        """
        if self._phys_base is None:
            return None

        desc_guest &= 0xFFFFFFFF

        data = host = None
        for base in self._bk_host_base_candidates():
            probe = self._read_raw(base + desc_guest, XENIA_BK_DESC_READ)
            if probe and len(probe) >= XENIA_BK_DESC_READ and \
                    struct.unpack_from(">I", probe, XENIA_BK_SELF_OFF)[0] == XENIA_BK_DESC_MAGIC:
                data, host = probe, base + desc_guest
                self._bk_host_base = base
                break
        if data is None:
            return None

        def u32(off):
            return struct.unpack_from(">I", data, off)[0]

        def u16(off):
            return struct.unpack_from(">H", data, off)[0]

        desc = {
            "guest":       desc_guest,
            "host":        host,
            "size16":      u16(XENIA_BK_SIZE16_OFF),
            "prev_size16": u16(XENIA_BK_PREVSIZE_OFF),
            "flags":       u32(XENIA_BK_FLAGS_OFF),
            "root":        u32(XENIA_BK_DESC_ROOT_OFF),
            "free_bytes":  u32(XENIA_BK_DESC_FREE_OFF),
            "base":        u32(XENIA_BK_DESC_BASE_OFF),
            "alignment":   u32(XENIA_BK_DESC_ALIGN_OFF),
            "first":       u32(XENIA_BK_DESC_FIRST_OFF),
            "end":         u32(XENIA_BK_DESC_END_OFF),
            "count_a":     u32(XENIA_BK_DESC_COUNTA_OFF),
            "count_b":     u32(XENIA_BK_DESC_COUNTB_OFF),
            "regions":     u32(XENIA_BK_DESC_REGIONS_OFF),
            "tail":        u32(XENIA_BK_DESC_TAIL_OFF),
        }
        desc["size"] = (desc["end"] - desc["base"]) & 0xFFFFFFFF
        return desc

    def find_bk_heap_descriptors(self, deep=False):
        """
        Locate every BK heap descriptor reachable from the allocator root.

        The game-heap descriptor address is NOT stable across runs — the heap is
        created at runtime, so hardcoding 0x41A90000 only works for the session
        it was observed in.  What *is* stable is the allocator root at guest
        0x40000000 (the base of the Xbox 360 physical mapping): its header holds
        a table of pointers to the heaps it manages.

        Rather than trust a specific field offset in that table, we treat every
        plausible guest pointer in the root's first 0x100 bytes as a candidate
        and keep the ones that validate as descriptors (0xFFEEFFEE magic, a
        sane base/end pair that contains the descriptor itself).

        Returns a list of descriptor dicts, sorted largest heap first.
        """
        if self._phys_base is None:
            return []

        self.confirm_bk_host_base()
        found, seen = [], set()

        def consider(guest):
            guest &= 0xFFFFFFFF
            if guest in seen or guest % XENIA_BK_GRANULARITY:
                return
            seen.add(guest)
            d = self.read_bk_heap_descriptor(guest)
            if not d:
                return
            # A descriptor must sit inside the heap it describes, and that heap
            # must be a sane, non-empty forward range.
            if not (d["base"] <= guest < d["end"]):
                return
            if not (0 < d["size"] <= XENIA_MEM_SIZE):
                return
            if not (d["base"] <= d["first"] < d["end"]):
                return
            found.append(d)

        # Known-good starting points first.
        consider(XENIA_BK_CTRL_DESC_GUEST)
        if self._bk_desc_guest is not None:
            consider(self._bk_desc_guest)
        consider(XENIA_BK_DESC_GUEST)

        # Then every pointer-shaped word in the whole allocator root block.  The
        # root is 0x630 bytes; scanning only its first 0x100 finds the heaps
        # named in the header table but misses any listed further in.
        root = self._read_raw(self._bk_host(XENIA_BK_ROOT_GUEST), 0x630)
        if root:
            for off in range(0, len(root) - 3, 4):
                val = struct.unpack_from(">I", root, off)[0]
                if XENIA_BK_ROOT_GUEST <= val < XENIA_BK_ROOT_GUEST + XENIA_MEM_SIZE:
                    consider(val)

        # The region records also name memory ranges; each range base is a
        # candidate descriptor address.  Take the list heads from the
        # descriptors already found — calling read_bk_region_records() with no
        # head would resolve the game heap, which calls back into here.
        for d in list(found):
            for _, base, _, _ in self.read_bk_region_records(d["regions"]):
                consider(base)

        # Sweep for the magic when asked, or when the cheap paths turned up no
        # game heap at all.  The sweep is the only way to find a heap the root
        # does not reference.
        if deep or not any(d["base"] != XENIA_BK_ROOT_GUEST for d in found):
            for guest in self._scan_bk_descriptor_magic():
                consider(guest)

        found.sort(key=lambda d: d["size"], reverse=True)
        return found

    def _scan_bk_descriptor_magic(self, start=XENIA_BK_ROOT_GUEST,
                                  end=XENIA_BK_ROOT_GUEST + 0x02000000,
                                  chunk=0x100000):
        """
        Sweep guest memory for 0xFFEEFFEE in a descriptor's self-pointer slot.

        The magic lives at P+0x10 and P is 0x10-aligned, so only 0x10-aligned
        hits can be real; everything else is coincidence in payload data.
        Yields candidate descriptor addresses (P), which the caller must still
        validate — the magic alone is only 4 bytes and does occur by chance.
        """
        if self._phys_base is None:
            return

        magic = struct.pack(">I", XENIA_BK_DESC_MAGIC)
        guest = start
        while guest < end:
            size = min(chunk, end - guest)
            data = self._read_raw(self._bk_host(guest), size)
            if not data:
                guest += chunk          # unmapped hole — skip it
                continue
            idx = data.find(magic)
            while idx != -1:
                if idx % XENIA_BK_GRANULARITY == 0:
                    yield (guest + idx - XENIA_BK_SELF_OFF) & 0xFFFFFFFF
                idx = data.find(magic, idx + 1)
            guest += size

    # Guest range of the loaded XEX image.  Pointers into here are code or
    # static data, so a pointer stored in a payload often names the allocation
    # (a class vtable, or a literal string).
    XEX_LO = 0x82000000
    XEX_HI = 0x84000000

    def read_bk_cstring(self, guest, maxlen=64):
        """
        Read a printable NUL-terminated string at `guest`, or None.

        Requires at least 3 printable characters before the terminator so that
        random pointer-shaped bytes don't get reported as text.
        """
        data = self._read_raw(self._bk_host(guest), maxlen)
        if not data:
            return None
        out = []
        for ch in data:
            if ch == 0:
                break
            if ch < 0x20 or ch > 0x7E:
                return None
            out.append(chr(ch))
        if len(out) < 3:
            return None
        return "".join(out)

    @staticmethod
    def _ascii_runs(data, minlen=4):
        """Printable runs of at least `minlen` chars inside a byte string."""
        runs, cur = [], []
        for ch in data:
            if 0x20 <= ch <= 0x7E:
                cur.append(chr(ch))
            else:
                if len(cur) >= minlen:
                    runs.append("".join(cur))
                cur = []
        if len(cur) >= minlen:
            runs.append("".join(cur))
        return runs

    def survey_bk_heap(self, desc_guest, max_nodes=2000, scan=0x80):
        """
        Characterise a heap without needing names.

        Three views, cheapest first:
          * size histogram — many allocations of one exact size is a type, and
            the size alone often identifies it
          * text found ANYWHERE in the payload, not just at offset 0 (payloads
            here carry FourCCs like "SETT"/"RANK" well inside the block)
          * XEX pointers appearing at a consistent payload offset

        Returns (sizes, texts, pointers), each a list of (key, count) sorted by
        count descending.
        """
        sizes, texts, ptrs = {}, {}, {}

        for b in self._walk_heap_bk(desc_guest)[:max_nodes]:
            if b["state"] != HEAP_STATE_USED or not b.get("xenia_payload"):
                continue
            sizes[b["chunk_size"]] = sizes.get(b["chunk_size"], 0) + 1

            data = self._read_raw(b["xenia_payload"],
                                  min(scan, max(0x10, b["chunk_size"])))
            if not data:
                continue

            for s in self._ascii_runs(data):
                texts[s] = texts.get(s, 0) + 1

            for off in range(0, len(data) - 3, 4):
                w = struct.unpack_from(">I", data, off)[0]
                if self.XEX_LO <= w < self.XEX_HI:
                    ptrs[(off, w)] = ptrs.get((off, w), 0) + 1

        def top(d):
            return sorted(d.items(), key=lambda kv: kv[1], reverse=True)

        return top(sizes), top(texts), top(ptrs)

    def identify_bk_nodes(self, desc_guest, max_nodes=400, samples=3):
        """
        Try to name allocations by following pointers out of their payloads.

        Untracked nodes put payload at P+0x10, and many begin with a pointer to
        a vtable or a literal in the XEX image.  Group nodes by the first such
        pointer found: a pointer shared by many allocations is a type, and if it
        or the word next to it resolves to text, that text names the type.

        Returns a list of (pointer, count, total_bytes, sample_string, sizes).
        """
        groups = {}
        for b in self._walk_heap_bk(desc_guest)[:max_nodes]:
            if b["state"] != HEAP_STATE_USED or not b.get("xenia_payload"):
                continue
            head = self._read_raw(b["xenia_payload"], 8)
            if not head or len(head) < 8:
                continue
            w0, w1 = struct.unpack_from(">II", head, 0)
            ptr = next((w for w in (w0, w1) if self.XEX_LO <= w < self.XEX_HI),
                       None)
            if ptr is None:
                continue
            g = groups.setdefault(ptr, {"count": 0, "bytes": 0, "sizes": []})
            g["count"] += 1
            g["bytes"] += b["chunk_size"]
            if len(g["sizes"]) < samples:
                g["sizes"].append(b["chunk_size"])

        out = []
        for ptr, g in groups.items():
            # The pointer may be the string itself, or point at a struct whose
            # first field is a name pointer — try both.
            text = self.read_bk_cstring(ptr)
            if text is None:
                indirect = self._read_raw(self._bk_host(ptr), 4)
                if indirect and len(indirect) == 4:
                    inner = struct.unpack_from(">I", indirect, 0)[0]
                    if self.XEX_LO <= inner < self.XEX_HI:
                        text = self.read_bk_cstring(inner)
            out.append((ptr, g["count"], g["bytes"], text, g["sizes"]))
        out.sort(key=lambda r: r[1], reverse=True)
        return out

    def read_bk_region_records(self, head=None, limit=64):
        """
        Read a heap's free-region list:  {next, base, size, flags} × N.

        These 16-byte records describe memory the allocator tracks as free
        REGIONS rather than as heap nodes, so they account for space the node
        walk never sees.

        `head` defaults to the game heap's list.  Each heap has its OWN list —
        the head is descriptor+0x38 — so passing a single hardcoded head leaves
        the other heaps' free space unaccounted for.
        """
        if head is None:
            # Resolve without going through resolve_bk_heap_descriptor(), which
            # calls find_bk_heap_descriptors(), which calls back into here.
            desc = self.read_bk_heap_descriptor(
                self._bk_desc_guest if self._bk_desc_guest is not None
                else XENIA_BK_DESC_GUEST)
            head = desc["regions"] if desc else 0
        out, seen, guest = [], set(), head & 0xFFFFFFFF
        while guest and guest not in seen and len(out) < limit:
            seen.add(guest)
            data = self._read_raw(self._bk_host(guest), 0x10)
            if not data or len(data) < 0x10:
                break
            nxt, base, size, flags = struct.unpack_from(">IIII", data, 0)
            if base or size:
                out.append((guest, base, size, flags))
            guest = nxt & 0xFFFFFFFF
        return out

    def resolve_bk_heap_descriptor(self):
        """
        Guest address of the GAME/LEVEL heap's descriptor, or None.

        This is the heap whose contents track the loaded map.  The process has
        three:
            0x40000000  XBLA shell — D3D shaders, menu UI      align 0x10
            0x40220000  one dominant ~751KB buffer             align 0x10
            0x41A90000  game/level data                        align 0x20
        Only the last is of interest here.  It has been at 0x41A90000 in every
        session observed, so that is tried first; the fallback prefers the
        coarser 0x20 alignment, which is what distinguishes it from the
        allocator's own heaps.

        Do not substitute "largest" — this heap legitimately drops to 8 nodes at
        the XBLA menu while the shell heap holds 650.
        """
        if self._bk_desc_guest is not None:
            if self.read_bk_heap_descriptor(self._bk_desc_guest):
                return self._bk_desc_guest
            self._bk_desc_guest = None   # stale (game reloaded) — rediscover

        candidates = self.find_bk_heap_descriptors()
        if not candidates:
            return None

        chosen = next((d for d in candidates
                       if d["guest"] == XENIA_BK_DESC_GUEST), None)
        if chosen is None:
            game = [d for d in candidates if d["base"] != XENIA_BK_ROOT_GUEST]
            game.sort(key=lambda d: (d["alignment"], d["size"]), reverse=True)
            chosen = (game or candidates)[0]

        self._bk_desc_guest = chosen["guest"]
        return self._bk_desc_guest

    def list_bk_heaps(self, refresh=False):
        """Guest addresses of every heap descriptor, cached across refreshes."""
        if refresh or not self._bk_desc_list:
            descs = self.find_bk_heap_descriptors()
            # Sort by heap base so the concatenated walk comes out in address
            # order, which is what the heap view's address bar assumes.
            self._bk_desc_list = [d["guest"]
                                  for d in sorted(descs, key=lambda d: d["base"])]
        return self._bk_desc_list

    def walk_all_bk_heaps(self):
        """
        Walk every heap and return the blocks concatenated in address order.

        The game spreads its allocations across several heaps, so walking only
        one shows a fraction of what is live — and not a predictable fraction,
        since which heap is busiest changes with game state.
        """
        blocks = []
        for guest in self.list_bk_heaps():
            blocks.extend(self._walk_heap_bk(guest))
        blocks.sort(key=lambda b: b["addr"])
        return blocks

    def debug_bk_heap(self):
        """Human-readable report of what the BK heap walker can see."""
        out = []
        out.append("profile      = %s (%s)" % (self.profile.name, self.profile.id))
        out.append("connected    = %s" % self.connected)
        if self._phys_base is None:
            out.append("phys_base    = None  -> not connected, nothing else can work")
            return "\n".join(out)
        detected = self._phys_base
        self.confirm_bk_host_base()      # may correct _phys_base — do it first
        out.append("phys_base    = 0x%016X%s"
                   % (self._phys_base,
                      "" if detected == self._phys_base
                      else "   (corrected from 0x%016X, which reads as zeroes)"
                           % detected))

        # Dump the descriptor slot under every candidate base.  Exactly one
        # should show the FFEEFFEE magic; if none does, the descriptor really
        # has moved and discovery has to find it.
        for base in self._bk_host_base_candidates():
            raw = self._read_raw(base + XENIA_BK_DESC_GUEST, 0x20)
            out.append("base 0x%011X + 0x%08X -> %s"
                       % (base, XENIA_BK_DESC_GUEST,
                          raw.hex(" ") if raw else "UNREADABLE"))
        # Confirm the base BEFORE reading anything else, or the root and region
        # reads below go to the wrong address and report as empty.
        self.confirm_bk_host_base()
        out.append("host_base    = %s"
                   % ("0x%011X" % self._bk_host_base if self._bk_host_base else "unconfirmed"))

        root = self._read_raw(self._bk_host(XENIA_BK_ROOT_GUEST), 0x20)
        if not root or len(root) < 0x20:
            out.append("allocator root @ 0x%08X: UNREADABLE" % XENIA_BK_ROOT_GUEST)
        else:
            out.append("allocator root @ 0x%08X: size16=%04X flags=%08X slot10=%08X"
                       % (XENIA_BK_ROOT_GUEST,
                          struct.unpack_from(">H", root, 0)[0],
                          struct.unpack_from(">I", root, 4)[0],
                          struct.unpack_from(">I", root, 0x10)[0]))

        # Deep scan here: the whole point of this dump is to find heaps the
        # normal path might be skipping.
        descs = self.find_bk_heap_descriptors(deep=True)
        if not descs:
            out.append("no heap descriptors found")
        out.append("heaps found: %d" % len(descs))
        for d in descs:
            blocks = self._walk_heap_bk(d["guest"])
            used   = sum(b["chunk_size"] for b in blocks
                         if b["state"] == HEAP_STATE_USED)
            bad    = [b for b in blocks if b["xenia_errors"]]
            out.append("  desc @0x%08X base=0x%08X end=0x%08X (0x%-8X) "
                       "align=0x%-4X free=0x%-8X n=%d/%d "
                       "-> %d nodes, 0x%X used, %d flagged"
                       % (d["guest"], d["base"], d["end"], d["size"],
                          d["alignment"], d["free_bytes"],
                          d["count_a"], d["count_b"],
                          len(blocks), used, len(bad)))

            # Free regions belong to a per-heap list at descriptor+0x38.
            regions = self.read_bk_region_records(d["regions"])
            walked  = blocks[-1]["xenia_guest"] + (blocks[-1]["next"]
                                                   - blocks[-1]["addr"]) \
                      if blocks else d["base"]
            accounted = (walked - d["base"]) + sum(r[2] for r in regions)
            out.append("     regions @0x%08X: %d, chain ends 0x%08X, "
                       "accounted 0x%X of 0x%X%s"
                       % (d["regions"], len(regions), walked,
                          accounted, d["size"],
                          "" if accounted == d["size"]
                          else "   <-- 0x%X unaccounted" % (d["size"] - accounted)))
            for rec, base, size, rflags in regions:
                out.append("       @0x%08X base=0x%08X size=0x%-8X flags=0x%08X"
                           % (rec, base, size, rflags))
            for b in bad[:4]:
                out.append("       flagged 0x%08X: %s"
                           % (b["xenia_guest"], "; ".join(b["xenia_errors"])))

        chosen = self.resolve_bk_heap_descriptor()
        out.append("chosen (game/level heap) = %s"
                   % ("0x%08X" % chosen if chosen else "None"))
        return "\n".join(out)

    def _walk_heap_bk(self, desc_guest=None):
        """
        Walk the Xenia-canary BK heap.

        Starts at the heap descriptor (which is the first node in the chain) and
        steps forward by (size16 << 4).  Termination is bounded by the heap end
        recorded in the descriptor, so a corrupt node cannot walk off into
        unrelated memory.

        desc_guest defaults to whatever resolve_bk_heap_descriptor() finds; pass
        an explicit address to walk a specific heap (e.g.
        XENIA_BK_CTRL_DESC_GUEST for the allocator's own control heap).

        Three checks run on every node; all are cheap and all are recorded in
        the block's "errors" list rather than silently swallowed:
          * prev_size16 must equal the previous node's size16
          * the self-pointer must equal guest(P + 0x10)
          * data_size must equal (size16 << 4) - 0x60
        A bad self-pointer or an out-of-range span stops the walk, because past
        that point we no longer know where the next header begins.

        Pass desc_guest=XENIA_BK_CTRL_DESC_GUEST to walk the allocator's own
        control heap instead — same format, different bounds.
        """
        if self._phys_base is None:
            return []

        if desc_guest is None:
            desc_guest = self.resolve_bk_heap_descriptor()
            if desc_guest is None:
                return []

        desc = self.read_bk_heap_descriptor(desc_guest)
        if desc is None:
            return []

        hdr_sz    = XENIA_BK_HDR_READ
        heap_end  = desc["end"]

        blocks      = []
        guest       = desc_guest
        prev_size16 = 0          # the descriptor's own prev_size16 is 0
        stop_reason = "hit XENIA_BK_MAX_NODES (%d)" % XENIA_BK_MAX_NODES

        while len(blocks) < XENIA_BK_MAX_NODES:
            if guest >= heap_end:
                stop_reason = "reached heap end 0x%08X" % heap_end
                break

            host = self._bk_host(guest)
            data = self._read_raw(host, hdr_sz)
            if not data or len(data) < hdr_sz:
                stop_reason = ("cannot read header at 0x%08X (uncommitted?)"
                               % guest)
                break

            size16    = struct.unpack_from(">H", data, XENIA_BK_SIZE16_OFF)[0]
            prev16    = struct.unpack_from(">H", data, XENIA_BK_PREVSIZE_OFF)[0]
            flags     = struct.unpack_from(">I", data, XENIA_BK_FLAGS_OFF)[0]
            flink     = struct.unpack_from(">I", data, XENIA_BK_FLINK_OFF)[0]
            blink     = struct.unpack_from(">I", data, XENIA_BK_BLINK_OFF)[0]
            self_low  = struct.unpack_from(">I", data, XENIA_BK_SELF_OFF)[0]
            data_size = struct.unpack_from(">I", data, XENIA_BK_SIZE_OFF)[0]

            # A zero span would loop forever.
            if size16 == 0:
                stop_reason = "size16 == 0 at 0x%08X" % guest
                break

            node_span = size16 * XENIA_BK_GRANULARITY
            errors    = []
            fatal     = False

            if prev_size16 is not None and prev16 != prev_size16:
                errors.append("prev_size16=%04X expected %04X" % (prev16, prev_size16))

            if guest + node_span > heap_end:
                errors.append("span 0x%X overruns heap end 0x%08X" % (node_span, heap_end))
                fatal = True

            pad      = (flags & XENIA_BK_FLAG_PAD_MASK) >> XENIA_BK_FLAG_PAD_SHIFT
            is_exact = bool(flags & XENIA_BK_FLAG_EXACT)

            expected_self = (guest + XENIA_BK_SELF_OFF) & 0xFFFFFFFF
            is_desc    = (self_low == XENIA_BK_DESC_MAGIC)
            is_tracked = (not is_desc) and self_low == expected_self
            is_free    = (not is_desc) and not (flags & XENIA_BK_FLAG_ALLOCATED)

            # Two independent signals say "this is a real header": a valid
            # self-pointer, or a prev16 that links back to the node we just
            # walked.  Untracked nodes have payload at P+0x10 and so fail the
            # first test while passing the second — demanding both is what
            # turned them into phantoms.  Only when NEITHER holds is this
            # genuinely unrecognisable.
            chain_ok = (prev_size16 is None) or (prev16 == prev_size16)
            if not (is_desc or is_tracked or chain_ok):
                resync = self._bk_resync(guest + XENIA_BK_GRANULARITY, heap_end)
                stop   = resync if resync is not None else heap_end
                blocks.append(self._bk_unparsed_block(guest, stop, prev_size16))
                if resync is None:
                    break
                guest       = resync
                prev_size16 = None
                continue

            page_end = bool(flags & XENIA_BK_FLAG_PAGE_END)
            # Only a page-end node can be the last one, but most are not — so
            # confirm by looking for a real header immediately after it.
            next_why = (self._bk_probe_next(guest + node_span, heap_end, size16)
                        if page_end else None)
            is_last  = page_end and next_why is not None

            if is_desc:
                # Descriptors have no payload; their span is header-only.
                hdr_bytes  = node_span
                chunk_size = 0
                state      = HEAP_STATE_PERM
            elif is_last and (flags & XENIA_BK_FLAG_ALLOCATED) \
                    and node_span < XENIA_BK_OVERHEAD:
                # (see _bk_node_follows: is_last is verified, not assumed)
                # Degenerate boundary node: marked allocated but too small to
                # hold a tracked payload.  Nothing sensible to report, so call
                # it structural rather than invent a size or raise an error.
                hdr_bytes  = node_span
                chunk_size = 0
                data_size  = 0
                state      = HEAP_STATE_PERM
            elif is_free:
                # data_size is stale on a freed node — whatever it held before
                # the free is still sitting there, and reporting it as "used"
                # produces nonsense like 0x1441B0 inside a 0x20 node.  The whole
                # span is reclaimable, so that is the free size.
                hdr_bytes  = XENIA_BK_BASE_HDR
                chunk_size = node_span
                data_size  = 0
                state      = HEAP_STATE_EMPTY
            elif not is_tracked:
                # Untracked allocation: 0x10 header, payload immediately after.
                # There is no data_size field, but the flags carry the padding
                # count, so the requested size is still recoverable.
                hdr_bytes  = XENIA_BK_BASE_HDR
                chunk_size = node_span - XENIA_BK_BASE_HDR
                state      = HEAP_STATE_USED
                if is_exact:
                    # capacity == round_up(request), so the pad nibble recovers
                    # the request exactly.
                    data_size = max(0, chunk_size - pad)
                else:
                    # Oversized block and no data_size field to consult: the pad
                    # nibble only pins down the request modulo 0x10.  Report the
                    # capacity rather than guess.
                    data_size = chunk_size
            else:
                # Tracked allocation: 0x50 tracking block after the base header.
                hdr_bytes  = XENIA_BK_HDR_SIZE
                chunk_size = max(0, node_span - XENIA_BK_OVERHEAD)
                state      = HEAP_STATE_USED
                # data_size is the size originally requested.  What capacity is
                # allowed to be depends on the exact-fit bit: equal to the
                # rounded request, or merely not smaller than it.
                rounded = ((data_size + XENIA_BK_GRANULARITY - 1)
                           & ~(XENIA_BK_GRANULARITY - 1))
                if is_exact and rounded != chunk_size:
                    errors.append("exact-fit but data_size=%08X rounds to %08X, "
                                  "capacity %08X" % (data_size, rounded, chunk_size))
                elif rounded > chunk_size:
                    errors.append("data_size=%08X rounds to %08X, exceeds capacity %08X"
                                  % (data_size, rounded, chunk_size))
                # Independent cross-check: the flags nibble records the rounding
                # padding, which data_size also implies.  Two sources
                # disagreeing is a far stronger corruption signal than either
                # one alone, and this holds for exact and oversized alike.
                elif rounded - data_size != pad:
                    errors.append("flags pad=%d but round_up(data_size)-data_size=%d"
                                  % (pad, rounded - data_size))

            prev_host = (host - prev16 * XENIA_BK_GRANULARITY) if prev16 else host

            block = {
                "addr":       host,
                "end_addr":   host + node_span - 1,
                "prev":       prev_host,
                "next":       host + node_span,
                "state":      state,
                "chunk_size": chunk_size,
                "used_size":  data_size if not is_desc else 0,
                "unused":     max(0, chunk_size - data_size) if not is_desc else 0,

                # BK-Xenia-specific extras
                "xenia_self_low": self_low,
                "xenia_data_len": data_size if not is_desc else 0,
                "xenia_guest":    guest,
                "xenia_payload":  None if is_desc else host + hdr_bytes,
                "xenia_hdr_size": hdr_bytes,
                "xenia_tracked":  is_tracked,
                "xenia_pad":      pad,
                "xenia_exact":    is_exact,
                "xenia_last":     is_last,
                "xenia_page_end": page_end,
                "xenia_size16":   size16,
                "xenia_prev16":   prev16,
                "xenia_flags":    flags,
                "xenia_flink":    flink,
                "xenia_blink":    blink,
                "xenia_is_desc":  is_desc,
                # flink==blink and both inside the bin table means "not on any
                # free list"; the value identifies which size-class bin this
                # node belongs to.  Anything else is a live free-list link.
                "xenia_bin":      ((flink - XENIA_BK_BIN_TABLE) // 8
                                   if flink == blink and
                                   XENIA_BK_BIN_TABLE <= flink < XENIA_BK_BIN_TABLE_END
                                   else None),
                "xenia_errors":   errors,
            }
            blocks.append(block)

            if is_last:
                # Page-boundary node with nothing valid after it.  Everything
                # past here is managed as free regions rather than as nodes.
                stop_reason = "after page-end block at 0x%08X: %s" % (guest, next_why)
                break

            if fatal:
                # We no longer know where the next header is.  Rather than end
                # the walk (which hides everything downstream), hunt forward for
                # the next node that proves itself with a valid self-pointer.
                resync = self._bk_resync(guest + XENIA_BK_GRANULARITY, heap_end)
                if resync is None:
                    stop_reason = "resync found no further node after 0x%08X" % guest
                    break
                errors.append("resynced to 0x%08X" % resync)
                guest       = resync
                prev_size16 = None       # chain broken; can't verify the next prev16
                continue

            prev_size16 = size16
            guest      += node_span

        # Record why the walk ended on the final block.  "Why did it stop here?"
        # is otherwise unanswerable from a dump alone, and the answer is the
        # difference between a heap that really is small and a walker bug.
        if blocks:
            end_guest = blocks[-1]["xenia_guest"] + (blocks[-1]["next"]
                                                    - blocks[-1]["addr"])
            blocks[-1]["xenia_stop_reason"] = stop_reason
            blocks[-1]["xenia_errors"] = list(blocks[-1]["xenia_errors"]) + [
                "walk ended at 0x%08X: %s" % (end_guest, stop_reason)]

        return blocks

    def _bk_unparsed_block(self, guest, stop, prev_size16):
        """A placeholder block covering [guest, stop) that failed to parse."""
        host = self._bk_host(guest)
        span = max(XENIA_BK_GRANULARITY, stop - guest)
        return {
            "addr":       host,
            "end_addr":   host + span - 1,
            "prev":       host,
            "next":       host + span,
            "state":      HEAP_STATE_UNPARSED,
            "chunk_size": span,
            "used_size":  0,
            "unused":     0,
            "xenia_self_low": 0,
            "xenia_data_len": 0,
            "xenia_guest":    guest,
            "xenia_payload":  None,
            "xenia_hdr_size": 0,
            "xenia_tracked":  False,
            "xenia_pad":      0,
            "xenia_exact":    False,
            "xenia_last":     False,
            "xenia_page_end": False,
            "xenia_size16":   0,
            "xenia_prev16":   prev_size16 or 0,
            "xenia_flags":    0,
            "xenia_flink":    0,
            "xenia_blink":    0,
            "xenia_is_desc":  False,
            "xenia_bin":      None,
            "xenia_errors":   ["unparsed 0x%X bytes at 0x%08X" % (span, guest)],
        }

    def _bk_probe_next(self, guest, heap_end, prev_size16):
        """
        Probe for a node header at `guest`.  Returns None if one is there, or a
        string explaining what was found instead.

        Used to decide whether a page-boundary node is genuinely the last one.
        Accepts on any of the three independent signals a real header carries:
        a back-link matching the node just walked, its own self-pointer, or the
        descriptor magic.  One suffices — requiring all three would reject
        untracked nodes, which have payload where the self-pointer would be.

        The failure strings include the raw bytes on purpose.  "The walk stopped
        here" is only actionable if you can tell an uncommitted page from a
        header this function is wrongly rejecting.
        """
        if self._phys_base is None:
            return "not connected"
        if guest >= heap_end:
            return "0x%08X is at/past heap end 0x%08X" % (guest, heap_end)

        data = self._read_raw(self._bk_host(guest), XENIA_BK_HDR_READ)
        if not data or len(data) < XENIA_BK_HDR_READ:
            return "0x%08X unreadable — uncommitted, so the heap ends here" % guest

        size16   = struct.unpack_from(">H", data, XENIA_BK_SIZE16_OFF)[0]
        prev16   = struct.unpack_from(">H", data, XENIA_BK_PREVSIZE_OFF)[0]
        self_low = struct.unpack_from(">I", data, XENIA_BK_SELF_OFF)[0]
        head     = data[:16].hex(" ")

        if size16 == 0:
            return "0x%08X size16=0 [%s]" % (guest, head)
        if guest + size16 * XENIA_BK_GRANULARITY > heap_end:
            return ("0x%08X span 0x%X overruns heap end 0x%08X [%s]"
                    % (guest, size16 * XENIA_BK_GRANULARITY, heap_end, head))

        if (prev16 == prev_size16
                or self_low == ((guest + XENIA_BK_SELF_OFF) & 0xFFFFFFFF)
                or self_low == XENIA_BK_DESC_MAGIC):
            return None

        return ("0x%08X readable but no signal: prev16=%04X (want %04X), "
                "self=%08X [%s]"
                % (guest, prev16, prev_size16, self_low, head))

    def _bk_node_follows(self, guest, heap_end, prev_size16):
        return self._bk_probe_next(guest, heap_end, prev_size16) is None

    def _bk_resync(self, guest, heap_end, chunk=0x10000):
        """
        Find the next node header at or after `guest` by brute force.

        Used only after the chain breaks.  A node proves itself by carrying its
        own address in the self-pointer slot: u32 at P+0x10 == P+0x10.  That is
        a 1-in-2^32 coincidence, so a single hit is enough to trust.  Free nodes
        zero that slot and so cannot be found this way — resync lands on the
        next *allocated* node, which is the best available anchor.
        """
        if self._phys_base is None:
            return None

        guest = (guest + XENIA_BK_GRANULARITY - 1) & ~(XENIA_BK_GRANULARITY - 1)
        while guest < heap_end:
            size = min(chunk, heap_end - guest)
            data = self._read_raw(self._bk_host(guest), size)
            if not data:
                guest += chunk
                continue
            limit = len(data) - (XENIA_BK_SELF_OFF + 4)
            for off in range(0, limit + 1, XENIA_BK_GRANULARITY):
                want = (guest + off + XENIA_BK_SELF_OFF) & 0xFFFFFFFF
                got  = struct.unpack_from(">I", data, off + XENIA_BK_SELF_OFF)[0]
                if got == want:
                    return guest + off
            guest += len(data)
        return None

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
