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

from bizhawk_memory import XeniaMemoryReader, XENIA_BK_PROFILE


def main():
    reader = XeniaMemoryReader(XENIA_BK_PROFILE)

    ok, msg, profile = reader.connect()
    print(msg)
    if not ok:
        return 1

    # connect() auto-detects the game; force the BK profile so the BK walker is
    # used even if detection guessed Tooie.
    reader.profile = XENIA_BK_PROFILE

    print()
    lines = [reader.debug_bk_heap()]

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
