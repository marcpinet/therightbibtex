"""Bibliothèque locale (SQLite) : dossiers de favoris (imbriqués), favoris (le BibTeX
tel qu'enregistré, plus quelques champs lus pour l'affichage et la recherche),
historique des recherches. Les suppressions passent par une corbeille
(deleted_at = jeton d'annulation), purgée au bout de 30 jours ; une copie de la base
est faite chaque jour dans backups/. Les réglages et les clés d'API sont dans
config.json (voir config.py)."""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bibtex, config

DATA_DIR = config.home()
DB_PATH = DATA_DIR / "library.db"
BACKUP_DIR = DATA_DIR / "backups"
BACKUPS_KEPT = 14
# Une seule personne par installation : ses données portent toutes ce numéro (le schéma
# garde user_id, ce qui permettrait plusieurs bibliothèques dans la même base).
LOCAL_UID = 1
MAX_FOLDERS = 500
MAX_FAVORITES = 20000
MAX_BIBTEX_LEN = 100_000
# Couleurs de surligneur proposées pour les dossiers ("" = sans couleur).
FOLDER_COLORS = ("yellow", "pink", "green", "blue", "orange", "violet")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init():
    conn = connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS folders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            parent_id  INTEGER REFERENCES folders(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            color      TEXT NOT NULL DEFAULT '',
            position   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS folders_user ON folders(user_id);
        CREATE TABLE IF NOT EXISTS favorites (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            folder_id    INTEGER REFERENCES folders(id) ON DELETE CASCADE,
            bibtex       TEXT NOT NULL,
            cite_key     TEXT NOT NULL DEFAULT '',
            entry_type   TEXT NOT NULL DEFAULT '',
            title        TEXT NOT NULL DEFAULT '',
            authors      TEXT NOT NULL DEFAULT '',
            year         TEXT NOT NULL DEFAULT '',
            venue        TEXT NOT NULL DEFAULT '',
            doi          TEXT NOT NULL DEFAULT '',
            arxiv        TEXT NOT NULL DEFAULT '',
            note         TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            deleted_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS favorites_user_folder ON favorites(user_id, folder_id);
        CREATE TABLE IF NOT EXISTS history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            query      TEXT NOT NULL,
            kind       TEXT NOT NULL DEFAULT '',
            title      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS history_user ON history(user_id, id);
    """)
    conn.execute("INSERT OR IGNORE INTO users (id, name, created_at) VALUES (?, 'local', ?)", (LOCAL_UID, now()))
    conn.commit()
    conn.close()


def housekeeping():
    """Entretien quotidien : corbeille de plus de 30 jours, copie de la base du jour
    (les 14 dernières sont gardées)."""
    now_dt = datetime.now(timezone.utc)
    limit = (now_dt - timedelta(days=30)).isoformat(timespec="seconds")
    conn = connect()
    conn.execute("DELETE FROM favorites WHERE deleted_at IS NOT NULL AND substr(deleted_at, 1, 25) < ?", (limit,))
    conn.execute("DELETE FROM folders WHERE deleted_at IS NOT NULL AND substr(deleted_at, 1, 25) < ?", (limit,))
    conn.commit()
    conn.close()
    daily = BACKUP_DIR / f"library-{now_dt:%Y-%m-%d}.db"
    if not daily.exists():
        backup(daily)
    for old in sorted(BACKUP_DIR.glob("library-????-??-??.db"))[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)


def backup(dest: Path) -> Path:
    """Copie cohérente de la base (API de sauvegarde SQLite), même en cours d'écriture."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    src = connect()
    out = sqlite3.connect(str(tmp))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    os.replace(tmp, dest)
    return dest


def _token() -> str:
    # jeton de corbeille : horodatage (pour la purge) + aléa (pour l'annulation)
    return now() + "#" + secrets.token_hex(6)


# ----------------------------------------------------------------- dossiers

def list_folders(uid: int) -> dict:
    conn = connect()
    folders = [dict(r) for r in conn.execute(
        "SELECT id, parent_id, name, color, position, created_at FROM folders "
        "WHERE user_id=? AND deleted_at IS NULL ORDER BY position, name COLLATE NOCASE", (uid,))]
    counts = {r["folder_id"]: r["n"] for r in conn.execute(
        "SELECT folder_id, COUNT(*) AS n FROM favorites WHERE user_id=? AND deleted_at IS NULL "
        "GROUP BY folder_id", (uid,))}
    conn.close()
    children: dict = {}
    for f in folders:
        children.setdefault(f["parent_id"], []).append(f["id"])

    def total(fid):
        return counts.get(fid, 0) + sum(total(c) for c in children.get(fid, []))

    for f in folders:
        f["count"] = counts.get(f["id"], 0)
        f["total"] = total(f["id"])
    return {"folders": folders, "inbox": counts.get(None, 0), "all": sum(counts.values())}


def _owned_folder(conn, uid: int, fid) -> bool:
    if fid is None:
        return True
    return bool(conn.execute("SELECT 1 FROM folders WHERE id=? AND user_id=? AND deleted_at IS NULL",
                             (fid, uid)).fetchone())


def _descendants(conn, uid: int, fid: int) -> list[int]:
    out, stack = [], [fid]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack += [r["id"] for r in conn.execute(
            "SELECT id FROM folders WHERE user_id=? AND parent_id=? AND deleted_at IS NULL", (uid, cur))]
    return out


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()[:120]


def create_folder(uid: int, name: str, parent_id=None, color: str | None = None) -> int | str:
    """color None : celle du dossier parent (un sous-dossier garde le surligneur de son parent)."""
    name = clean_name(name)
    if not name:
        return "Folder name is empty"
    if color is not None and color not in ("",) + FOLDER_COLORS:
        return "Unknown color"
    conn = connect()
    try:
        if not _owned_folder(conn, uid, parent_id):
            return "Parent folder not found"
        n = conn.execute("SELECT COUNT(*) FROM folders WHERE user_id=? AND deleted_at IS NULL", (uid,)).fetchone()[0]
        if n >= MAX_FOLDERS:
            return "Too many folders"
        if color is None:
            row = conn.execute("SELECT color FROM folders WHERE id=?", (parent_id,)).fetchone() if parent_id else None
            color = row["color"] if row else ""
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM folders WHERE user_id=? AND "
                           "parent_id IS ?", (uid, parent_id)).fetchone()[0]
        cur = conn.execute("INSERT INTO folders (user_id, parent_id, name, color, position, created_at) "
                           "VALUES (?,?,?,?,?,?)", (uid, parent_id, name, color, pos, now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_folder(uid: int, fid: int, name=None, parent_id="keep", color: str | None = None) -> str | None:
    if color is not None and color not in ("",) + FOLDER_COLORS:
        return "Unknown color"
    conn = connect()
    try:
        if not _owned_folder(conn, uid, fid):
            return "Folder not found"
        if name is not None:
            name = clean_name(name)
            if not name:
                return "Folder name is empty"
            conn.execute("UPDATE folders SET name=? WHERE id=? AND user_id=?", (name, fid, uid))
        if color is not None:
            conn.execute("UPDATE folders SET color=? WHERE id=? AND user_id=?", (color, fid, uid))
        if parent_id != "keep":
            if parent_id is not None:
                if not _owned_folder(conn, uid, parent_id):
                    return "Parent folder not found"
                if parent_id in _descendants(conn, uid, fid):
                    return "Can't move a folder into itself"
            conn.execute("UPDATE folders SET parent_id=? WHERE id=? AND user_id=?", (parent_id, fid, uid))
        conn.commit()
        return None
    finally:
        conn.close()


def delete_folder(uid: int, fid: int) -> str | None:
    """Envoie le dossier, ses sous-dossiers et leurs favoris à la corbeille.
    Renvoie le jeton d'annulation."""
    conn = connect()
    try:
        if not _owned_folder(conn, uid, fid):
            return None
        token = _token()
        ids = _descendants(conn, uid, fid)
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE favorites SET deleted_at=? WHERE user_id=? AND deleted_at IS NULL AND folder_id IN ({marks})",
                     (token, uid, *ids))
        conn.execute(f"UPDATE folders SET deleted_at=? WHERE user_id=? AND id IN ({marks})", (token, uid, *ids))
        conn.commit()
        return token
    finally:
        conn.close()


def undo(uid: int, token: str) -> int:
    conn = connect()
    n = conn.execute("UPDATE folders SET deleted_at=NULL WHERE user_id=? AND deleted_at=?", (uid, token)).rowcount
    n += conn.execute("UPDATE favorites SET deleted_at=NULL WHERE user_id=? AND deleted_at=?", (uid, token)).rowcount
    conn.commit()
    conn.close()
    return n


# ----------------------------------------------------------------- favoris

def _summary_fields(text: str) -> dict:
    e = bibtex.parse_one(text)
    if not e:
        return {"cite_key": "", "entry_type": "", "title": "", "authors": "", "year": "", "venue": "", "doi": "",
                "arxiv": ""}
    s = bibtex.entry_summary(e)
    return {"cite_key": s["key"], "entry_type": s["type"], "title": s["title"][:500],
            "authors": s["authors"][:2000], "year": s["year"], "venue": s["venue"][:500],
            "doi": (s["doi"] or "").lower()[:200], "arxiv": (s["arxiv"] or "")[:40]}


def _fav_row(r) -> dict:
    d = dict(r)
    d.pop("user_id", None)
    d.pop("deleted_at", None)
    return d


def list_favorites(uid: int, folder="all", q: str = "", recursive: bool = True, ids: list[int] | None = None,
                   limit: int | None = None) -> list[dict]:
    conn = connect()
    sql = "SELECT * FROM favorites WHERE user_id=? AND deleted_at IS NULL"
    args: list = [uid]
    if ids is not None:
        sql += f" AND id IN ({','.join('?' * len(ids))})" if ids else " AND 0"
        args += ids
    elif folder == "inbox":
        sql += " AND folder_id IS NULL"
    elif folder != "all":
        fids = _descendants(conn, uid, int(folder)) if recursive else [int(folder)]
        sql += f" AND folder_id IN ({','.join('?' * len(fids))})"
        args += fids
    if q:
        for word in q.split()[:8]:
            sql += " AND (title LIKE ? OR authors LIKE ? OR cite_key LIKE ? OR venue LIKE ? OR year = ? OR doi LIKE ?)"
            like = f"%{word}%"
            args += [like, like, like, like, word, like]
    sql += " ORDER BY updated_at DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [_fav_row(r) for r in conn.execute(sql, args)]
    conn.close()
    return rows


def find_duplicate(conn, uid: int, folder_id, fields: dict) -> int | None:
    conds, args = [], []
    if fields["doi"]:
        conds.append("doi=?")
        args.append(fields["doi"])
    if fields["arxiv"]:
        conds.append("arxiv=?")
        args.append(fields["arxiv"])
    if fields["title"]:
        conds.append("lower(title)=lower(?)")
        args.append(fields["title"])
    if not conds:
        return None
    row = conn.execute(f"SELECT id FROM favorites WHERE user_id=? AND folder_id IS ? AND deleted_at IS NULL "
                       f"AND ({' OR '.join(conds)}) LIMIT 1", (uid, folder_id, *args)).fetchone()
    return row["id"] if row else None


def add_favorite(uid: int, folder_id, text: str, note: str = "", force: bool = False) -> dict:
    text = (text or "").strip()
    if not text or len(text) > MAX_BIBTEX_LEN or not bibtex.parse_one(text):
        return {"error": "Unreadable BibTeX"}
    conn = connect()
    try:
        if not _owned_folder(conn, uid, folder_id):
            return {"error": "Folder not found"}
        n = conn.execute("SELECT COUNT(*) FROM favorites WHERE user_id=? AND deleted_at IS NULL", (uid,)).fetchone()[0]
        if n >= MAX_FAVORITES:
            return {"error": "Too many favorites"}
        fields = _summary_fields(text)
        dup = find_duplicate(conn, uid, folder_id, fields)
        if dup and not force:
            return {"duplicate": dup}
        ts = now()
        cur = conn.execute(
            "INSERT INTO favorites (user_id, folder_id, bibtex, cite_key, entry_type, title, authors, year, venue, doi, "
            "arxiv, note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, folder_id, text, fields["cite_key"], fields["entry_type"], fields["title"], fields["authors"],
             fields["year"], fields["venue"], fields["doi"], fields["arxiv"], str(note or "")[:2000], ts, ts))
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


def update_favorite(uid: int, fav_id: int, text=None, folder_id="keep", note=None) -> str | None:
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM favorites WHERE id=? AND user_id=? AND deleted_at IS NULL",
                           (fav_id, uid)).fetchone()
        if not row:
            return "Favorite not found"
        sets, args = ["updated_at=?"], [now()]
        if text is not None:
            text = text.strip()
            if not text or len(text) > MAX_BIBTEX_LEN or not bibtex.parse_one(text):
                return "Unreadable BibTeX"
            f = _summary_fields(text)
            sets += ["bibtex=?", "cite_key=?", "entry_type=?", "title=?", "authors=?", "year=?", "venue=?",
                     "doi=?", "arxiv=?"]
            args += [text, f["cite_key"], f["entry_type"], f["title"], f["authors"], f["year"], f["venue"],
                     f["doi"], f["arxiv"]]
        if folder_id != "keep":
            if not _owned_folder(conn, uid, folder_id):
                return "Folder not found"
            sets.append("folder_id=?")
            args.append(folder_id)
        if note is not None:
            sets.append("note=?")
            args.append(str(note)[:2000])
        conn.execute(f"UPDATE favorites SET {', '.join(sets)} WHERE id=? AND user_id=?", (*args, fav_id, uid))
        conn.commit()
        return None
    finally:
        conn.close()


