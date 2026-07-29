--------------------------------------------------------------------------------
-- Find static pointers to Banjo-Tooie (XBLA) heap nodes.
--
-- Xenia maps guest memory at host = 0x100000000 + guest, big-endian.
--
-- SLAB HEAP: nodes start on 0x10000 boundaries; the payload the game passes
-- around is node + 0x40.  So a pointer to a slab object reads 0x00NN0040.
--
-- ALLOCATOR HEAPS (0x40000000+): a TRACKED node's payload is P + 0x50, and the
-- node carries its own address at P + 0x10 -- so a candidate can be verified
-- rather than guessed at.
--
-- Every hit is validated by reading the thing it points at:
--   slab   -> first word == node address, and 0xDEDEDEDE at 0x40 + data_len
--   heap   -> word at P+0x10 == P+0x10
-- Unvalidated near-misses are counted separately so nothing is hidden.
--------------------------------------------------------------------------------

local start_addr = 0x1826A0000        -- host addresses to scan
local end_addr   = 0x1826B0000

local PHYS_BASE   = 0x100000000
local SLAB_STRIDE = 0x10000
local SLAB_HDR    = 0x40              -- slab payload offset
local FOOTER      = 0xDEDEDEDE
local SLAB_LO     = 0x00010000        -- plausible slab-heap guest range
local SLAB_HI     = 0x00800000

local TRACKED_HDR = 0x50              -- allocator-heap tracked payload offset
local SELF_OFF    = 0x10
local HEAP_LO     = 0x40000000
local HEAP_HI     = 0x42000000

-- The allocator ROOT block holds 128 free-list bins, and an empty bin points at
-- itself.  That satisfies "word at P+0x10 == P+0x10" by construction, so every
-- bin looks like a valid tracked node.  Exclude the block outright.
local ROOT_LO     = 0x40000000
local ROOT_HI     = 0x40000630

local addToList   = true              -- create Cheat Engine address records
local showMisses  = 12                -- print this many near misses, 0 = none

local function bswap32(x)
    return ((x & 0xFF) << 24) |
           ((x & 0xFF00) << 8) |
           ((x & 0xFF0000) >> 8) |
           ((x >> 24) & 0xFF)
end

-- Read a big-endian u32 from a HOST address; nil if unreadable.
local function readBE(addr)
    local ok, v = pcall(readInteger, addr)
    if not ok or v == nil then return nil end
    return bswap32(v) & 0xFFFFFFFF
end

-- Read a big-endian u32 from a GUEST address.
local function readGuest(guest)
    return readBE(PHYS_BASE + guest)
end

-- Does `guest` look like a live slab payload?  Returns data_len or nil.
local function checkSlab(guest)
    local node = guest - SLAB_HDR
    if (node & (SLAB_STRIDE - 1)) ~= 0 then return nil end
    if node < SLAB_LO or node >= SLAB_HI then return nil end
    if readGuest(node) ~= node then return nil end          -- self address
    local len = readGuest(node + 4)
    if len == nil or len == 0 or len > 0x2000000 then return nil end
    if readGuest(node + SLAB_HDR + len) ~= FOOTER then return nil end
    return len
end

-- Does `guest` look like a tracked allocator-heap payload?  Returns P or nil.
local function checkHeapNode(guest)
    local p = guest - TRACKED_HDR
    if (p & 0xF) ~= 0 then return nil end
    if p < HEAP_LO or p >= HEAP_HI then return nil end
    if p >= ROOT_LO and p < ROOT_HI then return nil end     -- bin table, not a node
    if readGuest(p + SELF_OFF) ~= (p + SELF_OFF) then return nil end
    return p
end

local function record(addr, desc)
    if not addToList then return end
    local rec = getAddressList().createMemoryRecord()
    rec.Address     = string.format("%X", addr)
    rec.Type        = vtDword
    rec.Description = desc
end

local slabHits, heapHits, nearMiss = 0, 0, 0
local hits   = {}          -- ordered {addr, kind, target} for table detection
local misses = {}

print(string.format("Scanning %X - %X for heap pointers", start_addr, end_addr))

