"""
xenia_memory.py - Reads Xbox 360 guest memory from a running Xenia-canary process.

Xenia maps Xbox 360 physical memory at a fixed offset inside its own process, so
a guest address is read at (host_base + guest).  The host base is nominally
0x100000000, but _find_phys_base() has been observed returning other mappings
that read as all zeroes, so it is confirmed against a known heap descriptor
before anything relies on it.

Both Banjo-Kazooie and Banjo-Tooie (XBLA) run the SAME title-side allocator:
three heaps, each described by a 0x50-byte descriptor carrying 0xFFEEFFEE.

    guest 0x40000000  allocator root + bin table, and the shell heap
                      (D3D shaders, XBLA menu)             align 0x10
    guest 0x40220000  one dominant ~751KB buffer           align 0x10
    0x20-aligned      BK: 0x41A90000    BT: 0x40320000     align 0x20

Banjo-Tooie additionally has an unrelated SLAB heap at guest 0x70000: fixed
0x10000 stride, 0x40-byte header, 0xDEDEDEDE footer.  No amount of scanning for
0xFFEEFFEE would ever find it — a reminder that "all the heaps we can find" is
not "all the heaps".

WHICH HEAP HOLDS THE GAME depends on the title, and the two are not analogous:

  Kazooie has no slab heap, so its 0x20-aligned heap IS the game/level heap —
  ActorArray, level assets, everything that churns with the map.

  Tooie SPLITS its game data by size.  Large objects go in the SLAB heap
  (ActorArray, Player Object, BoneTransformLists, the 0x21DC0/0x4A380 asset
  buffers) — 0x10000 granularity makes that ruinous for anything small, so a
  0x1258 ActorArray still occupies a full 0x10000 slab.  Small objects go in
  the two non-shell FFEEFFEE heaps instead, as ordinary tracked nodes.

  Static pointers in the XEX confirm the split: scanning 0x1826A0000-0x1826B0000
  found 4 pointers to slab payloads (node + 0x40) and 9 to tracked heap nodes
  (P + 0x50) — six into 0x40220000 and three into 0x40320000, none into the
  shell heap at 0x40000000.

A WARNING ABOUT THE TEXT SURVEY: it reports strings found in payloads, which
only exist in allocations that contain strings.  A heap holding 300 game
objects and 5 menu strings surveys as "menu".  Tooie's 0x40220000 and
0x40320000 heaps were mislabelled that way ("Menu/UI", "Shader src") until
pointer scanning showed both are mixed.  Treat survey text as evidence of what
is present, never of what predominates.

Heap format details are documented on the constants below.
"""

import ctypes
import ctypes.wintypes
import struct
import sys
import time
from typing import Optional

from emu_common import (
    PROCESS_VM_READ, PROCESS_VM_WRITE, PROCESS_QUERY_INFORMATION,
    TH32CS_SNAPPROCESS, MEM_COMMIT, PAGE_READWRITE, PAGE_EXECUTE_READWRITE,
    HEAP_STATE_EMPTY, HEAP_STATE_USED, HEAP_STATE_PERM, HEAP_STATE_UNPARSED,
    GameProfile, PROCESSENTRY32, MEMORY_BASIC_INFORMATION,
)

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
#     P+0x50  payload,  capacity == span - 0x60
#             followed by a 0x10 trailer, so total overhead is 0x60
#     and round_up(data_size, 0x10) == capacity
#
#     The payload offset is 0x50, NOT 0x60.  Span arithmetic cannot tell the
#     two apart — 0x50 header + 0x10 trailer and a flat 0x60 header both leave
#     0x60 of overhead — so this was originally guessed wrong.  What settles it
#     is a pointer the game itself holds: the BK actor array pointer reads
#     0x41B78330 for the node at 0x41B782E0, which is exactly P+0x50.
#     Untracked nodes have no trailer (their payload fills span - 0x10 exactly,
#     confirmed by a 0x600 payload holding exactly 32 records of 0x30).
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
#   D+0x18 allocator root    D+0x1C largest free region   D+0x20 heap base
#   D+0x24 alignment         D+0x28 first node            D+0x2C heap end (excl)
#   D+0x30, D+0x34 counters  D+0x38 free-region list      D+0x40 tail node
#
# A heap is NOT one continuous chain: it is runs of nodes separated by free
# REGIONS, listed at D+0x38, which contain no node headers.  A walk that cannot
# step over them stops at the first one — on a fragmented heap that was a third
# of the total.
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
XENIA_BK_DESC_GUEST      = 0x41A90000  # BK game heap descriptor (observed)
XENIA_BT_DESC_GUEST      = 0x40320000  # BT game heap descriptor (observed)
XENIA_BK_CTRL_DESC_GUEST = 0x40000630  # allocator control heap (0x40000000..0x40100000)

# Banjo-Tooie uses this SAME allocator — three heaps, identical layout, with the
# game heap again distinguished by 0x20 alignment where the allocator's own
# heaps use 0x10.  BT additionally has a separate slab heap at guest 0x70000
# (see _walk_heap_bt), but the BK-style game heap is the interesting one.
XENIA_GAME_DESC_GUESSES  = (XENIA_BK_DESC_GUEST, XENIA_BT_DESC_GUEST)

XENIA_BK_BASE_HDR      = 0x10          # base header every node has
XENIA_BK_TRACKED_HDR   = 0x50          # tracked node: payload at P+0x50
XENIA_BK_TRACKED_TAIL  = 0x10          # ...followed by a 0x10 trailer
XENIA_BK_OVERHEAD      = 0x60          # = TRACKED_HDR + TRACKED_TAIL
XENIA_BK_HDR_SIZE      = 0x60          # legacy alias for the total overhead
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
# (control heap root block and descriptor).  The top byte is a per-heap tag.
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

# Kept for backwards compatibility: host address of the first *node* (not the
# descriptor, which sits 0x50 earlier at 0x141A90000).
XENIA_BK_HEAP_START    = XENIA_PHYS_BASE + XENIA_BK_DESC_GUEST + XENIA_BK_HDR_SIZE

