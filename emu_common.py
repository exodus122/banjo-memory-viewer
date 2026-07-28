"""
emu_common.py - Pieces shared by the BizHawk and Xenia memory readers.

The two readers have no logic in common: one scans BizHawk's process for a flat
8MB N64 RDRAM buffer, the other decodes the Xbox 360 title's own heap allocator.
What they do share is Win32 plumbing, the heap-state enum the UI renders, and
the GameProfile record.  Keeping those here lets each reader live in its own
module without either importing the other.

Dependency direction is one way:

    bizhawk_memory  ->  xenia_memory  ->  emu_common
"""

import ctypes
import ctypes.wintypes
from dataclasses import dataclass, field
from typing import List

# ── Windows constants ─────────────────────────────────────────────────────────
PROCESS_VM_READ           = 0x0010
PROCESS_VM_WRITE          = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS        = 0x00000002
MEM_COMMIT                = 0x1000
PAGE_READWRITE            = 0x04
PAGE_EXECUTE_READWRITE    = 0x40


# ── Heap block states ─────────────────────────────────────────────────────────
# The UI maps these to colours and tab filters.  EMPTY/USED/PERM are the states
# the N64 heaps use; UNPARSED is Xenia-only, for a stretch of heap that could
# not be read as node headers.  It is deliberately not one of the first three:
# the view degrades unknown states to "UNK", which is the honest label, whereas
# calling an unparsed gap "free" would be a lie the summary would then total up.
HEAP_HEADER_SIZE   = 0x10
HEAP_STATE_EMPTY   = 0
HEAP_STATE_USED    = 1
HEAP_STATE_PERM    = 2
HEAP_STATE_UNPARSED = 3


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


# ── Win32 structures ──────────────────────────────────────────────────────────

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
