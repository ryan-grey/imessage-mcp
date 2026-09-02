#!/usr/bin/env python3
"""iMessage, as a local MCP server for the Claude desktop app.

Search, list, read and send Apple Messages on this Mac. Everything is local:
the message database is opened strictly read-only, sending goes through
Messages.app itself via AppleScript, and nothing ever touches the network.

Setup, once:

    python3 -m venv ~/.local/imessage-mcp/.venv
    ~/.local/imessage-mcp/.venv/bin/pip install mcp

then register in claude_desktop_config.json:

    "imessage": {
      "command": "/Users/<you>/.local/imessage-mcp/.venv/bin/python",
      "args": ["/Users/<you>/Documents/imessage-mcp/server.py"]
    }

Two manual macOS grants are required - see README.md:
  1. Full Disk Access for the process that runs this server.
  2. The Automation prompt allowing it to control Messages, on first send.

Privacy, by construction:
  - chat.db is opened with mode=ro&immutable=1: SQLite takes no locks, writes
    nothing, and Messages.app is never blocked.
  - The only writes anywhere are the local FTS index (index.db) and the send
    audit log (sent.log), both in this directory and both gitignored.
  - There are no network calls in this file. None of the imports can make one.
"""

import datetime
import glob
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer

HOME = Path.home()
CHAT_DB = Path(os.environ.get("IMESSAGE_CHAT_DB", HOME / "Library/Messages/chat.db"))
REPO_DIR = Path(__file__).resolve().parent
INDEX_DB = Path(os.environ.get("IMESSAGE_INDEX_DB", REPO_DIR / "index.db"))
SENT_LOG = Path(os.environ.get("IMESSAGE_SENT_LOG", REPO_DIR / "sent.log"))
ADDRESSBOOK_GLOB = str(
    HOME / "Library/Application Support/AddressBook/**/AddressBook-v22.abcddb"
)

# Apple stores message dates as nanoseconds since 2001-01-01 UTC.
APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)

mcp = MCPServer(
    name="imessage",
    instructions=(
        "Local access to Apple Messages on this Mac: search, list chats, read "
        "threads, resolve contacts, and send. Reads are strictly read-only "
        "against chat.db; sends go through Messages.app and require "
        "confirm=true."
    ),
)


# ---------------------------------------------------------------- database --


def _chat_db():
    """A fresh read-only connection per request, so we always see new rows.

    immutable=1 makes SQLite take no locks at all - Messages.app can never be
    blocked by us - at the cost of not tracking changes within a connection,
    which is why connections are never reused.
    """
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("SELECT 1 FROM message LIMIT 1")
    except sqlite3.Error as e:
        con.close()
        raise RuntimeError(
            f"cannot read {CHAT_DB}: {e}. The process running this server "
            "needs Full Disk Access (System Settings > Privacy & Security)."
        )
    return con


def _decode_attributed(blob):
    """Best-effort text out of the typedstream attributedBody blob.

    Modern macOS often leaves message.text NULL and puts the body only in
    attributedBody, an NSArchiver typedstream. Full typedstream parsing is a
    project of its own; the string payload reliably sits five bytes after the
    'NSString' class name, length-prefixed (one byte, or 0x81 + uint16le, or
    0x82 + uint32le). index_status() reports how often this works.
    """
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i == -1:
        return None
    i += len(b"NSString") + 5  # skip \x01\x94\x84\x01\x2b
    if i >= len(blob):
        return None
    n = blob[i]
    i += 1
    if n == 0x81:
        n = int.from_bytes(blob[i : i + 2], "little")
        i += 2
    elif n == 0x82:
        n = int.from_bytes(blob[i : i + 4], "little")
        i += 4
    try:
        return blob[i : i + n].decode("utf-8")
    except UnicodeDecodeError:
        return blob[i : i + n].decode("utf-8", errors="replace")