# Xenia BT heap: fixed-stride slab allocator, unrelated to the one above.
# Each slab starts on a 0x10000-byte boundary.  The payload (data_length) can
# exceed 0x10000 bytes — in that case the node occupies multiple consecutive
# 0x10000 slots, and the NEXT node begins at the next 0x10000 boundary after
# the end of this node's content.
# Layout of one node (verified: 72/72 footers matched in a live walk):
#   [0x00] u32  self_addr_low  — this node's guest address
#   [0x04] u32  data_length    — payload bytes (may be > 0x10000)
#   [0x08..0x3F] 0xDE fill
#   [0x40..0x40+data_length-1] payload
#   after payload: 0x40 bytes of 0xDEDEDEDE sentinel footer
# Unused slabs sit BETWEEN live nodes, so a walk that stops at the first
# self-pointer mismatch reports only whatever prefix happened to be contiguous.
XENIA_BT_HEAP_START  = 0x100070000  # first node host address (observed)
XENIA_BT_HEAP_END    = 0x100640000  # last observed end — kept as a soft hint only
XENIA_BT_SLAB_STRIDE = 0x10000     # alignment of node starts
XENIA_BT_HDR_SIZE    = 0x40        # header bytes before payload
XENIA_BT_FOOTER_SIZE = 0x40        # 0x10 × 0xDEDEDEDE sentinel words after payload
XENIA_BT_FOOTER_MARK = 0xDEDEDEDE
# Maximum number of nodes to walk before giving up (safety cap).
XENIA_BT_MAX_NODES   = 4096
# Unused slabs sit between live nodes, so the walk steps over them.  This caps
# how long a run of them may be before the heap is considered finished.
XENIA_BT_MAX_GAP_SLABS = 32


# ── Xenia-canary Banjo-Tooie ──────────────────────────────────────────────────

XENIA_BT_PROFILE = GameProfile(
    name="Banjo-Tooie (Xenia)",
    id="xenia_bt",
    emulator="xenia",

    # Bounds are discovered from the heap descriptor at walk time; leaving them
    # at 0 makes the view derive its range from the blocks actually found,
    # instead of stretching the address bar across a 512 MB cap.
    heap_start=0,
    heap_size =0,

    watches_file="bt_xenia_watches.json",

    overlay_names={},   # TODO: map Xbox 360 level IDs
    hex_regions={},
    actor_array_pointers={
        "Actor Array": 0x1826A2BCC,
    },
)

# ── Xenia-canary Banjo-Kazooie ────────────────────────────────────────────────

XENIA_BK_PROFILE = GameProfile(
    name="Banjo-Kazooie (Xenia)",
    id="xenia_bk",
    emulator="xenia",

    heap_start=0,   # discovered from the heap descriptor at walk time
    heap_size =0,

    watches_file="bk_xenia_watches.json",

    overlay_names={},
    hex_regions={},
    # Static pointer holding the guest address of the live ActorArray.  It
    # resolves to the tracked node's payload (P+0x50) — the same value the heap
    # tagger matches — so actors_view can use it directly with no header offset.
    actor_array_pointers={
        "Actor Array": 0x18249F68C,
    },
)

