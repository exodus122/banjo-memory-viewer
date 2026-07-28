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

Module layout
-------------
  emu_common.py    Win32 plumbing, heap-state enum, GameProfile
  xenia_memory.py  XeniaMemoryReader and the Xbox 360 heap formats
  bizhawk_memory.py (this file)  BizHawkMemoryReader, plus re-exports of the
                   above so existing `from bizhawk_memory import ...` callers
                   keep working unchanged.
"""

import array
import ctypes
import ctypes.wintypes
import struct
import sys
import random
import time
from typing import List, Optional

from emu_common import (
    PROCESS_VM_READ, PROCESS_VM_WRITE, PROCESS_QUERY_INFORMATION,
    TH32CS_SNAPPROCESS, MEM_COMMIT, PAGE_READWRITE, PAGE_EXECUTE_READWRITE,
    HEAP_HEADER_SIZE, HEAP_STATE_EMPTY, HEAP_STATE_USED, HEAP_STATE_PERM,
    HEAP_STATE_UNPARSED, GameProfile, PROCESSENTRY32,
    MEMORY_BASIC_INFORMATION,
)

# Re-exported so callers importing Xenia names from this module still resolve.
from xenia_memory import (            # noqa: F401  (re-export)
    XeniaMemoryReader,
    XENIA_BT_PROFILE, XENIA_BK_PROFILE, ALL_XENIA_PROFILES,
    XENIA_PHYS_BASE, XENIA_MEM_SIZE,
    XENIA_BT_HEAP_START, XENIA_BT_HEAP_END, XENIA_BT_SLAB_STRIDE,
    XENIA_BT_HDR_SIZE, XENIA_BT_FOOTER_SIZE, XENIA_BT_FOOTER_MARK,
    XENIA_BT_MAX_NODES,
    XENIA_BK_HEAP_START, XENIA_BK_HDR_SIZE, XENIA_BK_MAX_NODES,
    XENIA_BK_DESC_GUEST, XENIA_BT_DESC_GUEST, XENIA_BK_ROOT_GUEST,
)

# N64 constants (shared)
BASE_ADDR    = 0x80000000
RDRAM_SIZE   = 0x00800000   # 8 MB

# Xenia constants and heap formats now live in xenia_memory.py; the names this
# module's callers use are re-exported at the top.





# GameProfile now lives in emu_common (imported above) so that xenia_memory can
# use it without importing this module.


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


# The Xenia profiles live in xenia_memory and are imported at the top of this
# module, so the ordering here matches what the UI expects.
ALL_PROFILES = ALL_N64_PROFILES + ALL_XENIA_PROFILES


# ── Convenience aliases so trainer_app.py imports still work ──────────────────
HEAP_START                = BK_PROFILE.heap_start
HEAP_SIZE                 = BK_PROFILE.heap_size
OVERLAY_MGR_LOADED_ID_ADDR = BK_PROFILE.overlay_mgr_addr
FRAMEBUFFER_WIDTH_ADDR     = BK_PROFILE.framebuffer_width_addr


# PROCESSENTRY32 and MEMORY_BASIC_INFORMATION now live in emu_common
# (imported above), so both readers share one definition.


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
            # buf.raw already copies; slicing it copies a second time, which at
            # multi-megabyte bulk sizes is a real cost for no benefit.
            if n.value == size:
                return buf.raw
            return buf.raw[:n.value]
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

    def _read_n64_bulk_raw(self, n64_addr, size):
        """Bulk read WITHOUT undoing BizHawk's word swizzle.

        For the heap walk, which touches 16 bytes per block out of a
        multi-megabyte buffer, unswizzling the whole thing is pure waste — the
        transform costs a full extra copy and byteswap of every megabyte read.
        Callers must read through the swizzle themselves (see
        _parse_heap_header's `swizzled` argument).  n64_addr must be 4-aligned.
        """
        if size <= 0:
            return b""
        proc_base = self._n64_to_proc(n64_addr)
        if proc_base is None:
            return None
        return self._read_raw(proc_base, (size + 3) & ~3)

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
            # Raw (still swizzled): _parse_heap_header reads through the swizzle,
            # which avoids byteswapping megabytes to use 16 bytes per block.
            bulk = self._read_n64_bulk_raw(p.heap_start, p.heap_size + 0x1000)

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
                    block = self._parse_heap_header(bulk, off, addr,
                                                    swizzled=True)
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

    def _parse_heap_header(self, data, off, addr, swizzled=False):
        """Parse one heap block header out of an already-fetched buffer.

        `off` is the byte offset within `data` corresponding to `addr`.
        If `data` doesn't extend far enough past the header to cover the
        free-list fields (prev_free/next_free, only present for free
        blocks), those are topped up with one small live read - same as
        the original single-read implementation did unconditionally.

        `swizzled=True` means `data` is still in BizHawk's raw form, with the
        bytes of each 4-byte word reversed.  Undoing that for the whole buffer
        costs a copy and a byteswap of every megabyte read, to use 16 bytes per
        block — so instead we read through the swizzle here:

            big-endian u32 at an aligned offset in the UNSWIZZLED buffer
            == little-endian u32 at the same offset in the RAW buffer

        because reversing a word's bytes and then reading it big-endian is the
        same as reading the original little-endian.  Individual bytes need the
        ^3 flip.  Requires a 4-aligned buffer base and offset, hence the guard.
        """
        if off < 0 or off + 0x10 > len(data):
            return None
        if swizzled and (off & 3):
            return None      # caller must pass 4-aligned offsets

        if swizzled:
            prev_ptr = struct.unpack_from("<I", data, off + 0x0)[0]
            next_ptr = struct.unpack_from("<I", data, off + 0x4)[0]
            b0 = data[(off + 0xC) ^ 3]
            b1 = data[(off + 0xD) ^ 3]
            b2 = data[(off + 0xE) ^ 3]
            b3 = data[(off + 0xF) ^ 3]
        else:
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
                fmt = "<I" if swizzled else ">I"
                block["prev_free"] = struct.unpack_from(fmt, data, off + 0x10)[0]
                block["next_free"] = struct.unpack_from(fmt, data, off + 0x14)[0]
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