def _body(text, blob):
    """Message body: decoded attributedBody first, text column as fallback.

    U+FFFC is the attachment placeholder; attachments are reported separately
    so it is stripped here.
    """
    out = _decode_attributed(blob) or text or ""
    return out.replace("￼", "").strip()


def _ts(apple):
    """Apple epoch (ns since 2001, or s in very old rows) -> ISO local time."""
    if not apple:
        return None
    if apple > 10**12:
        apple = apple / 1e9
    return (APPLE_EPOCH + datetime.timedelta(seconds=apple)).astimezone().isoformat(
        timespec="seconds"
    )


def _apple(iso):
    """ISO date/datetime -> Apple epoch nanoseconds. Naive input is local."""
    dt = datetime.datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int((dt - APPLE_EPOCH).total_seconds() * 1e9)


MSG_SELECT = """
SELECT m.ROWID AS rowid, m.date, m.is_from_me, m.text, m.attributedBody,
       m.cache_has_attachments, h.id AS handle,
       c.guid AS chat_guid,
       COALESCE(NULLIF(c.display_name, ''), c.chat_identifier) AS chat_name
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join j ON j.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = j.chat_id
"""


def _attachments(con, rowids):
    """Names and paths only - the files themselves are never opened."""
    if not rowids:
        return {}
    marks = ",".join("?" * len(rowids))
    out = defaultdict(list)
    for r in con.execute(
        f"""SELECT j.message_id, a.transfer_name, a.filename
            FROM message_attachment_join j
            JOIN attachment a ON a.ROWID = j.attachment_id
            WHERE j.message_id IN ({marks})""",
        rowids,
    ):
        out[r["message_id"]].append(
            {"name": r["transfer_name"], "path": r["filename"]}
        )
    return out


def _shape(con, rows):
    att = _attachments(
        con, [r["rowid"] for r in rows if r["cache_has_attachments"]]
    )
    return [
        {
            "rowid": r["rowid"],
            "time": _ts(r["date"]),
            "from_me": bool(r["is_from_me"]),
            "sender": "me" if r["is_from_me"] else (r["handle"] or "unknown"),
            "chat_id": r["chat_guid"],
            "chat_name": r["chat_name"],
            "body": _body(r["text"], r["attributedBody"]),
            "attachments": att.get(r["rowid"], []),
        }
        for r in rows
    ]


# ------------------------------------------------------------------- index --


