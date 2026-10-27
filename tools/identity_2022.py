"""Install identity: ``Name#tag`` derived from the machine MAC, uid, client path.

RoE-Setup runs once and writes ``config/identity.json``
(``{name, tag, display, uid, client_dir}``) plus the ``config/members.json``
registry ``{uid: display}``. Services read both at launch: a registered uid
shows its chosen name; an unregistered uid MUST get ``None`` here — callers
keep the canned/default name then. Writing a fallback name into every lobby
avatar card stalled clients after login (zero heartbeats); that regression is
why this module never invents names for unknown uids.
"""
import getpass
import hashlib
import json
from pathlib import Path
import re
import uuid

NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,15}')
SEED = 'roe-identity-2022'
# Card buffer fits 16 bytes of name; longer displays get trimmed by fit_name.
CARD_NAME_LIMIT = 16
# Where a chosen name surfaces once services consume identity/members:
# lobby avatar card (0x2ef), room member list, team roster, in-match spawn
# profile. The CHGSKEY session key is wire crypto — not a name surface.


def valid_name(name):
    return isinstance(name, str) and NAME_RE.fullmatch(name) is not None


def machine_seed():
    """Stable per-machine material: the real MAC, else the host name."""
    node = uuid.getnode()
    # getnode() returns a random value (bit 0x01 set in the top octet) when no
    # MAC exists — unstable between calls, so fall back to the host name.
    if node >> 40 & 1:
        import platform
        node = int.from_bytes(
            hashlib.sha256(('host:' + platform.node()).encode('utf-8')).digest()[:6], 'big')
    return node


def machine_tag():
    """Permanent tag 1..999999 (``#000001``-``#999999``) for this machine."""
    node = machine_seed()
    raw = hashlib.sha256((SEED + ':' + hex(node)).encode('ascii')).digest()
    return 1 + int.from_bytes(raw[:3], 'big') % 999999


def identity_for(name, client_dir=''):
    tag = machine_tag()
    return {'name': name, 'tag': f'{tag:06d}', 'display': f'{name}#{tag:06d}',
            'uid': tag, 'client_dir': client_dir}


def member_display(names, uid):
    """Registered display for a uid — by full uid or by the six-digit suffix
    ``uid%1e6`` a guest sees in the lobby. Returns ``None`` when the uid is
    not registered; callers must then keep the built-in canned/default name
    instead of writing a fallback onto the wire."""
    names = names or {}
    found = names.get(str(int(uid))) or names.get(str(int(uid) % 1000000))
    return found if isinstance(found, str) and found else None


def fit_name(display, limit=CARD_NAME_LIMIT):
    """Fits ``Name#tag`` into the card buffer: on overflow trim the base —
    the tag carries the uniqueness, never cut it."""
    if len(display.encode('utf-8')) <= limit:
        return display
    if '#' in display:
        base, _, tag = display.rpartition('#')
        keep = limit - len(tag) - 1
        if keep >= 1:
            return base[:keep] + '#' + tag
    return display[:limit]


def _path(root, name):
    return Path(root) / 'config' / name


def load_identity(root):
    path = _path(root, 'identity.json')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not valid_name(data.get('name')) or not isinstance(data.get('uid'), int):
        return None
    return data


def load_members(root):
    return load_names_file(_path(root, 'members.json'))


def load_names_file(path):
    """``{uid: display}`` registry from a JSON file; missing file -> {}."""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def save_identity(root, identity):
    """Writes identity.json and registers this uid in members.json."""
    config = Path(root) / 'config'
    config.mkdir(parents=True, exist_ok=True)
    (config / 'identity.json').write_text(
        json.dumps(identity, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    members = load_members(root)
    members[str(identity['uid'])] = identity['display']
    (config / 'members.json').write_text(
        json.dumps(members, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return identity


def check_session_key(root):
    """private/candidate-001.key must exist and hold exactly 16 bytes —
    it is the CHGSKEY session key (server-chosen; content is arbitrary)."""
    path = Path(root) / 'private' / 'candidate-001.key'
    try:
        return len(path.read_bytes()) == 16
    except OSError:
        return False


def default_client_dir():
    """Typical RoE install dirs, when identity is not set yet."""
    candidates = [
        Path(r'C:\Program Files (x86)\Steam\steamapps\common\Ring of Elysium'),
        Path(r'C:\Program Files\Steam\steamapps\common\Ring of Elysium'),
        Path(r'D:\Steam\steamapps\common\Ring of Elysium'),
        Path(r'D:\SteamLibrary\steamapps\common\Ring of Elysium'),
        Path(r'E:\Steam\steamapps\common\Ring of Elysium'),
    ]
    for path in candidates:
        if (path / 'Europa_Client.exe').is_file():
            return str(path)
    return ''


def find_client(path):
    """Accepts a dir or the exe path; returns the normalized client dir."""
    if not path:
        return None
    candidate = Path(path.strip().strip('"'))
    if candidate.is_file() and candidate.name.lower() == 'europa_client.exe':
        candidate = candidate.parent
    return str(candidate) if (candidate / 'Europa_Client.exe').is_file() else None


def suggested_name():
    user = getpass.getuser() or ''
    return user if valid_name(user) else 'Player'
