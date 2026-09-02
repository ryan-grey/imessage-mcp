"""Build a fully synthetic chat.db fixture.

Every name, number, address and message here is invented. No real chat.db,
no real contact data, ever - the fixture is generated from this code so the
repository never contains message content.
"""

import datetime
import sqlite3

APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)


def apple_ns(iso):
    dt = datetime.datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int((dt - APPLE_EPOCH).total_seconds() * 1e9)


def typedstream_blob(text):
    """A minimal blob shaped the way the decoder expects real ones to be:
    'NSString' class name, the five marker bytes, a length prefix, UTF-8."""
    b = text.encode("utf-8")
    if len(b) < 0x80:
        ln = bytes([len(b)])
    elif len(b) < 0x10000:
        ln = b"\x81" + len(b).to_bytes(2, "little")
    else:
        ln = b"\x82" + len(b).to_bytes(4, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84"
        b"NSMutableAttributedString\x00\x84\x84\x12NSAttributedString"
        b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"
        b"NSString" + b"\x01\x94\x84\x01+" + ln + b + b"\x86\x84"
    )


SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT,
                   display_name TEXT, service_name TEXT);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
                      attributedBody BLOB, handle_id INTEGER, date INTEGER,
                      is_from_me INTEGER DEFAULT 0, is_read INTEGER DEFAULT 1,
                      cache_has_attachments INTEGER DEFAULT 0);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER,
                                message_date INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT,
                         transfer_name TEXT, mime_type TEXT);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""

# (rowid, chat, handle, from_me, iso_date, text_col, blob_text, is_read)
MESSAGES = [
    (1, 1, 1, 0, "2026-08-01T10:00:00", "plain text only, no blob", None, 1),
    (2, 1, 1, 1, "2026-08-01T10:05:00", None, "reply stored only as a blob", 1),
    (3, 1, 1, 0, "2026-08-02T09:00:00", None, "pizza on friday at seven?", 1),
    (4, 1, 1, 1, "2026-08-02T09:01:00", None, "pizza sounds great", 1),
    (5, 2, 2, 0, "2026-08-03T12:00:00", None, "group says hello", 0),
    (6, 2, 3, 0, "2026-08-03T12:01:00", None, "long one: " + "x" * 300, 0),
    (7, 1, 1, 0, "2026-08-04T15:00:00", "￼", "￼", 1),  # attachment-only
]


def build(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO handle VALUES (?,?,?)",
        [
            (1, "+15550001111", "iMessage"),
            (2, "+15550002222", "iMessage"),
            (3, "fixture@example.com", "iMessage"),
        ],
    )
    con.executemany(
        "INSERT INTO chat VALUES (?,?,?,?,?)",
        [
            (1, "iMessage;-;+15550001111", "+15550001111", "", "iMessage"),
            (2, "iMessage;+;chat00000fixture", "chat00000fixture",
             "Fixture Group", "iMessage"),
        ],
    )
    con.executemany(
        "INSERT INTO chat_handle_join VALUES (?,?)",
        [(1, 1), (2, 2), (2, 3)],
    )
    for rowid, chat, handle, from_me, iso, text, blob_text, is_read in MESSAGES:
        blob = typedstream_blob(blob_text) if blob_text else None
        date = apple_ns(iso)
        con.execute(
            "INSERT INTO message (ROWID, guid, text, attributedBody, handle_id,"
            " date, is_from_me, is_read, cache_has_attachments)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (rowid, f"MSG-{rowid}", text, blob, handle, date, from_me, is_read,
             1 if rowid == 7 else 0),
        )
        con.execute(
            "INSERT INTO chat_message_join VALUES (?,?,?)", (chat, rowid, date)
        )
    con.execute(
        "INSERT INTO attachment VALUES (1, '/synthetic/path/photo.heic',"
        " 'photo.heic', 'image/heic')"
    )
    con.execute("INSERT INTO message_attachment_join VALUES (7, 1)")
    con.commit()
    con.close()


if __name__ == "__main__":
    import sys

    build(sys.argv[1])
