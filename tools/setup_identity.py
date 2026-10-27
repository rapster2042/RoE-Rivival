"""First-run setup: pick an in-game name and the RoE install directory.

Run once (RoE-Setup.exe). Writes ``config/identity.json`` —
``{name, tag, display, uid, client_dir}`` — and registers this uid in
``config/members.json``. The tag derives from the machine MAC, so re-running
on the same PC keeps the same ``#tag`` and the uid stays unique per machine.

The name surfaces on: lobby avatar card, room member list, team roster, and
the in-match spawn profile — services read the registry at launch. The tag
format matches what guests see, so friends can register each other by the
six-digit suffix.
"""
import sys
from pathlib import Path

if getattr(sys, 'frozen', False):
    # Built exe sits at the project root — root is the exe's directory.
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from identity_2022 import (check_session_key, default_client_dir, find_client,
                           identity_for, load_identity, save_identity,
                           suggested_name, valid_name)


def ask(prompt, default=''):
    suffix = f' [{default}]' if default else ''
    value = input(f'{prompt}{suffix}: ').strip()
    return value or default


def choose_name(existing):
    while True:
        name = ask('Your in-game name', existing or suggested_name())
        if valid_name(name):
            return name
        print('  Name must be 1-16 chars: letters, digits, "_" or "-", start with letter/digit.')


def choose_client_dir(existing):
    while True:
        path = ask('Path to your Ring of Elysium install', existing or default_client_dir())
        found = find_client(path)
        if found:
            return found
        print('  Europa_Client.exe not found there. Point to the game folder (or the exe itself).')


def main():
    print('Ring of Elysium - first run setup')
    print('=' * 40)
    existing = load_identity(ROOT)
    if existing:
        print(f'Current identity: {existing["display"]} (uid {existing["uid"]})')
        print('Press Enter on a question to keep the current value.')
        print()

    name = choose_name(existing and existing['name'] or '')
    client_dir = choose_client_dir(existing and existing.get('client_dir') or '')

    identity = save_identity(ROOT, identity_for(name, client_dir))
    print()
    print(f'Identity:   {identity["display"]}')
    print(f'Tag:        #{identity["tag"]} (from this PC\'s MAC - stable across reinstalls)')
    print(f'Client uid: {identity["uid"]}')
    print(f'Game path:  {identity["client_dir"]}')
    print()
    if check_session_key(ROOT):
        print('Session key: private/candidate-001.key OK (16 bytes)')
    else:
        print('WARNING: private/candidate-001.key missing or not 16 bytes -')
        print('         the proxy cannot answer CHGSKEY without it.')
    print()
    print('Saved to config/identity.json. Your name appears on the lobby card,')
    print('the room member list, the team roster and in-match automatically.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nSetup cancelled.')
        raise SystemExit(1)
