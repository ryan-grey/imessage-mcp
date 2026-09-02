"""Tests against a fully synthetic chat.db. Runs under pytest or plainly:

    python3 tests/test_server.py
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import make_fixture

_TMP = tempfile.mkdtemp(prefix="imessage-mcp-test-")
_FIXTURE = os.path.join(_TMP, "fixture-chat.db")
make_fixture.build(_FIXTURE)
_AB = os.path.join(_TMP, "AddressBook-v22.abcddb")
make_fixture.build_addressbook(_AB)
os.environ["IMESSAGE_CHAT_DB"] = _FIXTURE
os.environ["IMESSAGE_INDEX_DB"] = os.path.join(_TMP, "index.db")
os.environ["IMESSAGE_SENT_LOG"] = os.path.join(_TMP, "sent.log")
os.environ["IMESSAGE_ADDRESSBOOK_GLOB"] = _AB

import server  # noqa: E402  (env must be set before this import)


def test_decode_short_and_long():
    assert server._decode_attributed(
        make_fixture.typedstream_blob("hello there")
    ) == "hello there"
    long = "y" * 5000
    assert server._decode_attributed(make_fixture.typedstream_blob(long)) == long
    assert server._decode_attributed(None) is None
    assert server._decode_attributed(b"garbage with no marker") is None


def test_time_roundtrip():
    ns = make_fixture.apple_ns("2026-08-01T10:00:00+00:00")
    iso = server._ts(ns)
    assert iso.startswith("2026-08-01") or iso.startswith("2026-07-31")
    assert server._apple("2026-08-01T10:00:00+00:00") == ns


def test_search_finds_decoded_bodies():
    out = json.loads(server.search_messages("pizza"))
    assert out["count"] == 2
    bodies = {m["body"] for m in out["messages"]}
    assert "pizza on friday at seven?" in bodies
    assert all(m["body"] for m in out["messages"])  # decoded, not empty
    # newest first
    assert out["messages"][0]["from_me"] is True


def test_search_filters():
    assert json.loads(server.search_messages("pizza", contact="0001111"))["count"] == 2
    assert json.loads(server.search_messages("pizza", contact="0002222"))["count"] == 0
    assert json.loads(server.search_messages("hello", chat_id="Fixture Group"))["count"] == 1
    assert json.loads(
        server.search_messages("pizza", since="2026-08-02T00:00:00+00:00")
    )["count"] == 2
    assert json.loads(
        server.search_messages("pizza", until="2026-08-01T23:59:00+00:00")
    )["count"] == 0


def test_list_chats():
    out = json.loads(server.list_chats())
    assert out["count"] == 2
    byname = {c["name"]: c for c in out["chats"]}
    group = byname["Fixture Group"]
    parts = {p["handle"]: p["name"] for p in group["participants"]}
    assert parts == {
        "+15550002222": None,  # not in the synthetic AddressBook
        "fixture@example.com": "Blake Sample",
    }
    assert group["unread"] == 2
    assert group["last_preview"].startswith("long one:")
    one = byname["Alex Fixture"]  # 1:1 chat renamed from its raw handle
    assert one["chat_id"] == "iMessage;-;+15550001111"
    assert one["unread"] == 0
    assert one["last_time"] is not None


def test_get_thread_by_handle_and_chat():
    out = json.loads(server.get_thread(handle="+15550001111"))
    assert out["count"] == 5
    times = [m["time"] for m in out["messages"]]
    assert times == sorted(times)  # oldest first
    assert out["messages"][0]["body"] == "plain text only, no blob"
    att = out["messages"][-1]
    assert att["attachments"] == [
        {"name": "photo.heic", "path": "/synthetic/path/photo.heic"}
    ]
    assert att["body"] == ""  # U+FFFC placeholder stripped

    grp = json.loads(server.get_thread(chat_id="iMessage;+;chat00000fixture"))
    assert grp["count"] == 2
    assert grp["messages"][0]["sender"] == "+15550002222"

    assert "error" in json.loads(server.get_thread(chat_id="nope"))
    assert "error" in json.loads(server.get_thread())


def test_get_contact_handles():
    out = json.loads(server.get_contact_handles("alex"))
    assert out["source"] == "addressbook"
    assert out["handles"] == [
        {"name": "Alex Fixture", "kind": "phone", "handle": "5550001111"}
    ]
    out = json.loads(server.get_contact_handles("sample"))
    assert out["handles"][0]["handle"] == "Fixture@Example.com"


def test_contact_name_join():
    assert server._norm_handle("+1 (555) 000-1111") == "5550001111"
    assert server._norm_handle("Fixture@Example.COM") == "fixture@example.com"
    assert server._norm_handle("") is None
    assert server._name_for("+15550001111") == "Alex Fixture"
    assert server._name_for("fixture@example.com") == "Blake Sample"
    assert server._name_for("+19998887777") is None
    out = json.loads(server.search_messages("pizza"))
    incoming = [m for m in out["messages"] if not m["from_me"]][0]
    assert incoming["sender_name"] == "Alex Fixture"
    assert incoming["chat_name"] == "Alex Fixture"
    thread = json.loads(server.get_thread(chat_id="iMessage;+;chat00000fixture"))
    names = {m["sender"]: m["sender_name"] for m in thread["messages"]}
    assert names == {"+15550002222": None, "fixture@example.com": "Blake Sample"}


def test_owner_name_on_from_me_rows():
    server._OWNER["at"] = 0.0
    out = json.loads(server.get_thread(handle="+15550001111"))
    mine = [m for m in out["messages"] if m["from_me"]]
    assert mine and all(m["sender_name"] == "Riley Owner" for m in mine)
    theirs = [m for m in out["messages"] if not m["from_me"]]
    assert all(m["sender_name"] == "Alex Fixture" for m in theirs)
    search = json.loads(server.search_messages("pizza"))
    sent = next(m for m in search["messages"] if m["from_me"])
    assert sent["sender_name"] == "Riley Owner"


def test_addressbook_reads_wal_fresh_names():
    """A rename synced from iCloud sits in the AddressBook's WAL until
    Contacts checkpoints. immutable=1 serves the stale checkpointed name;
    the ro-first open must see the new one."""
    ab = os.environ["IMESSAGE_ADDRESSBOOK_GLOB"]
    w = sqlite3.connect(ab)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("UPDATE ZABCDRECORD SET ZLASTNAME='Arrington'"
              " WHERE ZFIRSTNAME='Alex'")
    w.commit()  # committed into the WAL; closing would checkpoint, so don't
    try:
        stale = sqlite3.connect(f"file:{ab}?mode=ro&immutable=1", uri=True)
        old = stale.execute("SELECT ZLASTNAME FROM ZABCDRECORD"
                            " WHERE ZFIRSTNAME='Alex'").fetchone()[0]
        stale.close()
        assert old == "Fixture"  # proves immutable=1 would show the old name
        refreshed = json.loads(server.refresh_contacts())
        assert refreshed["contacts_mapped"] >= 3
        assert refreshed["owner_name"] == "Riley Owner"
        assert server._name_for("+15550001111") == "Alex Arrington"
    finally:
        w.execute("UPDATE ZABCDRECORD SET ZLASTNAME='Fixture'"
                  " WHERE ZFIRSTNAME='Alex'")
        w.commit()
        w.close()
        server._CONTACTS["at"] = 0.0
        server._OWNER["at"] = 0.0


def test_index_status():
    out = json.loads(server.index_status())
    assert out["counts"]["message"] == 7
    assert out["attributed_body_decoding"]["working"] is True
    assert out["newest_message"] is not None


def test_send_refuses_without_confirm():
    out = json.loads(server.send_message("+15550001111", "hi"))
    assert out["sent"] is False and "confirm" in out["error"]
    assert not os.path.exists(os.environ["IMESSAGE_SENT_LOG"])
    out = json.loads(server.send_message("", "hi", confirm=True))
    assert out["sent"] is False
    out = json.loads(
        server.send_message("+15550001111", "hi", attachment_path="/no/file",
                            confirm=True)
    )
    assert out["sent"] is False and "no such file" in out["error"]


def test_send_target_resolution():
    assert server._resolve_send_target("iMessage;+;chatX") == (
        "iMessage;+;chatX", True)
    assert server._resolve_send_target("chat00000fixture") == (
        "iMessage;+;chat00000fixture", True)
    assert server._resolve_send_target("+15550001111") == (
        "+15550001111", False)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e}")
    sys.exit(1 if fails else 0)
