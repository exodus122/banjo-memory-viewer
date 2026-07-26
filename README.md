# Banjo Memory Viewer — BizHawk N64 / Xenia Xbox 360 Memory Visualizer

A Python tool that hooks into a running emulator, reads live game memory,
and displays it in a dedicated GUI window. Supports **Banjo-Kazooie** and
**Banjo-Tooie**, running on either the original N64 versions via **BizHawk**
or the Xbox 360 remasters via **Xenia-canary**. On connect, it automatically
detects whichever emulator and game is running and picks the matching
profile.

---

## Features

| Tab | What it shows |
|-----|---------------|
| **Heap** | Every block in the dynamic heap — state (FREE/USED/PERM), address range, sizes, auto-detected type. Visual bar chart with hover tooltips and click-to-scroll. Sortable, filterable. CSV export. |
| **Actors** | Every live entry in the game's actor array — marker/model IDs resolved to human-readable names, position, yaw/pitch/roll, scale, and state. Can point at a custom array address. Sortable, filterable. CSV export. |
| **Watches** | User-defined memory watches with labels, data types, grouping, freeze/poke, drag-and-drop reorder, and pointer chain expressions. Auto-saved per game. |
| **Hex Viewer** | Live hex dump of any RDRAM/memory region. Changed bytes highlighted in red. Preset regions per game + manual jump-to-address. |

---

## Requirements

- Python 3.9+
- Windows (both `ReadProcessMemory` backends are Windows-only)
- One of:
  - **BizHawk** 2.9+ with the N64 core, for the original N64 versions: https://tasvideos.org/BizHawk
  - **Xenia-canary**, for the Xbox 360 remasters (Banjo-Kazooie / Banjo-Tooie via Rare Replay or XBLA)

```
pip install -r requirements.txt
```

