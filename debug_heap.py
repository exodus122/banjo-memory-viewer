"""
Standalone BK/Xenia heap diagnostic.

Run it with Xenia already open and the game running:

    python debug_heap.py

It connects, prints a report on every heap it can find, and writes the same
text to debug_heap.txt next to this script so it is easy to paste elsewhere.
"""

import os
import sys
import traceback

from bizhawk_memory import (XeniaMemoryReader, XENIA_BK_PROFILE,
                            XENIA_BT_PROFILE, HEAP_STATE_USED)


def dump_big_allocation(reader, min_size=0x80000):
    """
    Hexdump the head of the largest allocation in every heap.

    A ~770KB block shows up in both games — in Kazooie's 0x40220000 heap and in
    Tooie's 0x40320000 heap, at near identical sizes — which says it is engine
    or emulator state rather than game content.  Its first bytes are the
    cheapest way to find out what it actually is.
    """
    out = ["=== largest allocations (first 0x80 bytes) ==="]
    seen = []
    for guest in reader.list_bk_heaps():
        for b in reader._walk_heap_bk(guest):
            if b["chunk_size"] >= min_size and b.get("xenia_payload"):
                seen.append((b["chunk_size"], guest, b))
    seen.sort(reverse=True, key=lambda t: t[0])
    if not seen:
        out.append("  none above 0x%X" % min_size)
    for size, guest, b in seen[:4]:
        data = reader._read_raw(b["xenia_payload"], 0x80)
        out.append("  heap 0x%08X  node 0x%08X  payload 0x%X  size 0x%X"
                   % (guest, b["xenia_guest"], b["xenia_payload"], size))
        if not data:
            out.append("    unreadable")
            continue
        for row in range(0, len(data), 16):
            chunk = data[row:row + 16]
            text = "".join(chr(c) if 0x20 <= c <= 0x7E else "." for c in chunk)
            out.append("    +%03X  %-47s  %s"
                       % (row, chunk.hex(" "), text))
    return "\n".join(out)


def survey_section(reader, title, sizes, texts, ptrs):
    out = ["", "=== %s ===" % title,
           "  -- most common allocation sizes --"]
    for size, count in sizes[:12]:
        out.append("     0x%-8X x%-5d  (%d bytes total)"
                   % (size, count, size * count))
    out.append("  -- text found in payloads --")
    if not texts:
        out.append("     none")
    for s, count in texts[:20]:
        out.append("     x%-5d %s" % (count, s[:60]))
    out.append("  -- XEX pointers by payload offset --")
    if not ptrs:
        out.append("     none")
    for (off, w), count in ptrs[:15]:
        named = reader.read_bk_cstring(w)
        out.append("     +0x%-4X 0x%08X x%-5d %s"
                   % (off, w, count, '"%s"' % named if named else ""))
    return out


def survey_all_heaps(reader):
    """Node count + survey for every FFEEFFEE heap, whichever game is running.

    debug_bt_heap() only lists Tooie's descriptors, so its allocator heaps had
    never been surveyed — which is exactly the data needed to say which of them
    holds the small game objects that live on the single N64 heap.
    """
    out = []
    for guest in reader.list_bk_heaps():
        d = reader.read_bk_heap_descriptor(guest)
        if not d:
            continue
        blocks = reader._walk_heap_bk(guest)
        used = sum(b["chunk_size"] for b in blocks
                   if b["state"] == HEAP_STATE_USED)
        title = ("heap 0x%08X  base 0x%08X  %d KB  align 0x%X  "
                 "-> %d nodes, 0x%X used"
                 % (guest, d["base"], d["size"] // 1024, d["alignment"],
                    len(blocks), used))
        out += survey_section(reader, title, *reader.survey_bk_heap(guest))
    return out