def _index_con():
    con = sqlite3.connect(INDEX_DB)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS msg(
            rowid INTEGER PRIMARY KEY, body TEXT, date INTEGER, handle TEXT,
            chat_guid TEXT, chat_name TEXT, from_me INTEGER);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts
            USING fts5(body, content='msg', content_rowid='rowid');
        """
    )
    return con


def _refresh_index(ix):
    """Pull rows newer than the last indexed ROWID into the FTS index.

    The first call indexes the whole history, which can take a minute on a
    large chat.db; after that it is incremental and cheap.
    """
    last = ix.execute("SELECT COALESCE(MAX(rowid), 0) FROM msg").fetchone()[0]
    src = _chat_db()
    try:
        rows = src.execute(
            MSG_SELECT + " WHERE m.ROWID > ? GROUP BY m.ROWID ORDER BY m.ROWID",
            (last,),
        ).fetchall()
        for r in rows:
            body = _body(r["text"], r["attributedBody"])
            ix.execute(
                "INSERT OR REPLACE INTO msg VALUES(?,?,?,?,?,?,?)",
                (
                    r["rowid"],
                    body,
                    r["date"],
                    r["handle"],
                    r["chat_guid"],
                    r["chat_name"],
                    r["is_from_me"],
                ),
            )
            ix.execute("INSERT INTO fts(rowid, body) VALUES(?,?)", (r["rowid"], body))
        ix.commit()
        return len(rows)
    finally:
        src.close()


# ------------------------------------------------------------------- tools --


def search_messages(
    query: str,
    contact: str = "",
    chat_id: str = "",
    since: str = "",
    until: str = "",
    limit: int = 50,
) -> str:
    """Full-text search over decoded message bodies, newest first.

    contact narrows to a handle (substring match on phone/email, both
    directions of the conversation); chat_id to one chat by guid or name;
    since/until are ISO dates or datetimes, local time if no zone given.
    """
    ix = _index_con()
    try:
        _refresh_index(ix)
        match = " ".join(
            '"' + t.replace('"', '""') + '"' for t in query.split()
        )
        sql = "SELECT m.* FROM fts JOIN msg m ON m.rowid = fts.rowid WHERE fts MATCH ?"
        args = [match]
        if contact:
            sql += " AND m.handle LIKE ?"
            args.append(f"%{contact}%")
        if chat_id:
            sql += " AND (m.chat_guid = ? OR m.chat_name = ?)"
            args += [chat_id, chat_id]
        if since:
            sql += " AND m.date >= ?"
            args.append(_apple(since))
        if until:
            sql += " AND m.date <= ?"
            args.append(_apple(until))
        sql += " ORDER BY m.date DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        rows = ix.execute(sql, args).fetchall()
        out = [
            {
                "rowid": r["rowid"],
                "time": _ts(r["date"]),
                "from_me": bool(r["from_me"]),
                "sender": "me" if r["from_me"] else (r["handle"] or "unknown"),
                "chat_id": r["chat_guid"],
                "chat_name": r["chat_name"],
                "body": r["body"],
            }
            for r in rows
        ]
        return json.dumps({"count": len(out), "messages": out}, ensure_ascii=False)
    finally:
        ix.close()


def list_chats(limit: int = 30, since: str = "") -> str:
    """Recent chats: name, participants, last-message preview/time, unread."""
    con = _chat_db()
    try:
        sql = """
            SELECT c.ROWID AS rid, c.guid,
                   COALESCE(NULLIF(c.display_name, ''), c.chat_identifier) AS name,
                   MAX(m.date) AS last_date,
                   SUM(CASE WHEN m.is_read = 0 AND m.is_from_me = 0
                       THEN 1 ELSE 0 END) AS unread
            FROM chat c
            JOIN chat_message_join j ON j.chat_id = c.ROWID
            JOIN message m ON m.ROWID = j.message_id
            GROUP BY c.ROWID
        """
        args = []
        if since:
            sql += " HAVING last_date >= ?"
            args.append(_apple(since))
        sql += " ORDER BY last_date DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        out = []
        for ch in con.execute(sql, args).fetchall():
            parts = [
                r["id"]
                for r in con.execute(
                    """SELECT h.id FROM chat_handle_join j
                       JOIN handle h ON h.ROWID = j.handle_id
                       WHERE j.chat_id = ?""",
                    (ch["rid"],),
                )
            ]
            lastrow = con.execute(
                MSG_SELECT + " WHERE j.chat_id = ? ORDER BY m.date DESC LIMIT 1",
                (ch["rid"],),
            ).fetchone()
            out.append(
                {
                    "chat_id": ch["guid"],
                    "name": ch["name"],
                    "participants": parts,
                    "last_time": _ts(ch["last_date"]),
                    "last_preview": (
                        _body(lastrow["text"], lastrow["attributedBody"])[:120]
                        if lastrow
                        else ""
                    ),
                    "unread": ch["unread"] or 0,
                }
            )
        return json.dumps({"count": len(out), "chats": out}, ensure_ascii=False)
    finally:
        con.close()


def get_thread(
    chat_id: str = "",
    handle: str = "",
    since: str = "",
    until: str = "",
    limit: int = 200,
) -> str:
    """Ordered messages for one chat, oldest first within the window.

    Pass chat_id (guid, chat identifier, or display name) for any chat
    including groups, or handle (phone/email) for a 1:1 conversation.
    """
    if not chat_id and not handle:
        return json.dumps({"error": "pass chat_id or handle"})
    con = _chat_db()
    try:
        where, args = [], []
        if chat_id:
            row = con.execute(
                "SELECT ROWID FROM chat WHERE guid = ? OR chat_identifier = ? "
                "OR display_name = ?",
                (chat_id, chat_id, chat_id),
            ).fetchone()
            if row is None:
                return json.dumps({"error": f"no chat matching {chat_id!r}"})
            where.append("j.chat_id = ?")
            args.append(row[0])
        else:
            where.append("h.id = ?")
            args.append(handle)
        if since:
            where.append("m.date >= ?")
            args.append(_apple(since))
        if until:
            where.append("m.date <= ?")
            args.append(_apple(until))
        sql = (
            MSG_SELECT
            + " WHERE "
            + " AND ".join(where)
            + " GROUP BY m.ROWID ORDER BY m.date DESC LIMIT ?"
        )
        args.append(max(1, min(int(limit), 1000)))
        rows = list(reversed(con.execute(sql, args).fetchall()))
        return json.dumps(
            {"count": len(rows), "messages": _shape(con, rows)}, ensure_ascii=False
        )
    finally:
        con.close()


def get_contact_handles(name: str) -> str:
    """Resolve a contact name to phone/email handles via the AddressBook DB.

    Falls back to substring-matching the handles seen in chat.db when the
    AddressBook database is not readable.
    """
    like = f"%{name}%"
    results, readable = [], False
    for db in sorted(glob.glob(ADDRESSBOOK_GLOB, recursive=True)):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT r.ZFIRSTNAME AS first, r.ZLASTNAME AS last,
                       p.ZFULLNUMBER AS value, 'phone' AS kind
                FROM ZABCDRECORD r
                JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                WHERE (COALESCE(r.ZFIRSTNAME,'') || ' ' ||
                       COALESCE(r.ZLASTNAME,'')) LIKE ?
                UNION ALL
                SELECT r.ZFIRSTNAME, r.ZLASTNAME, e.ZADDRESS, 'email'
                FROM ZABCDRECORD r
                JOIN ZABCDEMAILADDRESS e ON e.ZOWNER = r.Z_PK
                WHERE (COALESCE(r.ZFIRSTNAME,'') || ' ' ||
                       COALESCE(r.ZLASTNAME,'')) LIKE ?
                """,
                (like, like),
            ).fetchall()
            readable = True
        except sqlite3.Error:
            continue
        finally:
            try:
                con.close()
            except Exception:
                pass
        for r in rows:
            value = r["value"] or ""
            if r["kind"] == "phone":
                value = re.sub(r"[^\d+]", "", value)
            results.append(
                {
                    "name": " ".join(
                        p for p in (r["first"], r["last"]) if p
                    ),
                    "kind": r["kind"],
                    "handle": value,
                }
            )
    if readable:
        return json.dumps(
            {"source": "addressbook", "handles": results}, ensure_ascii=False
        )
    # Handle-only fallback: whatever chat.db has seen.
    con = _chat_db()
    try:
        rows = con.execute(
            "SELECT DISTINCT id FROM handle WHERE id LIKE ?", (like,)
        ).fetchall()
    finally:
        con.close()
    return json.dumps(
        {
            "source": "chat.db handles (AddressBook not readable)",
            "handles": [{"name": None, "kind": "handle", "handle": r["id"]}
                        for r in rows],
        },
        ensure_ascii=False,
    )


