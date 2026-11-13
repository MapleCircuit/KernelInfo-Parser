from __future__ import annotations

"""parser/git_ast/identity.py - Contributor identities (pure, no I/O).

- `person_key(name, email)`: the `m_maintainer_person` row a (name, email) pair is stored as — the
  TableEngine's no-duplicate key, normalized by `clean_identity_text` (main._person_key delegates here).
- `clean_name(name)` / `identity_key(name, email)`: one developer under several `(name, email)` aliases,
  the rule of the webapp's Developers directory (webapp/API.md section 4.5, `maintainer_service`).
- `build_identity_index(personas)` / `merge_emails()`: the identity of every alias, name-less aliases
  joining the named identity that owns their address; `refresh_person_identities(db)` stores it in
  m_person_identity (the only function here that does I/O).

Shared by the parser's release statistics step (parser/git_ast/release_stats.py) and the webapp.
"""

import contextlib
from typing import Any, Iterable

from db_engine.spec import gt
from parser.maintainer_ast.maintainer_types import clean_identity_text


def person_key(name: str | None, email: str | None) -> tuple[str, str]:
    """Identity of an m_maintainer_person row: `(name, email)`, both normalized (`clean_identity_text`);
    a name-less person takes its address as the name, else "Unknown"."""
    email = clean_identity_text(email)
    return (clean_identity_text(name) or email or "Unknown", email)


def clean_name(name: str | None) -> str:
    """Display form of a persona name: trims whitespace, surrounding quotes and a leaked 'From:' header prefix."""
    clean = " ".join((name or "").split())
    if clean[:5].casefold() == "from:":
        clean = clean[5:].strip()
    while len(clean) >= 2 and clean[0] == clean[-1] and clean[0] in "\"'":
        clean = clean[1:-1].strip()
    return clean


def identity_key(name: str | None, email: str | None) -> str:
    """Normalized identity of a persona: m_maintainer_person holds one row per (name, email) alias,
    so the same developer appears under several person_ids. Group by case/whitespace-folded name,
    falling back to the email when the name is empty."""
    norm_name = clean_name(name).casefold()
    if norm_name:
        return f"n:{norm_name}"
    return f"e:{(email or '').strip().casefold()}"


def merge_emails(emails: Iterable[str | None]) -> list[str]:
    """Split comma-joined email fields and return the distinct addresses, first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for field in emails:
        for addr in (field or "").split(","):
            addr = addr.strip()
            if addr and addr.casefold() not in seen:
                seen.add(addr.casefold())
                out.append(addr)
    return out


def build_identity_index(personas: Iterable[tuple[int, str | None, str | None]]) -> dict[int, str]:
    """Map each (person_id, name, email) persona to its identity key.

    Personas group by normalized name. A name-less persona joins the named identity that owns its
    address; when several named identities use it (typo'd name, nickname), the one using it on the
    most personas, then the largest, wins. Named identities are never merged with each other by
    email: shared or mistyped addresses would chain distinct people together."""
    pid_to_key: dict[int, str] = {}
    pid_emails: dict[int, list[str]] = {}
    sizes: dict[str, int] = {}
    email_owners: dict[str, dict[str, int]] = {}
    for pid, name, email in personas:
        key = identity_key(name, email)
        pid_to_key[pid] = key
        pid_emails[pid] = [a.casefold() for a in merge_emails([email])]
        sizes[key] = sizes.get(key, 0) + 1
        if key.startswith("n:"):
            for addr in pid_emails[pid]:
                owners = email_owners.setdefault(addr, {})
                owners[key] = owners.get(key, 0) + 1
    for pid, key in pid_to_key.items():
        if not key.startswith("e:"):
            continue
        candidates: dict[str, int] = {}
        for addr in pid_emails[pid]:
            for owner, cnt in email_owners.get(addr, {}).items():
                candidates[owner] = candidates.get(owner, 0) + cnt
        if candidates:
            pid_to_key[pid] = max(candidates, key=lambda k: (candidates[k], sizes[k], k))
    return pid_to_key


# m_person_identity.identity_key width (core/DBLayout.py table 59)
IDENTITY_KEY_LEN = 255


def refresh_person_identities(db: Any) -> int:
    """Store the identity of every m_maintainer_person row in m_person_identity (core/API.md section 4,
    row 59); returns the number of rows written or deleted.

    The name-less owner rule is global (one new alias can move others), so the whole index is computed
    again; only rows whose key changed are written, and rows of persons that no longer exist deleted."""
    if db is None:
        return 0

    def _dec(v: Any) -> str:
        return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else ("" if v is None else str(v))

    rows = db.select_rows("m_maintainer_person", ["person_id", "name", "email"], {"person_id": gt(0)})
    index = build_identity_index((int(pid), _dec(name), _dec(email)) for pid, name, email in rows)
    stored_rows = db.select_rows("m_person_identity", ["person_id", "identity_key"])
    stored = {int(pid): _dec(key) for pid, key in stored_rows}
    changed = [(pid, key[:IDENTITY_KEY_LEN]) for pid, key in index.items() if stored.get(pid) != key[:IDENTITY_KEY_LEN]]
    gone = sorted(pid for pid in stored if pid not in index)
    with db.transaction():
        for i in range(0, len(changed), 5000):
            db.insert_rows(
                "m_person_identity",
                ["person_id", "identity_key"],
                changed[i : i + 5000],
                on_conflict="update",
            )
        if gone:
            db.delete_rows("m_person_identity", {"person_id": gone})
    return len(changed) + len(gone)