def main():
    reader = XeniaMemoryReader(XENIA_BK_PROFILE)

    ok, msg, profile = reader.connect()
    print(msg)
    if not ok:
        return 1

    # connect() auto-detects the game.  Honour that, but fall back to BK.
    is_bt = (profile is not None and profile.id == "xenia_bt")
    reader.profile = XENIA_BT_PROFILE if is_bt else XENIA_BK_PROFILE
    print("Running %s diagnostic" % ("Banjo-Tooie" if is_bt else "Banjo-Kazooie"))

    print()
    if is_bt:
        parts = [reader.debug_bt_heap()]

        # The slab heap is Tooie's real game-object heap, so survey it the same
        # way the allocator heaps are surveyed.
        parts += survey_section(reader, "slab heap (guest 0x70000)",
                                *reader.survey_slab_heap())

        # Tooie's allocator heaps have never been surveyed — this is what says
        # which of them holds the small objects that share one heap on N64.
        parts += survey_all_heaps(reader)

        parts.append("")
        parts.append(dump_big_allocation(reader))

        report = "\n".join(parts)
        print(report)
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "debug_heap.txt")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(msg + "\n\n" + report + "\n")
            print("\nSaved to %s" % out_path)
        except OSError as e:
            print("\nCould not save report: %s" % e)
        reader.disconnect()
        return 0

    lines = [reader.debug_bk_heap()]

    # Why isn't the actor array tagged?  Read the pointer the tagger uses and
    # report exactly where it lands.
    ACTOR_ARRAY_PTR = 0x18249F68C      # matches _build_bk_xenia_tag_scan_cache
    lines.append("")
    lines.append("=== actor array pointer ===")
    raw = reader.read_u32_be(ACTOR_ARRAY_PTR)
    lines.append("read_u32_be(0x%X) = %s"
                 % (ACTOR_ARRAY_PTR,
                    "0x%08X" % raw if raw is not None else "None (read failed)"))
    if raw:
        lines.append(reader.locate_pointer(raw))

    # Try to put names to allocations by following pointers out of payloads.
    for desc in reader.list_bk_heaps(refresh=True):
        d = reader.read_bk_heap_descriptor(desc)
        lines.append("")
        lines.append("=== heap @0x%08X (base 0x%08X, 0x%X bytes) ==="
                     % (desc, d["base"] if d else 0, d["size"] if d else 0))
        sizes, texts, ptrs = reader.survey_bk_heap(desc)

        lines.append("  -- most common allocation sizes --")
        for size, count in sizes[:12]:
            lines.append("     0x%-8X x%-5d  (%d bytes total)"
                         % (size, count, size * count))

        lines.append("  -- text found in payloads --")
        if not texts:
            lines.append("     none")
        for s, count in texts[:20]:
            lines.append("     x%-5d %s" % (count, s[:60]))

        lines.append("  -- XEX pointers by payload offset --")
        if not ptrs:
            lines.append("     none")
        for (off, w), count in ptrs[:15]:
            named = reader.read_bk_cstring(w)
            lines.append("     +0x%-4X 0x%08X x%-5d %s"
                         % (off, w, count, '"%s"' % named if named else ""))

        rows = reader.identify_bk_nodes(desc)
        if rows:
            lines.append("  -- grouped by leading XEX pointer --")
            for ptr, count, total, text, ssizes in rows[:15]:
                lines.append("     0x%08X x%-5d %8d bytes  sizes=%-20s %s"
                             % (ptr, count, total,
                                ",".join("0x%X" % s for s in ssizes),
                                '"%s"' % text if text else ""))

    lines.append("")
    lines.append(dump_big_allocation(reader))

    report = "\n".join(lines)
    print(report)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "debug_heap.txt")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(msg + "\n\n" + report + "\n")
        print("\nSaved to %s" % out_path)
    except OSError as e:
        print("\nCould not save report: %s" % e)

    reader.disconnect()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        code = 1
    # Keep the window open when launched by double-clicking.
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    sys.exit(code)