def index_status() -> str:
    """Row counts, newest message time, and attributedBody decode health."""
    con = _chat_db()
    try:
        counts = {
            t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("message", "chat", "handle")
        }
        newest = con.execute("SELECT MAX(date) FROM message").fetchone()[0]
        sample = con.execute(
            "SELECT attributedBody FROM message "
            "WHERE attributedBody IS NOT NULL ORDER BY ROWID DESC LIMIT 25"
        ).fetchall()
        decoded = sum(
            1 for r in sample if _decode_attributed(r["attributedBody"])
        )
    finally:
        con.close()
    status = {
        "chat_db": str(CHAT_DB),
        "counts": counts,
        "newest_message": _ts(newest),
        "attributed_body_decoding": {
            "working": decoded > 0 or not sample,
            "decoded": f"{decoded}/{len(sample)} recent blobs",
        },
    }
    if INDEX_DB.exists():
        ix = _index_con()
        try:
            status["fts_index"] = {
                "path": str(INDEX_DB),
                "rows": ix.execute("SELECT COUNT(*) FROM msg").fetchone()[0],
                "last_rowid": ix.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM msg"
                ).fetchone()[0],
            }
        finally:
            ix.close()
    else:
        status["fts_index"] = "not built yet (first search builds it)"
    return json.dumps(status)


