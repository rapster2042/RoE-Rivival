"""Скан handler-map живого клиента: узлы {msgid@0,next@8,ctx@0x10,fn@0x18}.

Ищем qword-указатели на диапазон обработчиков FUN_140f15000..140f16fff
(и известные 140e88150/141184f80) во всех читаемых регионах процесса.
Для каждого попадания читаем msgid из node+0 (hit-0x18).
Также обходим известную таблицу модулей *(u64*)0x143040778.
"""
import ctypes, struct, sys
import ctypes.wintypes as w

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = w.HANDLE
k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.CreateToolhelp32Snapshot.restype = w.HANDLE

class PE32(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD),("cntUsage", w.DWORD),("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD),("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", ctypes.c_long),("dwFlags", w.DWORD),("szExeFile", ctypes.c_char*260)]

class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p),("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", w.DWORD),("PartitionId", ctypes.c_ushort),("RegionSize", ctypes.c_size_t),
                ("State", w.DWORD),("Protect", w.DWORD),("Type", w.DWORD)]

def find_pid(name="Europa_Client.exe"):
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    pe = PE32(); pe.dwSize = ctypes.sizeof(pe); pids=[]
    if k32.Process32First(snap, ctypes.byref(pe)):
        while True:
            if pe.szExeFile.decode("latin1").lower()==name.lower(): pids.append(pe.th32ProcessID)
            if not k32.Process32Next(snap, ctypes.byref(pe)): break
    k32.CloseHandle(snap); return pids

class Mem:
    def __init__(self, pid):
        self.h = k32.OpenProcess(0x0410|0x0008, False, pid)
    def rd(self, addr, n):
        buf = ctypes.create_string_buffer(n); got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
            return None
        return buf.raw[:got.value]
    def q(self, a):
        b=self.rd(a,8); return struct.unpack("<Q",b)[0] if b else None
    def u32(self,a):
        b=self.rd(a,4); return struct.unpack("<I",b)[0] if b else None

# handler fn range in image (base 0x140000000 assumed; verify by reading PE)
HANDLERS = set(range(0x140f15000, 0x140f16820, 4)) | {
    0x140e88150,0x141184f80,0x141185980,0x141185650,0x141185a30,0x140f15afd,
    0x140f15d06,0x140f15ff0,0x140f15230,0x140f15270,0x140f15100,0x140f15470,
    0x140f15610,0x140f15740,0x140dc3570,0x140f154ff,0x140f15555,0x140f155ed,
    0x140f155ff,0x140f15608,0x140f15632,0x140f15734,0x140f1573a,0x140f157c6,
    0x140f157d0,0x140f15871,0x140f15980,0x140f159ac,0x140f159b0,0x140f15a2a,
    0x140f15a55,0x140f15adb,0x140f15ae0,0x140f15ae5,0x140f15af0,0x140f15b5d,
    0x140f15b60,0x140f15bf9,0x140f15c4b,0x140f15cd6,0x140f15ce3,0x140f15ce8,
    0x140f15cf0,0x140f15cf9,0x140f15d32,0x140f15d37,0x140f15d3c,0x140f15d40,
    0x140f15d49,0x140f15d7e,0x140f15d83,0x140f15d88,0x140f15d90,0x140f15de0,
    0x140f15e9f,0x140f15eaa,0x140f15efc,0x140f15fef,0x140f1612f,0x140f16134,
    0x140f161c0,0x140f161df,0x140f162af,0x140f162b0,0x140f1674f,0x140f16750,
    0x140f167de,0x140f1680b,0x140f16810}

def main():
    pids = find_pid()
    print("pids:", pids)
    if not pids: return
    mem = Mem(pids[0])
    # verify base
    mz = mem.rd(0x140000000, 2)
    print("base check @140000000:", mz)
    # 1) known module table at 0x143040778
    root = mem.q(0x143040778)
    print("module-table root:", hex(root) if root else root)
    if root:
        buckets, cnt = mem.q(root+0xA0), mem.u32(root+0xA8)
        print(" buckets:", hex(buckets or 0), "cnt:", cnt)
        if buckets and cnt and cnt < 0x10000:
            seen = {}
            for i in range(cnt):
                node = mem.q(buckets+i*8); hops=0
                while node and hops<64:
                    raw = mem.rd(node, 0x30)
                    if not raw: break
                    mid, = struct.unpack_from("<i", raw, 0)
                    f1,mod,hnd,nxt = struct.unpack_from("<QQQQ", raw, 8)
                    if mid not in seen: seen[mid] = (mod,hnd)
                    node, hops = nxt, hops+1
            for mid,(mod,hnd) in sorted(seen.items()):
                mark = ' *HANDLER*' if hnd in HANDLERS else ''
                if hnd and 0x140000000 <= hnd < 0x144000000:
                    print(f"  msgid {mid:#06x} -> mod {mod:#x} hnd {hnd:#x}{mark}")
    # 2) raw scan for handler-fn pointers -> node msgid at -0x18
    print("\n== scanning committed regions for handler fn pointers ==")
    addr = 0
    found = {}
    mbi = MBI()
    scanned_regions = 0
    while addr < 0x7fffffffffff:
        r = k32.VirtualQueryEx(mem.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not r: break
        base = mbi.BaseAddress or 0; size = mbi.RegionSize or 0
        nxt = base + size
        if mbi.State == 0x1000 and size and size < 0x40000000 and (mbi.Protect & 0xcc):  # MEM_COMMIT, readable
            data = mem.rd(base, size)
            if data:
                scanned_regions += 1
                # scan qwords for values in handler range
                for off in range(0, len(data)-8, 8):
                    v, = struct.unpack_from("<Q", data, off)
                    if v in HANDLERS:
                        node = base+off-0x18
                        mid = mem.u32(node)
                        ctx = mem.q(node+0x10)
                        nxtp = mem.q(node+8)
                        key = (mid, v)
                        found.setdefault(key, []).append((node, ctx, nxtp))
        addr = nxt
        if nxt <= base: break
    print("regions scanned:", scanned_regions)
    for (mid, fn), hits in sorted(found.items(), key=lambda kv: kv[0][1]):
        print(f"fn {fn:#x} <- msgid {mid if mid is not None else '?':#08x}  nodes={len(hits)}"
              + "".join(f" [node={a:#x} ctx={c:#x} next={n:#x}]" for a,c,n in hits[:4]))

if __name__ == "__main__":
    main()