-- Step 4, not 16: a pointer FIELD can sit at any 4-byte offset.  (The original
-- script's 16-byte step was right for finding self-pointers inside node
-- headers, but would miss three quarters of ordinary pointer fields.)
for addr = start_addr, end_addr - 4, 4 do
    local val = readBE(addr)
    if val then
        if (val & 0xFFFF) == SLAB_HDR and (val >> 24) == 0 then
            local len = checkSlab(val)
            if len then
                slabHits = slabHits + 1
                local node = val - SLAB_HDR
                print(string.format("SLAB  %09X -> %08X  node %08X  len %X",
                                    addr, val, node, len))
                record(addr, string.format("slab %08X len %X", node, len))
                hits[#hits+1] = {addr = addr, kind = "SLAB", target = val}
            else
                -- Right shape, wrong target: worth knowing about, but not a hit.
                nearMiss = nearMiss + 1
                if #misses < showMisses then
                    misses[#misses+1] = string.format("%09X -> %08X", addr, val)
                end
            end
        elseif val >= HEAP_LO and val < HEAP_HI then
            local p = checkHeapNode(val)
            if p then
                heapHits = heapHits + 1
                print(string.format("HEAP  %09X -> %08X  node %08X",
                                    addr, val, p))
                record(addr, string.format("heapnode %08X", p))
                hits[#hits+1] = {addr = addr, kind = "HEAP", target = val}
            end
        end
    end
end

--------------------------------------------------------------------------------
-- Pointer tables.
--
-- Individual pointers name one object; a run of them at constant stride is an
-- ARRAY of records with a pointer at a fixed offset, which names a whole
-- category at once.  Those are the ones worth adding to the tag scan cache.
--------------------------------------------------------------------------------
-- 2 is deliberate, not sloppy: a double-buffered resource is a two-entry table,
-- and requiring 3 hid one (0x182674774, which swaps two heap nodes each frame).
-- Pairs at stride 4 or 8 are common enough to be worth the extra noise.
local MIN_RUN     = 2
local MAX_STRIDE  = 0x40

print("")
print("Pointer tables (runs at constant stride):")
local tables_found = 0
local i = 1
while i <= #hits do
    local j = i
    local stride = nil
    while j < #hits do
        local d = hits[j+1].addr - hits[j].addr
        if d <= 0 or d > MAX_STRIDE then break end
        if stride == nil then stride = d
        elseif d ~= stride then break end
        j = j + 1
    end
    local count = j - i + 1
    if count >= MIN_RUN and stride then
        tables_found = tables_found + 1
        local kinds = {}
        for k = i, j do kinds[hits[k].kind] = true end
        local kind = (kinds.SLAB and kinds.HEAP) and "mixed"
                     or (kinds.SLAB and "slab" or "heap")
        print(string.format("  %09X  stride 0x%-3X  %3d entries  (%s)  first -> %08X",
                            hits[i].addr, stride, count, kind, hits[i].target))
    end
    i = (count >= MIN_RUN and stride) and (j + 1) or (i + 1)
end
if tables_found == 0 then print("  none") end

--------------------------------------------------------------------------------
-- Sparse tables.
--
-- The dense pass above breaks at the first slot that isn't a live pointer, so a
-- table with NULL or stale entries is reported as several short runs -- or
-- missed entirely.  Banjo-Tooie on N64 keeps its bone transforms in a 340-entry
-- table (D_80379E20, stride 8) that is mostly empty at any moment, and that
-- shape is invisible to a contiguous-run detector.
--
-- So: bucket hits by (addr mod stride) and look for clusters whose gaps are
-- whole multiples of the stride.  A table with 20 live entries out of 340 shows
-- up as one cluster instead of vanishing.
--------------------------------------------------------------------------------
-- Proximity is not structure.  A first cut of this pass reported 38 "tables"
-- from 254 hits -- the same regions re-grouped under several strides -- because
-- with stride 4 and a generous gap any two nearby pointers chain together.
--
-- The fix is to check the slots we did NOT hit.  In a real table every slot is
-- either a valid pointer or empty; in a coincidental cluster the space between
-- hits is ordinary data.  So a candidate only counts if EVERY slot across its
-- span reads as NULL or as a valid slab/heap pointer.
local STRIDES  = {0x4, 0x8, 0xC, 0x10, 0x14, 0x18, 0x20}
local MAX_GAP  = 24        -- empty slots tolerated between live entries
local MIN_LIVE = 4         -- live entries needed to call it a table

local function slotsLookLikeTable(base, stride, slots)
    local other = 0
    for i = 0, slots - 1 do
        local v = readBE(base + i * stride)
        if v == nil then return false, -1 end
        if v ~= 0 and not checkSlab(v) and not checkHeapNode(v) then
            other = other + 1
            if other > 0 then return false, other end   -- strict: zero tolerance
        end
    end
    return true, other
end

print("")
print("Sparse tables (constant stride, NULL slots allowed):")
local sparse_found = 0
local reported = {}

-- A region densely packed with pointers validates at EVERY stride that divides
-- it, so one array gets reported two or three times at coarser strides (seen:
-- 0x182698E70 as stride 8/0x10/0x18 over the same bytes).  STRIDES is ascending,
-- so report the smallest stride that explains a span and suppress anything that
-- merely re-describes bytes already covered.
local covered = {}      -- {lo, hi} spans already reported
local function overlapsCovered(lo, hi)
    for _, c in ipairs(covered) do
        if lo < c[2] and hi > c[1] then return true end
    end
    return false
end

for _, stride in ipairs(STRIDES) do
    local buckets = {}
    for _, h in ipairs(hits) do
        local key = h.addr % stride
        buckets[key] = buckets[key] or {}
        table.insert(buckets[key], h)
    end
    for _, list in pairs(buckets) do
        local i = 1
        while i <= #list do
            local j = i
            while j < #list do
                local gap = list[j+1].addr - list[j].addr
                if gap % stride ~= 0 or gap > stride * MAX_GAP then break end
                j = j + 1
            end
            local live = j - i + 1
            if live >= MIN_LIVE then
                local span  = list[j].addr - list[i].addr
                local slots = span // stride + 1
                -- Only interesting if it is genuinely sparse; dense runs are
                -- already covered by the pass above.
                local lo, hi = list[i].addr, list[j].addr + stride
                if slots > live and not reported[list[i].addr]
                        and not overlapsCovered(lo, hi) then
                    local ok = slotsLookLikeTable(list[i].addr, stride, slots)
                    if ok then
                        reported[list[i].addr] = true
                        covered[#covered+1] = {lo, hi}
                        sparse_found = sparse_found + 1
                        print(string.format(
                            "  %09X  stride 0x%-3X  %3d live / %3d slots  first -> %08X",
                            list[i].addr, stride, live, slots, list[i].target))
                    end
                end
            end
            i = (live >= MIN_LIVE) and (j + 1) or (i + 1)
        end
    end
end
if sparse_found == 0 then print("  none") end

if #misses > 0 then
    print("")
    print(string.format("Near misses (%d total, showing %d) — right shape, "
                        .. "target did not validate:", nearMiss, #misses))
    for _, m in ipairs(misses) do print("  " .. m) end
end

print("")
print(string.format("Done. %d slab pointers, %d heap-node pointers, %d near misses",
                    slabHits, heapHits, nearMiss))