# -------------------------------------------------------------------- send --

_SEND_TO_BUDDY = """
on run argv
    set theTarget to item 1 of argv
    set theText to item 2 of argv
    set thePath to item 3 of argv
    tell application "Messages"
        set theService to 1st account whose service type = iMessage
        set theBuddy to participant theTarget of theService
        if theText is not "" then send theText to theBuddy
        if thePath is not "" then send POSIX file thePath to theBuddy
    end tell
end run
"""

_SEND_TO_CHAT = """
on run argv
    set theTarget to item 1 of argv
    set theText to item 2 of argv
    set thePath to item 3 of argv
    tell application "Messages"
        set theChat to a reference to chat id theTarget
        if theText is not "" then send theText to theChat
        if thePath is not "" then send POSIX file thePath to theChat
    end tell
end run
"""


def _resolve_send_target(to):
    """(guid-or-handle, is_chat). Group chats must go through their guid."""
    if ";" in to:  # already a full chat guid, e.g. iMessage;+;chat123...
        return to, True
    try:
        con = _chat_db()
    except RuntimeError:
        return to, False  # can't check; treat as a plain handle
    try:
        row = con.execute(
            "SELECT guid FROM chat WHERE chat_identifier = ? AND "
            "chat_identifier LIKE 'chat%'",
            (to,),
        ).fetchone()
    finally:
        con.close()
    return (row["guid"], True) if row else (to, False)


def _log_send(target, text, result):
    SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(SENT_LOG, "a", encoding="utf-8") as f:
        stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        f.write(f"{stamp}\t{target}\t{text[:80]!r}\t{result}\n")


def send_message(
    to: str, text: str = "", attachment_path: str = "", confirm: bool = False
) -> str:
    """Send an iMessage via Messages.app. Requires confirm=true.

    to is a handle (phone/email) or an existing chat id (works for group
    chats). Cannot create new group chats or send reactions/tapbacks -
    AppleScript has no way to do either. Every send is appended to sent.log.
    """
    if confirm is not True:
        return json.dumps(
            {
                "sent": False,
                "error": "refused: pass confirm=true to actually send. "
                "Show the user the exact target and text first.",
            }
        )
    if not to or not (text or attachment_path):
        return json.dumps(
            {"sent": False, "error": "need `to` and text or attachment_path"}
        )
    if attachment_path and not os.path.isfile(attachment_path):
        return json.dumps(
            {"sent": False, "error": f"no such file: {attachment_path}"}
        )
    target, is_chat = _resolve_send_target(to)
    script = _SEND_TO_CHAT if is_chat else _SEND_TO_BUDDY
    try:
        proc = subprocess.run(
            ["osascript", "-e", script, target, text, attachment_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok = proc.returncode == 0
        result = "ok" if ok else (proc.stderr.strip() or "osascript failed")
    except subprocess.TimeoutExpired:
        ok, result = False, "osascript timed out after 30s"
    _log_send(target, text, result)
    return json.dumps(
        {"sent": ok, "target": target, "via": "chat" if is_chat else "buddy",
         "result": result}
    )


# Registered here rather than via decorators so the functions above stay
# plain callables for the test suite.
for _fn in (
    search_messages,
    list_chats,
    get_thread,
    get_contact_handles,
    index_status,
    send_message,
):
    mcp.tool()(_fn)


if __name__ == "__main__":
    mcp.run()
