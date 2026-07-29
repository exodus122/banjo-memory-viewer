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
local MIN_RUN     = 3
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

if #misses > 0 then
    print("")
    print(string.format("Near misses (%d total, showing %d) — right shape, "
                        .. "target did not validate:", nearMiss, #misses))
    for _, m in ipairs(misses) do print("  " .. m) end
end

print("")
print(string.format("Done. %d slab pointers, %d heap-node pointers, %d near misses",
                    slabHits, heapHits, nearMiss))
