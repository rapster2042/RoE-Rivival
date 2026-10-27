RoE PRIVATE SERVER KIT
======================

Self-contained offline backend for an installed copy of Ring of
Elysium. No external dependencies, nothing outside this folder is
required - except the game itself, which you point at on first run.

REQUIREMENTS
  - Windows 10/11 x64
  - An installed RoE client (the 2022 build; the launcher verifies
    the Europa_Client.exe hash and refuses other versions)

HOW TO RUN
  1. Double-click Launch-RoE.bat
  2. Point "Game files" at your RoE install folder - the one
     containing Europa_Client.exe. The choice is saved for next time.
  3. Pick your options:
       - Bots        : AI opponents in the match + count
       - Storm       : zone shrink on/off (off recommended for testing)
       - Storm pace  : Normal / x2 / x3 / x5 / x10 slower shrink
       - Half health : HP floor at 500 (testing aid)
       - Clients     : 1 = solo test, 2 = duo (client2 patched in-memory)
       - Map         : Winter Island only
  4. Press Launch. Wait for "Done" in the status bar.
  5. Change your grapple button from E to something else. This is because everyone gets a Super Jump (E) and a double jump Glider.
  6. In game: use the main Play button, Training or Customs (see below).

MAIN PLAY BUTTON (matchmaking)
  Pressing Play on the main menu now works like Training did:
  about 5 seconds of "searching", then Match Found, then the
  loading screen into the shared match. This is new - report any
  stall at "match found" or missing spawn-pick.

CUSTOMS FLOW (the workaround)
  The room list cannot host a fresh room on first open, so:
  1. Open Customs - a pre-seeded room is already in the list.
  2. Enter the seeded room.
  3. Press Delete to leave/remove it. The room will still be
     shown in the list - that is normal, it is a stale record.
  4. You can now create a new room normally.
  5. Ready -> Start -> loading -> pick your spawn.

BETWEEN GAMES
  - Second matches usually work: after the result screen you land
    back in the lobby and can start again.
  - Occasionally the whole process may need a restart between
    games: close the client, stop services, relaunch via the bat.
    Services are reused while running, so a restart is fast.

KNOWN ISSUES
  - Bots do not always walk above the snow right now - some
    appear sunk into the ground. They still fight.
  - You can see bots aiming at you, but player aim pose is not
    replicated client-to-client in Customs yet - human players
    see each other's movement, not where they are aiming.
  - Backpacks spawn in several traversal types but all show the
    generic name "Backpack Lv1/2/3" - pick one up to see which
    kind it is.
  - Match-end flow is minimal.
  - Only Winter Island (16010) is supported - server fixtures
    for other maps do not exist yet.

CONTROLS NOTE
  - Turbo jump: hold E for about half a second.

OPTIONS NOTES
  - If services are already running on :8001, Launch reuses them
    (the running services' options win; restart them to change).
  - Storm pace only applies when Storm is checked, and only on a
    fresh service start.
  - Duo mode: client 1 creates the room, client 2 joins via room
    list. Client 2 gets memory patches after a 60s lobby settle -
    that is normal, leave it alone during the wait.
  - Game files are never modified; all patching happens in memory.

FOLDER LAYOUT
  tools/        backend + patcher scripts
  server/       lobby emulator (.NET) + game endpoint
  config/       server fixtures (map 16010 only)
  private/      session key
  .tools/       bundled Python + .NET runtimes
  runtime/      local account + region data
  runs/         per-session logs (created on launch)
  logs/         service stdout/stderr logs
  launcher-settings.json   remembered game-files path

TROUBLESHOOTING
  - "already running"   : close Europa_Client.exe first.
  - Stuck at match found: services started without room-service;
                          kill services via Task Manager (python.exe,
                          dotnet.exe) and relaunch through this UI.
  - Stuck at "Starting services": check logs\services-*.err.log,
                          then close and relaunch.
  - Crash on load       : corrupted/incomplete client install.
  - Service errors      : see logs\services-*.err.log.

GLIDER-LANDING BUG
  If you get stuck in the glider and cannot land, your install's
  UserData folder is corrupted (client treats the glider as a weapon
  slot instead of a vehicle state). Tick "Reset game settings" in the
  launcher - it backs up UserData and wipes it so the client makes
  clean defaults. The launcher auto-detects the known corruption
  marker and pre-checks this for you.
