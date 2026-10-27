"""apply_patches.py [pid] - apply all runtime patches to a running Europa_Client.exe.

Patches (idempotent - safe to run twice):
  1. PH assert NOP: ProtocalHandler_x64.dll + 0x38dca  ff 15 a0 42 09 00 -> 90x6
     (protocol-handler assertion that fires on our msg sequences)
  2. watchdog skip: EXE 0x14113f2ef  75 -> EB  (ForceReconnect watchdog
     conditional jump -> unconditional, keeps :8002 conn alive)
  3. teardown NOP: EXE 0x140babab1  ff 50 58 -> 31 c0 90  (EXE->PH teardown
     call -> xor eax,eax; nop - leaks instead of UAF-ing the conn obj)

Waits for the process and ProtocalHandler_x64.dll to appear (module loads
late after launch). Usage:
    python apply_patches.py            # auto-discover Europa_Client.exe PID
    python apply_patches.py 12345
    python apply_patches.py --wait-lobby <probe_log>   # wait for lobby traffic
                                                       # before patching

--wait-lobby tails the probe log (from current EOF) until it sees a client
msgid that only appears post-login (heartbeat/room-poll). Patching mid-login
stalls the client - verified: teardown NOP fired during the login burst
froze the pipeline; post-lobby patching is harmless.
"""
import ctypes, ctypes.wintypes as w, glob, os, re, sys, time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = w.HANDLE
k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.VirtualProtectEx.argtypes = [w.HANDLE, w.LPVOID, ctypes.c_size_t, w.DWORD, ctypes.POINTER(w.DWORD)]
k32.VirtualProtectEx.restype = w.BOOL
k32.WriteProcessMemory.argtypes = [w.HANDLE, w.LPVOID, w.LPCVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CreateToolhelp32Snapshot.restype = w.HANDLE
                                                      
PAGE_EXECUTE_READWRITE = 0x40


class ME(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("th32ModuleID", w.DWORD), ("th32ProcessID", w.DWORD),
                ("GlblcntUsage", w.DWORD), ("ProccntUsage", w.DWORD),
                ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)), ("modBaseSize", w.DWORD),
                ("hModule", w.HMODULE), ("szModule", w.WCHAR * 256), ("szExeFile", w.WCHAR * 260)]


class PE(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_byte)), ("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]

k32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
k32.CreateToolhelp32Snapshot.restype = w.HANDLE
k32.Module32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(ME)]
k32.Module32FirstW.restype = w.BOOL
k32.Module32NextW.argtypes = [w.HANDLE, ctypes.POINTER(ME)]
k32.Module32NextW.restype = w.BOOL
k32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(PE)]
k32.Process32FirstW.restype = w.BOOL
k32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PE)]
k32.Process32NextW.restype = w.BOOL


def find_pid(name="Europa_Client.exe", timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        hs = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        pe = PE(); pe.dwSize = ctypes.sizeof(pe)
        pid = None
        if k32.Process32FirstW(hs, ctypes.byref(pe)):
            while True:
                if pe.szExeFile == name:
                    pid = pe.th32ProcessID
                    break
                if not k32.Process32NextW(hs, ctypes.byref(pe)):
                    break
        k32.CloseHandle(hs)
        if pid:
            return pid
        time.sleep(0.5)
    return None


def module_base(pid, name):
    hs = k32.CreateToolhelp32Snapshot(0x18, pid)  # SNAPSHOTMODULE|MODULE32
    if hs in (0, -1, 0xFFFFFFFFFFFFFFFF) or hs is None:
        return None
    me = ME(); me.dwSize = ctypes.sizeof(me)
    base = None
    if k32.Module32FirstW(hs, ctypes.byref(me)):
        while True:
            if me.szModule == name:
                base = ctypes.cast(me.modBaseAddr, w.LPVOID).value
                break
            if not k32.Module32NextW(hs, ctypes.byref(me)):
                break
    k32.CloseHandle(hs)
    return base


def read_mem(h, addr, n):
    buf = ctypes.create_string_buffer(n)
    cnt = ctypes.c_size_t()
    k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(cnt))
    return buf.raw


def patch(h, addr, expected, patched, label):
    cur = read_mem(h, addr, len(expected))
    if cur[:len(patched)] == patched:
        print(f"  {label}: already patched ({cur.hex()})")
        return True
    if cur != expected:
        print(f"  {label}: FAILED - expected {expected.hex()}, found {cur.hex()}")
        return False
    old = w.DWORD()
    k32.VirtualProtectEx(h, addr, len(patched), PAGE_EXECUTE_READWRITE, ctypes.byref(old))
    cnt = ctypes.c_size_t()
    ok = k32.WriteProcessMemory(h, ctypes.c_void_p(addr), patched, len(patched), ctypes.byref(cnt))
    k32.VirtualProtectEx(h, addr, len(patched), old.value, ctypes.byref(w.DWORD()))
    after = read_mem(h, addr, len(patched))
    good = bool(ok) and after == patched
    print(f"  {label}: {'OK' if good else 'WRITE FAILED'} ({cur.hex()} -> {after.hex()})")
    return good