def delete_favorites(uid: int, ids: list[int]) -> str | None:
    ids = [int(i) for i in ids][:5000]
    if not ids:
        return None
    token = _token()
    conn = connect()
    conn.execute(f"UPDATE favorites SET deleted_at=? WHERE user_id=? AND deleted_at IS NULL AND id IN "
                 f"({','.join('?' * len(ids))})", (token, uid, *ids))
    conn.commit()
    conn.close()
    return token


def move_favorites(uid: int, ids: list[int], folder_id) -> str | None:
    ids = [int(i) for i in ids][:5000]
    conn = connect()
    try:
        if not _owned_folder(conn, uid, folder_id):
            return "Folder not found"
        conn.execute(f"UPDATE favorites SET folder_id=?, updated_at=? WHERE user_id=? AND deleted_at IS NULL AND id IN "
                     f"({','.join('?' * len(ids))})", (folder_id, now(), uid, *ids))
        conn.commit()
        return None
    finally:
        conn.close()


def _unique_entries(rows: list[dict]) -> list[str]:
    """Entrées triées par clé, clés rendues uniques (suffixes a, b, c...)."""
    used: dict[str, int] = {}
    out = []
    for r in sorted(rows, key=lambda r: (r["cite_key"].lower(), r["id"])):
        text = r["bibtex"].strip()
        key = r["cite_key"]
        if key:
            n = used.get(key.lower(), 0)
            used[key.lower()] = n + 1
            if n:
                text = bibtex.set_key(text, key + chr(ord("a") + (n - 1) % 26) + ("" if n <= 26 else str(n // 26)))
        out.append(text)
    return out


def export_bib(rows: list[dict]) -> str:
    return "\n\n".join(_unique_entries(rows)) + "\n"


def export_csl(rows: list[dict]) -> list[dict]:
    """Mêmes entrées en CSL-JSON (mêmes clés que l'export .bib)."""
    return [bibtex.to_csl(e) for e in (bibtex.parse_one(t) for t in _unique_entries(rows)) if e]


def import_bib(uid: int, folder_id, text: str) -> dict:
    entries = bibtex.parse(text or "")
    added = dup = 0
    for e in entries[:2000]:
        raw = e.raw if e.raw.endswith(("}", ")")) else bibtex.write_entry(e.type, e.key, list(e.fields.items()))
        res = add_favorite(uid, folder_id, raw)
        if res.get("id"):
            added += 1
        elif res.get("duplicate"):
            dup += 1
    return {"added": added, "duplicates": dup, "total": len(entries)}


# ----------------------------------------------------------------- historique

def add_history(uid: int, query: str, kind: str, title: str):
    query = (query or "").strip()[:1000]
    if not query:
        return
    conn = connect()
    conn.execute("DELETE FROM history WHERE user_id=? AND query=?", (uid, query))
    conn.execute("INSERT INTO history (user_id, query, kind, title, created_at) VALUES (?,?,?,?,?)",
                 (uid, query, kind, (title or "")[:300], now()))
    conn.execute("DELETE FROM history WHERE user_id=? AND id NOT IN (SELECT id FROM history WHERE user_id=? "
                 "ORDER BY id DESC LIMIT 50)", (uid, uid))
    conn.commit()
    conn.close()


def list_history(uid: int) -> list[dict]:
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, query, kind, title, created_at FROM history WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,))]
    conn.close()
    return rows


def clear_history(uid: int, hid: int | None = None):
    conn = connect()
    if hid:
        conn.execute("DELETE FROM history WHERE user_id=? AND id=?", (uid, hid))
    else:
        conn.execute("DELETE FROM history WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