Alternatively, grab the prebuilt standalone `.exe` — see
[Building a standalone .exe](#building-a-standalone-exe) below — which needs
no Python install at all.

---

## Running

```bash
python main.py
```

1. Start BizHawk (load Banjo-Kazooie or Banjo-Tooie, USA) **or** start
   Xenia-canary with the Xbox 360 version of either game.
2. Click **⟳ CONNECT** in the app.
3. The app tries BizHawk first, then Xenia, and auto-detects which game is
   running from whichever one it finds.

> **Run as Administrator** if Connect fails with a process access error.

> **Windows only.** The app requires a running BizHawk or Xenia-canary
> instance to connect to — there's no offline/demo mode.

---

## Building a standalone .exe

A prebuilt Windows executable can be produced with PyInstaller so the app
can be shared and run without installing Python:

```
build_exe.bat
```

This produces `dist\Banjo Memory Viewer.exe`. Share the whole `dist\` folder
— the `.exe` also needs the per-game `*_watches.json` files in a `watches\`
subfolder next to it so each game's watch list can be loaded and saved.

---

## Game support and auto-detection

The app supports four **game profiles**: Banjo-Kazooie and Banjo-Tooie, each
on either BizHawk (N64) or Xenia-canary (Xbox 360). Each profile stores all
game- and emulator-specific constants: heap boundaries, overlay ID maps,
hex-viewer preset regions, and the watches JSON filename.

On connect, the app scans BizHawk's RDRAM (or, if BizHawk isn't running,
Xenia's process memory) for each game's boot signature bytes, then confirms
the match. If the detected game differs from the current profile, the app
**automatically switches**, saving the old game's watches before loading the
new ones.

While running, the app **re-verifies the game signature every ~5 seconds**
(BizHawk profiles) or that the Xenia process is still alive (Xenia profiles).
If a different ROM loads, it detects the change and switches profiles without
any user action needed. Watch files are never cross-written: the old game's
watches are always fully saved before the new game's are loaded.

---

## Toolbar

| Control | Function |
|---------|----------|
| **⟳ CONNECT** | Scan for BizHawk (then Xenia), open the process, auto-detect the game. |
| **⏸ PAUSE / ▶ RESUME** | Freeze all polling. Useful when you need a stable snapshot. |
| fps counter | Shows the poll rate of the most recently completed frame. |
| Game / ROM label | Displays the active game profile name next to the status dot. |
| ● status dot | Green = connected live, red = disconnected. |

---

## Heap tab

The heap tab walks the game's doubly-linked heap block list every poll and
displays each block as a row in a sortable table.

### Columns

| Column | Description |
|--------|-------------|
| State | FREE / USED / PERM |
| Address | N64 virtual start address of the block header |
| End addr | Last byte address of the block |
| Chunk size | Usable bytes in the block (excluding the 0x10-byte header) |
| Used size | Bytes actively occupied (chunk minus the unused trailing bytes) |
| Type | Auto-detected block type (see below) |
| Label | Symbol name when known |
| Source | Source file the symbol comes from |

### Block type detection

The app identifies blocks by comparing their start address against a set
of global pointers read live from RDRAM each poll. Recognised types include:

- **modelCache** — the model cache block (`gModelCache`)
- **markerList** — the object marker list (`gMarkerList`)
- **particle** — individual particle emitter slots (`gPartEmitMgr[n]`)
- **assetCache** — entries in the asset cache pointer array
- **overlay** — loaded overlay/level code segments
- **soundfont** — instrument banks at known fixed addresses (BK only)
- **BoneTransformList** — bone transform arrays (BT only)
- **EmptyHeapBlock** — sentinel blocks at heap boundaries
- **free** — unallocated blocks
- **unknown** — used/perm blocks not matched by any of the above

Dynamic types (asset, particle, unknown) are re-checked every frame.
All other types are cached once matched and only rechecked if the block
disappears from the list.

### Sorting and filtering

Click any column header to sort ascending; click again to sort descending.
The filter toolbar above the table has one-click tabs for **ALL**, **FREE**,
**USED**, and **PERM**, plus a free-text search box that matches against
address, type, label, and source columns simultaneously.

### Bar chart

A compact horizontal bar at the top of the heap tab shows the overall heap
layout — free (red), used (green), and permanent (blue) — proportional to
their actual sizes, with visible separators between blocks. Hover a segment
for a tooltip with that block's details, or click it to scroll straight to
that row in the table below.

### CSV export

The **Dump CSV** button saves the current filtered and sorted view to a
timestamped `.csv` file in the script directory.

### Status bar

The status bar always shows: active game, current level/map name,
total block count, free KB (and fragment count), and used KB.

---

## Actors tab

Walks the game's live actor array every poll and shows one row per slot:
address, marker/model pointers, marker and model IDs resolved to
human-readable names (via the enum/asset tables), position (X/Y/Z),
yaw/pitch/roll, scale, init state, and whether the slot is despawned.

- **Show despawned slots** — off by default; check it to include slots the
  game has marked despawned/inactive.
- **Filter** — free-text search across the visible columns.
- **Array / Override addr** — switch between the game's known actor arrays,
  or point the viewer at any custom address to inspect it the same way.
- Sortable by any column; **Dump CSV** exports the current filtered/sorted
  view the same way the Heap tab does.

---

## Hex Viewer tab

A scrollable live hex dump of any region of N64 RDRAM.

- **16 bytes per row** with N64 virtual addresses on the left and an ASCII
  sidebar on the right.
- **Changed bytes** (since the previous frame) are highlighted in red.
- **Preset regions** — a dropdown lets you jump to common memory areas for
  the active game (heap, stack, overlay manager, HUD, etc.). The list updates
  automatically when you switch games.
- **Jump-to-address** — type any hex address and press Enter to view a custom
  4KB window starting there.
- Only the visible rows are tagged on each frame; off-screen rows are skipped
  for performance.

---

## Watches tab

User-defined memory watches that read (and optionally write) arbitrary N64
addresses every ~16 ms (~60 fps).

### Data types

| Key | Type | Width |
|-----|------|-------|
| `u8` | Unsigned byte | 1 B |
| `s8` | Signed byte | 1 B |
| `u16` | Unsigned short | 2 B |
| `s16` | Signed short | 2 B |
| `u32` | Unsigned long | 4 B |
| `s32` | Signed long | 4 B |
| `f32` | IEEE 754 float | 4 B |
| `u64` | Unsigned long long | 8 B |
| `s64` | Signed long long | 8 B |

All multi-byte reads/writes are big-endian (N64 native).

### Address expressions and pointer chains

Watches support a full expression language for pointer chains, not just plain
addresses:

| Example | Meaning |
|---------|---------|
| `0x80123456` | Plain N64 address |
| `[0x80000010]` | Dereference: read u32 at that address, use the result as the target |
| `[[0x80000010]]` | Double dereference |
| `[0x80000010]+0x1C` | Dereference, then add an offset |
| `[0x80135490+4*[0x801354DC]]+0x724` | BT-style object table (multiply supported) |

Pointer watches are shown in blue in the table. Watches whose pointer chain
failed to resolve (e.g. null pointer, not connected) are shown in red.

### Groups

Watches can be organised into collapsible groups. Add groups from the
**+ GROUP** button or the right-click context menu. Collapsing a group hides
all its child watches from view.

### Freeze / unfreeze

Right-click a watch and choose **Toggle freeze** to lock its value. While
frozen, the app writes the stored freeze value back to RDRAM every poll
frame, preventing the game from changing it. Frozen watches are highlighted
in orange.

Each watch stores its own freeze value independently of its current live
reading. You can edit the freeze value at any time through the edit dialog.

### Set value now

Right-click → **Set value now** for a one-shot write without enabling
freeze. Accepts decimal, signed decimal, hex (`0x...`), or float literals
depending on the watch's data type. The written value is also stored as the
watch's freeze value for convenience.

### Sorting

Click any column header (Label, Address, Type, Value, Frozen) to sort the
watch list. Click again to reverse. Groups are sorted among themselves; their
children stay attached to them.

### Drag-and-drop reorder

Click and drag any watch or group row to reorder. A green insert line shows
the drop target position. Watches can be moved between groups or to the top
level.

### Adding and editing watches

- **+ ADD** button or right-click → **Add watch** opens the add dialog.
- **Double-click** any existing watch row to edit it in place.
- Fields: Label, Address expression, Data type, Display as hex, Freeze value, Group, Description.
- The description field accepts multi-line notes — useful for documenting what
  freeze values mean, as with the BT "Next Character" watch.

### Save / Load

- **SAVE** writes the current watch list to the active game's JSON file.
- **LOAD** opens a file picker to load any `.json` watch file.
- Watches are saved automatically on window close and on every game switch,
  so each game/emulator combination always has its own independent file
  (`bk_watches.json` / `bt_watches.json` / `bk_xenia_watches.json` /
  `bt_xenia_watches.json`).

---

## How it works

### Memory reading

BizHawk stores the full 8MB N64 RDRAM as a contiguous flat buffer in its
process memory. The app:

1. Enumerates all running processes to find `EmuHawk.exe`.
2. Opens the process with `PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION`.
3. Scans committed virtual memory regions for a ≥8MB block containing
   the game's boot signature at a known RDRAM offset, confirmed by the ROM title string.
4. All reads use `ReadProcessMemory(handle, rdram_base + (n64_addr - 0x80000000), ...)`.
5. All multi-byte values are read and written as **big-endian** (N64 native).

### N64 byte-swap and word alignment

All byte-level writes reconstruct the full aligned 32-bit word, modify the
target byte(s) within it, then write the whole word back — preserving
surrounding bytes correctly.

### N64 address → BizHawk process address

```
process_addr = rdram_base + (n64_addr - 0x80000000)
```

BizHawk uses domain name `"RDRAM"` internally; the app bypasses BizHawk's
API entirely and reads directly, so no Lua or socket bridge is needed.

### Xenia backend

The Xenia-canary reader works the same way in spirit — it finds the Xenia
process, opens it, and reads guest memory directly via `ReadProcessMemory` —
but Xbox 360 addresses, heap layout, and struct offsets all differ from the
N64 versions, so Xenia profiles carry their own separate set of constants
rather than reusing the BizHawk ones.

### Heap walking

Both games use the same doubly-linked block list format. Each 0x10-byte header:

| Offset | Field |
|--------|-------|
| +0x0 | prev block ptr (u32 BE) |
| +0x4 | next block ptr (u32 BE) |
| +0xC–0xE | unused bytes in block (u24 BE) |
| +0xF bits 7–6 | state: 0=FREE 1=USED 2=PERM |

`chunk_size` = next_ptr − block_addr − 0x10  
`used_size` = chunk_size − unused_bytes

BK heap: starts at `0x8002D500`, size `0x210520`.  
BT heap: starts at `0x80137800`, size `0x2C8800` (~2.8 MB).

---

## File structure

```
banjo-memory-viewer/
├── main.py                Entry point
├── trainer_app.py         Main window, polling loop, game-switch logic
├── bizhawk_memory.py      BizHawk (N64) memory reader, heap walker, game profiles, Xenia reader
├── heap_view.py           Heap block table, bar chart, type tagging, CSV export
├── actors_view.py         Live actor array table, name resolution, CSV export
├── hex_view.py            Scrollable hex dump with change highlighting
├── watches_view.py        Memory watches: freeze, poke, pointer chains, drag-drop
├── bt_assets.py           Banjo-Tooie asset name table for block type tagging
├── enums.h                Banjo-Kazooie enums for block type tagging
├── build_exe.bat          Builds the standalone Windows .exe via PyInstaller
└── watches/
    ├── bk_watches.json        Saved watches for Banjo-Kazooie (BizHawk)
    ├── bt_watches.json        Saved watches for Banjo-Tooie (BizHawk)
    ├── bk_xenia_watches.json  Saved watches for Banjo-Kazooie (Xenia)
    └── bt_xenia_watches.json  Saved watches for Banjo-Tooie (Xenia)
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "EmuHawk.exe not found" | Start BizHawk and load a supported game first, or start Xenia-canary instead |
| "Failed to open BizHawk process" | Run the app as Administrator |
| "Could not locate RDRAM" | Make sure the N64 core is active and the ROM is running (not paused at the very first frame) |
| Heap/Actors shows 0 rows | The game may still be loading — wait for the title screen |
| Wrong watch values | BK addresses are for **USA v1.0** only; BT addresses are for **USA** only; Xenia profiles use different addresses than their BizHawk counterparts |
| App runs but shows no data | Red dot = disconnected — make sure BizHawk or Xenia-canary is running with a supported game loaded |