ALL_XENIA_PROFILES = [XENIA_BT_PROFILE, XENIA_BK_PROFILE]


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
        # Which heap the UI is showing.  None = the game/level heap.
        self._heap_selection: Optional[str] = None
        # Rate limits for the expensive discovery paths (see _sweep_allowed).
        self._bk_last_sweep: float = 0.0
        self._heap_choices_cache: list = []
        self._heap_choices_ts: float = 0.0
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
            self._heap_choices_cache = []
            self._bk_last_sweep = 0.0

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
            return self._walk_heap_selected()
        if p.id != "xenia_bt":
            return []
        return self._walk_heap_selected()

    # ── Heap selection ────────────────────────────────────────────────────────

    SLAB_HEAP_KEY = "slab"
    ALL_HEAPS_KEY = "all"

    def list_heap_choices(self):
        """
        [(key, label)] of heaps the UI can show, game heap first.

        Both games run the same allocator with three heaps, and Tooie adds a
        separate slab heap.  Which one matters depends on what you are looking
        for, so this is a choice rather than a guess.
        """
        if self.profile.id not in ("xenia_bk", "xenia_bt"):
            return []
        if self._phys_base is None:
            return []

        # Called from the UI on every refresh; the heaps are created once at
        # startup, so re-reading their descriptors at frame rate is waste.
        now = time.monotonic()
        if self._heap_choices_cache and now - self._heap_choices_ts < 5.0:
            return self._heap_choices_cache

        out = []
        descs = []
        for guest in self.list_bk_heaps():
            d = self.read_bk_heap_descriptor(guest)
            if d:
                descs.append(d)
        # Alignment 0x20 marks the largest/primary heap; the allocator's own
        # heaps use 0x10.
        descs.sort(key=lambda d: (d["alignment"], d["size"]), reverse=True)

        # Which heap holds the actual game objects differs between the games.
        #
        # Kazooie has no slab heap, so its 0x20-aligned allocator heap IS the
        # game/level heap — ActorArray, level assets, the lot.
        #
        # Tooie keeps game objects in its slab heap instead, and its 0x20-aligned
        # heap holds the Xbox-side wrapper: one ~770KB engine buffer (a near
        # identical allocation exists in Kazooie's Buffer heap) plus hundreds of
        # 0x20-span handle nodes, each holding a pointer to the payload of the
        # allocation right after it, pointing back in turn.  That is C++ object
        # bookkeeping, not level data — so calling it "Game" was misleading.
        is_bt = (self.profile.id == "xenia_bt")

        for d in descs:
            if d["alignment"] == 0x20:
                role = "Objects+shaders" if is_bt else "Game"
            elif d["base"] == XENIA_BK_ROOT_GUEST:
                # The one heap no game pointer has been seen to reference:
                # compiled shader objects and a TrueType font (xarialuni.ttf,
                # with cmap/glyf/hhea tables) across ~975 tiny allocations.
                role = "Shaders/font"
            else:
                role = "Objects+UI" if is_bt else "Buffer"
            out.append(("0x%08X" % d["guest"],
                        "%s  0x%08X  (%d KB)"
                        % (role, d["guest"], d["size"] // 1024)))

        if is_bt:
            # First, and the default (see default_heap_key): this is Tooie's
            # real game heap — ActorArray, Player Object, BoneTransformLists and
            # the big asset buffers all live here.
            out.insert(0, (self.SLAB_HEAP_KEY, "Game / assets  slab 0x70000"))

        if len(out) > 1:
            out.append((self.ALL_HEAPS_KEY, "All heaps"))

        self._heap_choices_cache = out
        self._heap_choices_ts = now
        return out

    def set_heap_selection(self, key):
        self._heap_selection = key or None

    def get_heap_selection(self):
        return self._heap_selection

    def default_heap_key(self):
        """
        Which heap to show when the user hasn't chosen one: the one holding
        game objects.

        For Tooie that is the slab heap; for Kazooie, which has no slab heap,
        it is the 0x20-aligned allocator heap.  The UI reads this too, so the
        dropdown always names the heap actually being walked rather than
        assuming index 0.
        """
        if self.profile.id == "xenia_bt":
            return self.SLAB_HEAP_KEY
        guest = self.resolve_bk_heap_descriptor()
        return ("0x%08X" % guest) if guest is not None else None

    def _walk_heap_selected(self):
        """Walk whichever heap the UI has selected, else the profile default."""
        sel = self._heap_selection or self.default_heap_key()

        if sel == self.SLAB_HEAP_KEY:
            return self._walk_heap_bt()

        if sel == self.ALL_HEAPS_KEY:
            blocks = []
            if self.profile.id == "xenia_bt":
                blocks += self._walk_heap_bt()
            for guest in self.list_bk_heaps():
                blocks += self._walk_heap_bk(guest)
            blocks.sort(key=lambda b: b["addr"])
            return blocks

        if sel:
            try:
                return self._walk_heap_bk(int(sel, 16))
            except ValueError:
                pass

        return self._walk_heap_bk()      # default: game/level heap

    def _walk_heap_bt(self):
        """
        Walk the Banjo-Tooie slab heap.

        Node format (verified: 67/67 footers matched in a live walk):
            +0x00  u32  self      == this node's guest address
            +0x04  u32  data_len  payload bytes
            +0x08  0x38 bytes of 0xDE fill
            +0x40  payload
            +0x40+data_len  0x40 bytes of 0xDEDEDEDE footer
        Nodes start on 0x10000 boundaries and occupy
        ceil((0x40 + data_len + 0x40) / 0x10000) slabs.

        Unlike the previous version this does NOT stop at the first slab whose
        self-pointer doesn't match: unused slabs sit between live ones, and
        stopping at the first turns the heap into whatever prefix happened to be
        contiguous.  Empty slabs are emitted as FREE blocks instead.
        """
        if self._phys_base is None:
            return []

        # Pins down the real host base; _find_phys_base() has returned junk
        # (0x1C0000000 under Tooie) which every other view then reads through.
        self.confirm_bk_host_base()
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base or XENIA_PHYS_BASE))

        stride    = XENIA_BT_SLAB_STRIDE
        hdr_sz    = XENIA_BT_HDR_SIZE
        footer_sz = XENIA_BT_FOOTER_SIZE

        blocks = []
        guest  = self.find_bt_slab_start()
        misses = 0
        stop_reason = "hit XENIA_BT_MAX_NODES (%d)" % XENIA_BT_MAX_NODES

        while len(blocks) < XENIA_BT_MAX_NODES:
            host = base + guest
            data = self._read_raw(host, hdr_sz)
            if not data or len(data) < hdr_sz:
                stop_reason = "unreadable at guest 0x%08X" % guest
                break

            self_low = struct.unpack_from(">I", data, 0x00)[0]
            data_len = struct.unpack_from(">I", data, 0x04)[0]

            if self_low != guest or data_len > XENIA_MEM_SIZE:
                # Unused slab.  Record it and keep going; only give up after a
                # long unbroken run of them, which means the heap really ended.
                misses += 1
                if misses > XENIA_BT_MAX_GAP_SLABS:
                    stop_reason = ("%d empty slabs in a row ending 0x%08X"
                                   % (misses, guest))
                    break
                blocks.append(self._bt_empty_slab(base, guest, stride))
                guest += stride
                continue
            misses = 0

            errors  = []
            content = hdr_sz + data_len + footer_sz
            slots   = (content + stride - 1) // stride
            span    = slots * stride

            # The 0xDEDEDEDE marker is an independent check on data_len; if it
            # is not where data_len says it should be, the length is wrong.
            foot = self._read_raw(host + hdr_sz + data_len, 4)
            if not foot or len(foot) < 4:
                errors.append("footer unreadable at +0x%X" % (hdr_sz + data_len))
            elif struct.unpack_from(">I", foot, 0)[0] != XENIA_BT_FOOTER_MARK:
                errors.append("footer at +0x%X is %08X, expected %08X"
                              % (hdr_sz + data_len,
                                 struct.unpack_from(">I", foot, 0)[0],
                                 XENIA_BT_FOOTER_MARK))

            capacity = span - hdr_sz - footer_sz
            blocks.append({
                "addr":       host,
                "end_addr":   host + span - 1,
                "prev":       host,      # no explicit prev pointer in this format
                "next":       host + span,
                "state":      HEAP_STATE_EMPTY if data_len == 0 else HEAP_STATE_USED,
                "chunk_size": capacity,
                "used_size":  data_len,
                "unused":     capacity - data_len,
                "xenia_self_low": self_low,
                "xenia_data_len": data_len,
                "xenia_guest":    guest,
                "xenia_slots":    slots,
                "xenia_errors":   errors,
            })
            guest += span

        if blocks:
            blocks[-1]["xenia_stop_reason"] = ("walk ended at 0x%08X: %s"
                                               % (guest, stop_reason))
        for b in blocks:
            b["xenia_heap"] = "slab"
        return blocks

    def find_bt_slab_start(self, lo=0x00010000, hi=0x00100000):
        """
        First slab in the BT slab heap, found rather than assumed.

        XENIA_BT_HEAP_START was an observed constant (guest 0x70000), but the
        memory accounting shows committed pages BELOW it — so starting there can
        silently skip real slabs.  A slab proves itself twice: its first word is
        its own guest address, and 0xDEDEDEDE sits at 0x40 + data_len.  Two
        independent checks make a false positive implausible.
        """
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base or XENIA_PHYS_BASE))
        fallback = (XENIA_BT_HEAP_START - XENIA_PHYS_BASE) & 0xFFFFFFFF

        guest = lo & ~(XENIA_BT_SLAB_STRIDE - 1)
        while guest < hi:
            data = self._read_raw(base + guest, XENIA_BT_HDR_SIZE)
            if data and len(data) >= 8:
                self_low, data_len = struct.unpack_from(">II", data, 0)
                if self_low == guest and 0 < data_len <= XENIA_MEM_SIZE:
                    foot = self._read_raw(base + guest + XENIA_BT_HDR_SIZE
                                          + data_len, 4)
                    if foot and len(foot) == 4 and \
                            struct.unpack_from(">I", foot, 0)[0] == XENIA_BT_FOOTER_MARK:
                        return guest
            guest += XENIA_BT_SLAB_STRIDE
        return fallback

    def probe_bt_slab_granularity(self, step=0x100, limit=4000):
        """
        Look for slab headers the 0x10000 stride would step over.

        A slab proves itself twice — first word == its own guest address, and
        0xDEDEDEDE at 0x40 + data_len — so scanning at a finer granularity and
        counting the hits answers "is the stride too coarse" directly, rather
        than by inference.  If every hit is 0x10000-aligned the stride is right
        and a low node count is structural, not a walker bug.

        Returns (hits, aligned, misaligned_samples).
        """
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base or XENIA_PHYS_BASE))
        blocks = self._walk_heap_bt()
        if not blocks:
            return 0, 0, []
        lo = blocks[0]["xenia_guest"]
        hi = blocks[-1]["xenia_guest"] + (blocks[-1]["next"] - blocks[-1]["addr"])

        hits = aligned = 0
        samples = []
        guest = lo
        while guest < hi and hits < limit:
            data = self._read_raw(base + guest, min(0x100000, hi - guest))
            if not data:
                guest += 0x10000
                continue
            for off in range(0, len(data) - 8, step):
                if struct.unpack_from(">I", data, off)[0] != guest + off:
                    continue
                data_len = struct.unpack_from(">I", data, off + 4)[0]
                if not (0 < data_len <= XENIA_MEM_SIZE):
                    continue
                foot = self._read_raw(base + guest + off + XENIA_BT_HDR_SIZE
                                      + data_len, 4)
                if not foot or len(foot) < 4:
                    continue
                if struct.unpack_from(">I", foot, 0)[0] != XENIA_BT_FOOTER_MARK:
                    continue
                hits += 1
                if (guest + off) % XENIA_BT_SLAB_STRIDE == 0:
                    aligned += 1
                elif len(samples) < 8:
                    samples.append((guest + off, data_len))
            guest += len(data)
        return hits, aligned, samples

    def _bt_empty_slab(self, base, guest, stride):
        """An unused 0x10000 slab between live BT nodes."""
        host = base + guest
        return {
            "addr":       host,
            "end_addr":   host + stride - 1,
            "prev":       host,
            "next":       host + stride,
            "state":      HEAP_STATE_EMPTY,
            "chunk_size": stride,
            "used_size":  0,
            "unused":     stride,
            "xenia_self_low": 0,
            "xenia_data_len": 0,
            "xenia_guest":    guest,
            "xenia_slots":    1,
            "xenia_errors":   [],
        }

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

    # Minimum seconds between fallback magic sweeps.  The known-address and
    # root-table paths cost a handful of small reads and are not throttled;
    # only the multi-MB sweep is.
    BK_SWEEP_MIN_INTERVAL = 5.0

    def _sweep_allowed(self):
        now = time.monotonic()
        if now - self._bk_last_sweep < self.BK_SWEEP_MIN_INTERVAL:
            return False
        self._bk_last_sweep = now
        return True

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
        # does not reference — but it reads tens of MB, and "no game heap" is a
        # normal transient state during a load, so unthrottled it would stall
        # every refresh for as long as the heap is missing.  Diagnostics ask for
        # it explicitly and are never rate limited.
        if deep:
            for lo, hi in self.BK_SCAN_WIDE:
                for guest in self._scan_bk_descriptor_magic(lo, hi):
                    consider(guest)
        elif (not any(d["base"] != XENIA_BK_ROOT_GUEST for d in found)
              and self._sweep_allowed()):
            for lo, hi in self.BK_SCAN_NARROW:
                for guest in self._scan_bk_descriptor_magic(lo, hi):
                    consider(guest)

        found.sort(key=lambda d: d["size"], reverse=True)
        return found

    # Ranges swept when looking for heaps we were not told about.  The narrow
    # range is the allocator's own neighbourhood; the wide one also covers low
    # guest memory, where Tooie's slab heap lives (guest 0x70000) and where a
    # descriptor would otherwise be invisible to this scan.
    BK_SCAN_NARROW = ((XENIA_BK_ROOT_GUEST, XENIA_BK_ROOT_GUEST + 0x02000000),)
    BK_SCAN_WIDE   = ((0x00000000, 0x01000000),
                      (XENIA_BK_ROOT_GUEST, XENIA_BK_ROOT_GUEST + 0x10000000))

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
        return self.survey_blocks(self._walk_heap_bk(desc_guest),
                                  max_nodes=max_nodes, scan=scan)

    def survey_slab_heap(self, max_nodes=2000, scan=0x80):
        """survey_blocks() over Tooie's slab heap — its real game-object heap."""
        blocks = []
        for b in self._walk_heap_bt():
            # The slab walker has no xenia_payload; payload sits after the
            # 0x40-byte header.
            if b["state"] == HEAP_STATE_USED and b.get("xenia_data_len"):
                b = dict(b, xenia_payload=b["addr"] + XENIA_BT_HDR_SIZE)
                blocks.append(b)
        return self.survey_blocks(blocks, max_nodes=max_nodes, scan=scan)

    def survey_blocks(self, blocks, max_nodes=2000, scan=0x80):
        """Size / text / pointer survey over any list of walked blocks."""
        sizes, texts, ptrs = {}, {}, {}

        for b in blocks[:max_nodes]:
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
                       if d["guest"] in XENIA_GAME_DESC_GUESSES), None)
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
        out.append("swept for FFEEFFEE in: %s"
                   % ", ".join("0x%08X-0x%08X" % r for r in self.BK_SCAN_WIDE))
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
            # Free regions are walked through now (emitted as FREE blocks), so
            # the covered extent already includes them — adding them again
            # double counted and produced a negative shortfall.
            accounted = walked - d["base"]
            out.append("     regions @0x%08X: %d, chain ends 0x%08X, "
                       "covers 0x%X of 0x%X%s"
                       % (d["regions"], len(regions), walked,
                          accounted, d["size"],
                          "" if accounted >= d["size"]
                          else "   <-- 0x%X unaccounted" % (d["size"] - accounted)))
            for rec, base, size, rflags in regions:
                out.append("       @0x%08X base=0x%08X size=0x%-8X flags=0x%08X"
                           % (rec, base, size, rflags))
            for b in bad[:4]:
                out.append("       flagged 0x%08X: %s"
                           % (b["xenia_guest"], "; ".join(b["xenia_errors"])))
            if blocks and blocks[-1].get("xenia_stop_reason"):
                out.append("       %s" % blocks[-1]["xenia_stop_reason"])

        chosen = self.resolve_bk_heap_descriptor()
        out.append("chosen (game/level heap) = %s"
                   % ("0x%08X" % chosen if chosen else "None"))
        out.append("")
        out.append(self.debug_region_chain())
        out.append("")
        out.append(self.debug_memory_accounting())
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

        # A heap is NOT one continuous chain.  It is runs of nodes separated by
        # free REGIONS, which are tracked in a separate list and contain no node
        # headers at all.  Without stepping over them the walk stops at the
        # first one — on a fragmented heap that can be a third of the total.
        region_map = {}
        for _, rbase, rsize, _ in self.read_bk_region_records(desc.get("regions") or 0):
            if rsize:
                region_map[rbase] = rsize

        blocks      = []
        guest       = desc_guest
        # The first node walked is not always the first node in the heap: the
        # allocator's own heap puts its 0x630 root block ahead of the
        # descriptor, so the descriptor's prev16 legitimately points back at it.
        # Seeding 0 flagged that as corruption; None just skips the check once.
        prev_size16 = None
        stop_reason = "hit XENIA_BK_MAX_NODES (%d)" % XENIA_BK_MAX_NODES

        while len(blocks) < XENIA_BK_MAX_NODES:
            if guest >= heap_end:
                stop_reason = "reached heap end 0x%08X" % heap_end
                break

            # A free region here means the next node run starts after it.
            if guest in region_map:
                rsize = region_map[guest]
                blocks.append(self._bk_region_block(guest, rsize))
                guest      += rsize
                prev_size16 = None      # chain restarts; nothing to verify against
                continue

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
                # Tracked allocation: payload at P+0x50, with a 0x10 trailer
                # after it.  Overhead is 0x60 either way, so span arithmetic
                # cannot distinguish 0x50+trailer from a flat 0x60 header — the
                # game's own pointer can, and does: the BK actor array pointer
                # reads 0x41B78330 for the node at 0x41B782E0, i.e. P+0x50.
                hdr_bytes  = XENIA_BK_TRACKED_HDR
                chunk_size = max(0, node_span - XENIA_BK_OVERHEAD)
                state      = HEAP_STATE_USED
                # data_size is the size originally requested.  What capacity is
                # allowed to be depends on the exact-fit bit: equal to the
                # rounded request, or merely not smaller than it.
                rounded = ((data_size + XENIA_BK_GRANULARITY - 1)
                           & ~(XENIA_BK_GRANULARITY - 1))
                if chunk_size == 0:
                    # Zero-byte allocation: header only, no payload.  data_size
                    # still holds whatever the previous occupant left, so there
                    # is nothing meaningful to validate it against.
                    data_size = 0
                elif is_exact and rounded != chunk_size:
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
                "xenia_region":   False,
                "xenia_bin":      ((flink - XENIA_BK_BIN_TABLE) // 8
                                   if flink == blink and
                                   XENIA_BK_BIN_TABLE <= flink < XENIA_BK_BIN_TABLE_END
                                   else None),
                "xenia_errors":   errors,
            }
            blocks.append(block)

            if is_last:
                # Nothing parseable follows.  If that is because a free region
                # starts here, hop over it and keep going — the chain resumes on
                # the far side.  Otherwise this really is the end.
                nxt = guest + node_span
                if nxt in region_map:
                    rsize = region_map[nxt]
                    blocks.append(self._bk_region_block(nxt, rsize))
                    guest       = nxt + rsize
                    prev_size16 = None
                    continue
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
        # Kept OUT of xenia_errors on purpose: every walk ends somewhere, so
        # counting the reason as an error means a perfectly healthy heap always
        # reports at least one flagged node and the count stops meaning anything.
        if blocks:
            end_guest = blocks[-1]["xenia_guest"] + (blocks[-1]["next"]
                                                    - blocks[-1]["addr"])
            blocks[-1]["xenia_stop_reason"] = ("walk ended at 0x%08X: %s"
                                               % (end_guest, stop_reason))

        # Label every block with the heap it came from — Tooie shows two heaps
        # at once and they are otherwise indistinguishable in the table.
        label = "0x%08X" % desc_guest
        for b in blocks:
            b.setdefault("xenia_heap", label)

        return blocks

    def _bk_region_block(self, guest, size):
        """
        A free REGION: heap space held outside the node chain entirely.

        Emitted so the address space stays contiguous and the space is counted
        as free rather than vanishing between two node runs.
        """
        host = self._bk_host(guest)
        return {
            "addr":       host,
            "end_addr":   host + size - 1,
            "prev":       host,
            "next":       host + size,
            "state":      HEAP_STATE_EMPTY,
            "chunk_size": size,
            "used_size":  0,
            "unused":     size,
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
            "xenia_prev16":   0,
            "xenia_flags":    0,
            "xenia_flink":    0,
            "xenia_blink":    0,
            "xenia_is_desc":  False,
            "xenia_bin":      None,
            "xenia_region":   True,
            "xenia_errors":   [],
        }

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
            "xenia_region":   False,
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

    def survey_bt_heap(self, start=None, max_nodes=None):
        """
        Walk the BT slab heap recording every check rather than bailing.

        Deliberately does not stop at the first bad node: the point is to learn
        whether the slab model is right, and stopping early is what hides that.
        Returns (nodes, notes) where nodes is a list of dicts.
        """
        self.confirm_bk_host_base()          # also corrects _phys_base
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base or XENIA_PHYS_BASE))

        guest = (XENIA_BT_HEAP_START - XENIA_PHYS_BASE) & 0xFFFFFFFF \
            if start is None else start
        limit = max_nodes or XENIA_BT_MAX_NODES

        nodes, notes = [], []
        misses = 0
        while len(nodes) < limit:
            host = base + guest
            data = self._read_raw(host, XENIA_BT_HDR_SIZE)
            if not data or len(data) < XENIA_BT_HDR_SIZE:
                notes.append("unreadable at guest 0x%08X" % guest)
                break

            self_low = struct.unpack_from(">I", data, 0x00)[0]
            data_len = struct.unpack_from(">I", data, 0x04)[0]
            ok_self  = (self_low == guest)

            # The footer marker is the independent check the walker never used.
            footer_ok = None
            if ok_self and 0 < data_len <= XENIA_MEM_SIZE:
                foot = self._read_raw(host + XENIA_BT_HDR_SIZE + data_len, 4)
                if foot and len(foot) == 4:
                    footer_ok = (struct.unpack_from(">I", foot, 0)[0]
                                 == XENIA_BT_FOOTER_MARK)

            if not ok_self:
                misses += 1
                # Keep stepping by one slab so we can see whether the chain
                # resumes — if it does, "stop at first mismatch" was wrong.
                if misses > 32:
                    notes.append("gave up after 32 consecutive misses at 0x%08X"
                                 % guest)
                    break
                guest += XENIA_BT_SLAB_STRIDE
                continue
            misses = 0

            content = XENIA_BT_HDR_SIZE + data_len + XENIA_BT_FOOTER_SIZE
            slots   = (content + XENIA_BT_SLAB_STRIDE - 1) // XENIA_BT_SLAB_STRIDE
            nodes.append({
                "guest": guest, "self": self_low, "len": data_len,
                "slots": slots, "footer_ok": footer_ok,
                "head": data[:16].hex(" "),
            })
            guest += slots * XENIA_BT_SLAB_STRIDE

        return nodes, notes

    def survey_guest_regions(self, span=0x50000000):
        """
        Every committed page in the guest mapping, as (guest, size, protect).

        Sweeping for a magic value can only find heaps that carry that magic.
        This asks the OS what is actually committed, so memory belonging to an
        allocator we know nothing about still shows up.
        """
        if not self._k32 or not self.handle:
            return []
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base or XENIA_PHYS_BASE))

        out = []
        mbi = MEMORY_BASIC_INFORMATION()
        addr, end = base, base + span
        while addr < end:
            ret = self._k32.VirtualQueryEx(
                self.handle, ctypes.c_void_p(addr),
                ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not ret:
                break
            rbase = mbi.BaseAddress or addr
            rsize = mbi.RegionSize or 0x1000
            if mbi.State == MEM_COMMIT:
                out.append((rbase - base, rsize, mbi.Protect))
            addr = rbase + rsize
        return out

    def account_guest_memory(self, span=0x50000000):
        """
        Compare committed guest memory against the heaps we can explain.

        Returns (total_committed, accounted, leftovers) where leftovers is a
        list of (guest, size) not covered by any known heap, largest first.
        A large leftover is either a heap we have not found or memory that is
        not heap at all — the size is what tells you whether to care.
        """
        known = []
        for guest in self.list_bk_heaps():
            d = self.read_bk_heap_descriptor(guest)
            if d:
                known.append((d["base"], d["end"]))
        if self.profile.id == "xenia_bt":
            slab = self._walk_heap_bt()
            if slab:
                lo = (XENIA_BT_HEAP_START - XENIA_PHYS_BASE) & 0xFFFFFFFF
                hi = slab[-1]["xenia_guest"] + (slab[-1]["next"]
                                                - slab[-1]["addr"])
                known.append((lo, hi))

        total, accounted, leftovers = 0, 0, []
        for guest, size, _prot in self.survey_guest_regions(span):
            total += size
            lo, hi = guest, guest + size
            # Clip this region against every known heap range.
            free_parts = [(lo, hi)]
            for klo, khi in known:
                nxt = []
                for a, b in free_parts:
                    if khi <= a or klo >= b:
                        nxt.append((a, b))
                        continue
                    if a < klo:
                        nxt.append((a, klo))
                    if khi < b:
                        nxt.append((khi, b))
                free_parts = nxt
            uncovered = sum(b - a for a, b in free_parts)
            accounted += size - uncovered
            leftovers.extend((a, b - a) for a, b in free_parts if b > a)

        leftovers.sort(key=lambda r: r[1], reverse=True)
        return total, accounted, leftovers

    def debug_bt_heap(self):
        """Diagnostic for the Banjo-Tooie slab heap."""
        out = []
        if self._phys_base is None:
            return "not connected"

        detected = self._phys_base
        self.confirm_bk_host_base()
        out.append("phys_base = 0x%016X%s"
                   % (self._phys_base,
                      "" if detected == self._phys_base
                      else "  (corrected from 0x%016X)" % detected))

        # Does BT use the same allocator as BK?  If so the whole BK decode
        # applies and the slab model is the wrong abstraction entirely.
        descs = self.find_bk_heap_descriptors(deep=True)
        out.append("swept for FFEEFFEE in: %s"
                   % ", ".join("0x%08X-0x%08X" % r for r in self.BK_SCAN_WIDE))
        out.append("BK-style FFEEFFEE heap descriptors in this process: %d"
                   % len(descs))
        for d in descs:
            out.append("   desc @0x%08X base=0x%08X end=0x%08X (0x%X) align=0x%X"
                       % (d["guest"], d["base"], d["end"], d["size"],
                          d["alignment"]))

        slab_start = self.find_bt_slab_start()
        nodes, notes = self.survey_bt_heap(start=slab_start)
        out.append("")
        out.append("slab walk from guest 0x%08X (hardcoded constant was 0x%08X): "
                   "%d nodes"
                   % (slab_start,
                      (XENIA_BT_HEAP_START - XENIA_PHYS_BASE) & 0xFFFFFFFF,
                      len(nodes)))
        for n in notes:
            out.append("   note: %s" % n)

        good = sum(1 for n in nodes if n["footer_ok"] is True)
        bad  = sum(1 for n in nodes if n["footer_ok"] is False)
        out.append("   footer 0xDEDEDEDE: %d ok, %d wrong, %d unchecked"
                   % (good, bad, len(nodes) - good - bad))

        multi = sum(1 for n in nodes if n["slots"] > 1)
        small = sum(1 for n in nodes if n["len"] < 0x1000)
        out.append("   %d nodes span >1 slab, %d nodes are <0x1000 bytes"
                   % (multi, small))

        # Is the 0x10000 stride stepping over headers?  Scan finer and see.
        hits, aligned, samples = self.probe_bt_slab_granularity()
        out.append("   granularity probe (step 0x100): %d valid headers, "
                   "%d of them 0x10000-aligned" % (hits, aligned))
        if hits == aligned:
            out.append("   -> stride is correct; nothing is being stepped over, "
                       "so the node count is what the game actually allocates")
        else:
            out.append("   -> STRIDE IS TOO COARSE — headers exist off the "
                       "0x10000 grid:")
            for guest, data_len in samples:
                out.append("        0x%08X len=0x%X" % (guest, data_len))

        out.append("   first nodes:")
        for n in nodes[:12]:
            out.append("     0x%08X len=0x%-8X slots=%-3d footer=%-5s [%s]"
                       % (n["guest"], n["len"], n["slots"],
                          n["footer_ok"], n["head"]))

        out.append("")
        out.append(self.debug_region_chain())
        out.append("")
        out.append(self.debug_memory_accounting())
        return "\n".join(out)

    def locate_pointer(self, addr):
        """
        Explain where an address lands: which heap, which block, and whether it
        is exactly a block's payload start.

        Tagging matches a pointer against block payload starts, so when a tag
        does not appear the question is always one of: is the pointer garbage,
        is it in a heap we are not walking, or is it mid-block rather than at a
        payload start?  This answers all three.

        `addr` may be a host address or a guest address.
        """
        self.confirm_bk_host_base()
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base or XENIA_PHYS_BASE))
        guest = (addr - base) if addr >= base else addr
        guest &= 0xFFFFFFFF

        lines = ["pointer 0x%08X (host 0x%X)" % (guest, base + guest)]
        if guest == 0:
            lines.append("  NULL — the pointer read returned 0")
            return "\n".join(lines)

        for desc_guest in self.list_bk_heaps():
            d = self.read_bk_heap_descriptor(desc_guest)
            if not d or not (d["base"] <= guest < d["end"]):
                continue
            lines.append("  inside heap 0x%08X (base 0x%08X end 0x%08X, align 0x%X)"
                         % (desc_guest, d["base"], d["end"], d["alignment"]))
            for b in self._walk_heap_bk(desc_guest):
                lo = b["xenia_guest"]
                hi = lo + (b["next"] - b["addr"])
                if not (lo <= guest < hi):
                    continue
                hdr = b.get("xenia_hdr_size", 0)
                payload = lo + hdr
                lines.append("  block 0x%08X span 0x%X state=%d hdr=0x%X"
                             % (lo, hi - lo, b["state"], hdr))
                if guest == payload:
                    lines.append("  == payload start, so tagging SHOULD match")
                else:
                    lines.append("  payload starts 0x%08X — pointer is +0x%X "
                                 "into it, so an exact-match tag will NOT fire"
                                 % (payload, guest - payload))
                return "\n".join(lines)
            lines.append("  no block contains it (inside the heap but unwalked)")
            return "\n".join(lines)

        # The slab heap and the region chain hold most of Tooie's memory, so a
        # pointer landing outside the FFEEFFEE heaps is the common case, not an
        # error — searching only those was why this used to give up here.
        for b in self._walk_heap_bt():
            lo = b["xenia_guest"]
            hi = lo + (b["next"] - b["addr"])
            if lo <= guest < hi:
                payload = lo + XENIA_BT_HDR_SIZE
                lines.append("  inside the SLAB heap")
                lines.append("  slab 0x%08X span 0x%X data_len 0x%X"
                             % (lo, hi - lo, b.get("xenia_data_len", 0)))
                if guest == payload:
                    lines.append("  == payload start")
                else:
                    lines.append("  payload starts 0x%08X — pointer is +0x%X "
                                 "into it, so this is an object POOLED inside "
                                 "the slab rather than its own allocation"
                                 % (payload, guest - payload))
                return "\n".join(lines)

        for rbase, rsize, _nxt, _prev in self.walk_region_chain():
            if rbase <= guest < rbase + rsize:
                lines.append("  inside REGION 0x%08X (0x%X bytes), +0x%X in"
                             % (rbase, rsize, guest - rbase))
                lines.append("  regions have no node headers, so there is no "
                             "block structure to report here")
                return "\n".join(lines)

        lines.append("  not inside any heap, slab or region we know of — "
                     "check the memory accounting for unexplained runs")
        return "\n".join(lines)

    # Region header, for the chain rooted in the allocator root block.  These
    # are NOT FFEEFFEE heaps — they carry no descriptor and no magic, which is
    # why sweeping for the magic never found them.
    REGION_NEXT_OFF = 0x00
    REGION_PREV_OFF = 0x04
    REGION_SIZE_OFF = 0x18

    def walk_region_chain(self, start=None, limit=256):
        """
        Enumerate the allocator's large memory regions.

        Each region header holds {next, prev, ..., size}, and next == base+size
        for adjacent regions, so the chain tiles the address space.  Following
        it explains memory that neither the heap walks nor the magic sweep can
        see: in Tooie the chain covers tens of MB against ~9MB of heaps.

        Returns [(guest, size, next, prev)] in chain order.
        """
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base or XENIA_PHYS_BASE))

        def hdr(guest):
            data = self._read_raw(base + (guest & 0xFFFFFFFF), 0x20)
            if not data or len(data) < 0x20:
                return None
            return (struct.unpack_from(">I", data, self.REGION_NEXT_OFF)[0],
                    struct.unpack_from(">I", data, self.REGION_PREV_OFF)[0],
                    struct.unpack_from(">I", data, self.REGION_SIZE_OFF)[0])

        # Walk backwards to the head first, so the caller gets the whole chain
        # regardless of which member we happened to start from.
        cur = start if start is not None else 0x40100000
        seen = set()
        while True:
            h = hdr(cur)
            if not h or cur in seen:
                break
            seen.add(cur)
            prev = h[1]
            # The head's prev points into the root block, not at a region.
            if not prev or prev < XENIA_BK_ROOT_GUEST + 0x1000:
                break
            cur = prev

        out, seen = [], set()
        while cur and cur not in seen and len(out) < limit:
            seen.add(cur)
            h = hdr(cur)
            if not h:
                break
            nxt, prev, size = h
            if not (0 < size <= XENIA_MEM_SIZE):
                break
            out.append((cur, size, nxt, prev))
            cur = nxt
        return out

    def debug_region_chain(self):
        """
        The regions and the FFEEFFEE heaps tile the address space together, so
        a gap between one region and the next is only suspicious once the heaps
        sitting in it are accounted for.  The last region's `next` points back
        at the list head in the root block, which is not a gap at all.
        """
        rows = self.walk_region_chain()
        heaps = []
        for guest in self.list_bk_heaps():
            d = self.read_bk_heap_descriptor(guest)
            if d:
                heaps.append((d["base"], d["end"]))

        lines = ["region chain: %d regions" % len(rows)]
        total = 0
        for guest, size, nxt, prev in rows:
            total += size
            end = (guest + size) & 0xFFFFFFFF
            if nxt == end:
                note = ""
            elif nxt < XENIA_BK_ROOT_GUEST + 0x1000:
                note = "   (circular: back to the list head)"
            else:
                filled = sum(hi - lo for lo, hi in heaps if end <= lo and hi <= nxt)
                note = ("   (gap 0x%X filled by heaps)" % (nxt - end)
                        if filled == nxt - end
                        else "   <-- unexplained gap 0x%X" % (nxt - end))
            lines.append("  0x%08X  size 0x%-9X (%6d KB)  next 0x%08X%s"
                         % (guest, size, size // 1024, nxt, note))
        lines.append("  total 0x%X (%d MB)" % (total, total // (1 << 20)))

        # What the regions hold: sample past the header rather than at +0.
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base or XENIA_PHYS_BASE))
        for guest, size, _nxt, _prev in rows:
            data = self._read_raw(base + guest + 0x40, 0x40)
            if not data:
                continue
            lines.append("  content of 0x%08X at +0x40:" % guest)
            for row in range(0, len(data), 16):
                chunk = data[row:row + 16]
                text = "".join(chr(c) if 0x20 <= c <= 0x7E else "." for c in chunk)
                lines.append("    +%03X  %-47s  %s"
                             % (0x40 + row, chunk.hex(" "), text))
        return "\n".join(lines)

    def debug_memory_accounting(self):
        """Committed guest memory vs. what the known heaps explain."""
        total, accounted, leftovers = self.account_guest_memory()
        lines = ["committed guest memory: 0x%X (%d MB)" % (total, total // (1 << 20)),
                 "  explained by known heaps: 0x%X (%d%%)"
                 % (accounted, (accounted * 100 // total) if total else 0),
                 "  unexplained: 0x%X — largest runs:" % (total - accounted)]
        for guest, size in leftovers[:12]:
            lines.append("     0x%08X  0x%-9X (%d KB)" % (guest, size, size // 1024))
        lines.append("  (large unexplained runs are either a heap we have not "
                     "found, or not heap memory at all)")

        # A size alone doesn't say what a region is; its first bytes usually do.
        base = (self._bk_host_base if self._bk_host_base is not None
                else (self._phys_base or XENIA_PHYS_BASE))
        for guest, size in leftovers[:3]:
            if size < 0x10000:
                continue
            lines.append("  head of 0x%08X (0x%X bytes):" % (guest, size))
            data = self._read_raw(base + guest, 0x60)
            if not data:
                lines.append("    unreadable")
                continue
            for row in range(0, len(data), 16):
                chunk = data[row:row + 16]
                text = "".join(chr(c) if 0x20 <= c <= 0x7E else "." for c in chunk)
                lines.append("    +%03X  %-47s  %s" % (row, chunk.hex(" "), text))
        return "\n".join(lines)

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