def _resolve_lobby_log(path):
    """Accept a file, a lobby/ dir, or a run dir; return the events.jsonl path."""
    if os.path.isfile(path):
        return path
    cands = glob.glob(os.path.join(path, "events.jsonl"))
    cands += glob.glob(os.path.join(path, "lobby", "*", "events.jsonl"))
    cands += glob.glob(os.path.join(path, "*", "events.jsonl"))
    return max(cands, key=os.path.getmtime) if cands else None


def wait_lobby(log_path, start_off=0, timeout=240, settle=10.0):
    """Wait until the client reaches the lobby AND its bootstrap traffic
    settles (quiet window). Patching mid-handshake makes the client cycle
    its lobby TCP; the re-login session skips bootstrap and breaks the
    room list / appearance. Settle window ensures we patch an idle client."""
    real = _resolve_lobby_log(log_path)
    if not real:
        print(f"--wait-lobby: no events.jsonl under {log_path} - patching immediately")
        return
    try:
        f = open(real, "r", errors="replace")
    except OSError as e:
        print(f"--wait-lobby: cannot open {real}: {e} - patching immediately")
        return
    f.seek(start_off)  # only watch lines written after patcher started
    end = time.time() + timeout
    pat = re.compile(r'"(client_msg|room_event|local_actor_uid)"')
    print(f"--wait-lobby: watching {real} for post-login traffic + {settle:.0f}s settle ...")
    first_signal = None
    while time.time() < end:
        line = f.readline()  # readline: 'for line in f' buffers ahead and
        if line:             # misses lines appended while we poll
            if pat.search(line):
                first_signal = first_signal or time.time()
            continue
        if first_signal and (time.time() - first_signal) >= settle:
            # Пост-логин трафик идёт постоянно (0x3783-поллинг) — «тихого»
            # окна не бывает; ждём settle сек. после ПЕРВОГО сигнала.
            print(f"--wait-lobby: lobby seen +{settle:.0f}s - patching")
            f.close()
            return
        time.sleep(0.5)
    f.close()
    print("--wait-lobby: timed out - patching anyway (client may be stuck pre-lobby)")


def main():
    import os
    wait_log = None
    args = sys.argv[1:]
    if "--wait-lobby" in args:
        i = args.index("--wait-lobby")
        wait_log = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    wait_off = 0
    if wait_log:
        real_log = _resolve_lobby_log(wait_log)
        try:
            wait_off = os.path.getsize(real_log or wait_log)
        except OSError:
            wait_off = 0
    if args:
        pid = int(args[0])
    else:
        print("waiting for Europa_Client.exe ...")
        pid = find_pid()
        if not pid:
            print("no Europa_Client.exe found"); return 2
    print(f"pid={pid}")

    # wait for ProtocalHandler_x64.dll (loads after the login flow starts)
    ph = exe = None
    end = time.time() + 60
    while time.time() < end:
        ph = module_base(pid, "ProtocalHandler_x64.dll")
        exe = module_base(pid, "Europa_Client.exe")
        if ph and exe:
            break
        time.sleep(1.0)
    if not ph:
        print("ProtocalHandler_x64.dll not loaded after 60s - aborting")
        return 3
    if not exe:
        print("Europa_Client.exe module not found - aborting")
        return 3
    print(f"PH base: {ph:#x}   EXE base: {exe:#x}")

    if wait_log:
        wait_lobby(wait_log, wait_off)

    h = k32.OpenProcess(0x0438, False, pid)  # ALL_ACCESS-ish set used by prior tools
    if not h:
        print("OpenProcess failed"); return 4

    ok = True
    ok &= patch(h, ph + 0x38dca, bytes.fromhex("ff15a0420900"), b"\x90" * 6,
                "PH assert NOP")
    # 2026-10-09 20:25 (user-approved experiment, REVERT AFTER): the watchdog
    # skip is ON while testing the pre-scene path. Evidence: the ForceReconnect
    # watchdog code at exe+0x113f8b3 reads the route-mode flag 0x14302a9d0, and
    # both historical winning re-dials (cmd=6 relay register + 0xd78 poll,
    # 18:39:19 / 19:12:12) came from auto_test.py runs that applied this patch.
    # Hypothesis: with the watchdog live, our RST recycle trips ForceReconnect,
    # which resets the route mode to 0 -> migration dial(mgr,2) -> full
    # cmd=3 login with no 0xd78 poll -> rearm d77/d76 ignored (wedge).
    # The ~30-35s :8002 churn/re-entry test needs it OFF again afterwards.
    ok &= patch(h, exe + 0x113f2ef, bytes([0x75]), bytes([0xEB]),
                "watchdog skip")
    ok &= patch(h, exe + 0xbabab1, bytes.fromhex("ff5058"), bytes([0x31, 0xC0, 0x90]),
                "teardown NOP")
    k32.CloseHandle(h)
    print("all patches applied" if ok else "SOME PATCHES FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
