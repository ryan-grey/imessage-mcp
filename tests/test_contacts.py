"""Tests for the Contacts server against a fully synthetic in-memory
adapter - the Contacts.framework layer is swapped out, so no real card is
ever read or written. Runs under pytest or plainly:

    python3 tests/test_contacts.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_TMP = tempfile.mkdtemp(prefix="contacts-mcp-test-")
os.environ["CONTACTS_CHANGES_LOG"] = os.path.join(_TMP, "contacts-changes.log")

import contacts  # noqa: E402


class FakeCN:
    """In-memory stand-in for the Contacts.framework adapter."""

    def __init__(self):
        self._seq = 0
        self.cards = {}       # id -> card dict (raw per-container pieces)
        self.card_container = {}
        self.unified = {}     # unified id -> synthesized union card
        self.links = {}       # unified id -> [piece ids]
        self.containers_list = [
            {"id": "ICLOUD-1", "name": "Card", "type": "cardDAV"},
            {"id": "GOOGLE-1", "name": "Google (rgrey.web@gmail.com)",
             "type": "cardDAV"},
            {"id": "LOCAL-1", "name": "On My Mac", "type": "local"},
        ]
        self.groups_list = []  # {id, name, container_id}
        self.members = {}      # group id -> set of card ids
        self.images = {}       # card id -> photo bytes
        self.me_id = None

    def _new_id(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def seed(self, fields, container_id="ICLOUD-1"):
        return self.create(fields, container_id)

    def seed_linked(self, unified_fields, pieces):
        """A unified contact over per-container pieces, the way
        CNContactStore presents linked cards: the unified id resolves to
        no container. pieces: [(fields, container_id), ...]."""
        pids = [self.create(f, cid) for f, cid in pieces]
        uid = self._new_id("UNIFIED")
        card = {"identifier": uid, **self.BLANK}
        card.update(unified_fields)
        card["identifier"] = uid
        self.unified[uid] = card
        self.links[uid] = pids
        return uid, pids

    # --- adapter interface ---
    def fetch(self, container_id=None, group_id=None, name_query=None,
              unified=True):
        piece_ids = {p for ps in self.links.values() for p in ps}
        if unified:
            cards = [c for c in self.cards.values()
                     if c["identifier"] not in piece_ids]
            cards += list(self.unified.values())
        else:
            cards = list(self.cards.values())
        if group_id:
            cards = [c for c in cards
                     if c["identifier"] in self.members.get(group_id, set())]
        elif container_id:
            cards = [c for c in cards
                     if self.card_container.get(c["identifier"]) == container_id]
        elif name_query:
            q = name_query.lower()
            cards = [c for c in cards
                     if q in contacts._display_name(c).lower()]
        return [dict(c) for c in cards]

    def get(self, identifier):
        c = self.cards.get(identifier) or self.unified.get(identifier)
        return dict(c) if c else None

    def linked_pieces(self, identifier):
        if identifier in self.links:
            pids = self.links[identifier]
        else:
            pids = next((ps for ps in self.links.values()
                         if identifier in ps), [])
        return [dict(self.cards[p]) for p in pids]

    def containers(self):
        return [dict(c) for c in self.containers_list]

    def container_of(self, identifier):
        cid = self.card_container.get(identifier)
        return next((dict(c) for c in self.containers_list if c["id"] == cid),
                    None)

    def groups(self, container_id=None):
        return [
            {"id": g["id"], "name": g["name"]}
            for g in self.groups_list
            if container_id is None or g["container_id"] == container_id
        ]

    def group_member_ids(self, group_id):
        return sorted(self.members.get(group_id, set()))

    BLANK = {"given_name": "", "middle_name": "", "family_name": "",
             "organization": "", "job_title": "", "department": "",
             "nickname": "", "phones": [], "emails": [], "addresses": [],
             "urls": [], "social_profiles": [], "birthday": None,
             "dates": [], "has_image": False, "note": None}

    def create(self, fields, container_id):
        ident = self._new_id("CARD")
        card = {"identifier": ident, **self.BLANK}
        card.update(fields)
        card["identifier"] = ident
        self.cards[ident] = card
        self.card_container[ident] = container_id
        return ident

    def update(self, identifier, fields):
        self.cards[identifier].update(fields)

    def delete(self, identifier):
        del self.cards[identifier]
        del self.card_container[identifier]
        for ids in self.members.values():
            ids.discard(identifier)
        # a link with one remaining piece dissolves, as in the real store
        for uid in list(self.links):
            self.links[uid] = [p for p in self.links[uid] if p != identifier]
            if len(self.links[uid]) < 2:
                del self.links[uid]
                self.unified.pop(uid, None)

    def create_group(self, name, container_id):
        gid = self._new_id("GROUP")
        self.groups_list.append(
            {"id": gid, "name": name, "container_id": container_id})
        self.members[gid] = set()
        return gid

    def add_member(self, identifier, group_id):
        self.members[group_id].add(identifier)

    def remove_member(self, identifier, group_id):
        self.members[group_id].discard(identifier)

    def authorization_status(self):
        return 3  # authorized

    def photo(self, identifier):
        return self.images.get(identifier)

    def set_image(self, identifier, data):
        self.images[identifier] = bytes(data)
        self.cards[identifier]["has_image"] = True

    def me_card(self):
        return dict(self.cards[self.me_id]) if self.me_id else None

    def vcard_export(self, container_id=None):
        cards = self.fetch(container_id=container_id)
        return "".join(
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:"
            + contacts._display_name(c) + "\r\nEND:VCARD\r\n"
            for c in cards
        ).encode("utf-8")


def fresh():
    fake = FakeCN()
    contacts.CN = fake
    contacts._GROUPS["at"] = 0.0  # drop the group-map cache with the fake
    a = fake.seed({"given_name": "Alex", "family_name": "Fixture",
                   "phones": [{"label": "mobile", "value": "+1 (555) 000-1111"}],
                   "emails": [{"label": "home", "value": "alex@example.com"}]})
    b = fake.seed({"given_name": "Alex", "family_name": "Fixture",
                   "phones": [{"label": "work", "value": "5550001111"}],
                   "emails": [{"label": "work", "value": "afx@example.com"}],
                   "organization": "Fixture Co"}, container_id="GOOGLE-1")
    c = fake.seed({"given_name": "Blake", "family_name": "Sample",
                   "emails": [{"label": "home", "value": "blake@example.com"}]})
    gid = fake.create_group("Family", "ICLOUD-1")
    fake.add_member(a, gid)
    return fake, a, b, c, gid


def test_list_and_query():
    fake, a, b, c, gid = fresh()
    out = json.loads(contacts.list_contacts())
    assert out["count"] == 3
    byid = {x["identifier"]: x for x in out["contacts"]}
    assert byid[a]["container"] == "iCloud"
    assert byid[b]["container"] == "Google"
    assert byid[a]["groups"] == ["Family"]
    assert byid[a]["name"] == "Alex Fixture"
    # query by phone fragment, name, org; container and group filters
    assert json.loads(contacts.list_contacts(query="000-1111"))["count"] == 2
    assert json.loads(contacts.list_contacts(query="blake"))["count"] == 1
    assert json.loads(contacts.list_contacts(query="fixture co"))["count"] == 1
    assert json.loads(contacts.list_contacts(container="iCloud"))["count"] == 2
    assert json.loads(contacts.list_contacts(group="Family"))["count"] == 1


def test_get_contact_by_name_and_ambiguity():
    fake, a, b, c, gid = fresh()
    got = json.loads(contacts.get_contact(name="Blake"))
    assert got["identifier"] == c
    ambiguous = json.loads(contacts.get_contact(name="Alex"))
    assert "2 contacts match" in ambiguous["error"]
    assert json.loads(contacts.get_contact(identifier=a))["name"] == "Alex Fixture"


def test_find_duplicates():
    fake, a, b, c, gid = fresh()
    by_phone = json.loads(contacts.find_duplicates("phone"))
    assert by_phone["count"] == 1
    cluster = by_phone["clusters"][0]
    assert {x["identifier"] for x in cluster["cards"]} == {a, b}
    assert "organization" in cluster["differing_fields"]
    assert "emails" in cluster["differing_fields"]
    by_name = json.loads(contacts.find_duplicates("name"))
    assert by_name["count"] == 1
    assert json.loads(contacts.find_duplicates("email"))["count"] == 0
    assert "error" in json.loads(contacts.find_duplicates("bogus"))


def test_writes_refuse_without_confirm():
    fake, a, b, c, gid = fresh()
    log = os.environ["CONTACTS_CHANGES_LOG"]
    size_before = os.path.getsize(log) if os.path.exists(log) else 0
    for call in (
        lambda: contacts.create_contact({"given_name": "X"}),
        lambda: contacts.update_contact(a, {"given_name": "X"}),
        lambda: contacts.delete_contact(a),
        lambda: contacts.create_group("G"),
        lambda: contacts.add_to_group(a, "Family"),
        lambda: contacts.remove_from_group(a, "Family"),
        lambda: contacts.move_to_container(b),
        lambda: contacts.merge_contacts([a, b], keep=a),
    ):
        out = json.loads(call())
        assert out["ok"] is False and "confirm" in out["error"]
    assert len(fake.cards) == 3  # nothing changed
    size_after = os.path.getsize(log) if os.path.exists(log) else 0
    assert size_after == size_before  # refusals are never logged as changes


def test_create_update_delete_and_log():
    fake, a, b, c, gid = fresh()
    out = json.loads(contacts.create_contact(
        {"given_name": "Casey", "phones": [{"label": "mobile",
                                            "value": "555-222-3333"}]},
        confirm=True))
    assert out["ok"] and out["contact"]["container"] == "iCloud"
    new_id = out["contact"]["identifier"]
    out = json.loads(contacts.update_contact(
        new_id, {"organization": "Synthetic LLC"}, confirm=True))
    assert out["contact"]["organization"] == "Synthetic LLC"
    out = json.loads(contacts.delete_contact(new_id, confirm=True))
    assert out["ok"] and new_id not in fake.cards
    lines = [json.loads(l) for l in
             open(os.environ["CONTACTS_CHANGES_LOG"], encoding="utf-8")]
    assert [l["tool"] for l in lines] == [
        "create_contact", "update_contact", "delete_contact"]
    assert lines[1]["before"]["organization"] == ""
    assert lines[1]["after"]["organization"] == "Synthetic LLC"
    assert lines[2]["after"] is None


def test_icloud_resolution_never_google():
    fake, *_ = fresh()
    assert contacts._resolve_container("iCloud")["id"] == "ICLOUD-1"
    assert contacts._resolve_container("google")["id"] == "GOOGLE-1"
    # Two non-Google CardDAV containers and none named 'Card' -> refuse.
    fake.containers_list = [
        {"id": "X1", "name": "Work CardDAV", "type": "cardDAV"},
        {"id": "X2", "name": "Other CardDAV", "type": "cardDAV"},
    ]
    try:
        contacts._resolve_container("iCloud")
        assert False, "expected ambiguity error"
    except RuntimeError as e:
        assert "unambiguously" in str(e)


def test_move_to_container():
    fake, a, b, c, gid = fresh()
    out = json.loads(contacts.move_to_container(b, "iCloud", confirm=True))
    assert out["ok"]
    moved = out["contact"]
    assert moved["container"] == "iCloud"
    assert moved["organization"] == "Fixture Co"
    assert b not in fake.cards  # source deleted
    # moving a card already there is refused
    out = json.loads(contacts.move_to_container(
        moved["identifier"], "iCloud", confirm=True))
    assert out["ok"] is False and "already" in out["error"]


def test_move_preserves_group_membership_in_target():
    fake, a, b, c, gid = fresh()
    ggroup = fake.create_group("Google Friends", "GOOGLE-1")
    fake.add_member(b, ggroup)
    icgroup = fake.create_group("Shared", "ICLOUD-1")
    fake.add_member(b, icgroup)
    out = json.loads(contacts.move_to_container(b, "iCloud", confirm=True))
    assert out["groups_kept"] == ["Shared"]
    assert out["groups_dropped"] == ["Google Friends"]


def test_merge_contacts():
    fake, a, b, c, gid = fresh()
    out = json.loads(contacts.merge_contacts([a, b], keep=a, confirm=True))
    assert out["ok"]
    merged = out["contact"]
    assert merged["identifier"] == a
    # union of phones dedupes the same number in two formats
    assert len(merged["phones"]) == 1
    assert {e["value"] for e in merged["emails"]} == {
        "alex@example.com", "afx@example.com"}
    assert merged["organization"] == "Fixture Co"  # filled from the other card
    assert b not in fake.cards
    assert merged["groups"] == ["Family"]
    # guards
    assert json.loads(contacts.merge_contacts([a], keep=a, confirm=True))["ok"] is False
    assert json.loads(contacts.merge_contacts([a, c], keep="nope",
                                              confirm=True))["ok"] is False


def test_merge_unions_groups():
    fake, a, b, c, gid = fresh()
    g2 = fake.create_group("Colleagues", "GOOGLE-1")
    fake.add_member(b, g2)
    out = json.loads(contacts.merge_contacts([a, b], keep=a, confirm=True))
    assert set(out["contact"]["groups"]) == {"Family", "Colleagues"}


def test_group_tools():
    fake, a, b, c, gid = fresh()
    out = json.loads(contacts.create_group("New Circle", confirm=True))
    assert out["ok"]
    out = json.loads(contacts.add_to_group(c, "New Circle", confirm=True))
    assert out["added_to"] == "New Circle"
    assert json.loads(contacts.get_contact(identifier=c))["groups"] == ["New Circle"]
    out = json.loads(contacts.remove_from_group(c, "New Circle", confirm=True))
    assert out["ok"]
    assert json.loads(contacts.get_contact(identifier=c))["groups"] == []


def with_linked_pair():
    """fresh() plus a unified contact hiding an iCloud piece and a Google
    piece - the shape CNContactStore presents for linked cards."""
    fake, a, b, c, gid = fresh()
    uid, (ic, gg) = fake.seed_linked(
        {"given_name": "Miriam", "family_name": "Grey",
         "emails": [{"label": "home", "value": "mg@example.com"},
                    {"label": "old", "value": "md@example.com"}]},
        [({"given_name": "Miriam", "family_name": "Grey",
           "emails": [{"label": "home", "value": "mg@example.com"}],
           "phones": [{"label": "mobile", "value": "555-333-4444"}]},
          "ICLOUD-1"),
         ({"given_name": "Miriam", "family_name": "Dotson",
           "emails": [{"label": "old", "value": "md@example.com"}]},
          "GOOGLE-1")],
    )
    fake.add_member(uid, gid)  # the unified card carries iCloud groups
    return fake, a, b, c, gid, uid, ic, gg


def test_list_annotates_linked_cards():
    fake, a, b, c, gid, uid, ic, gg = with_linked_pair()
    out = json.loads(contacts.list_contacts())
    assert out["count"] == 4  # 3 plain + 1 unified; pieces stay hidden
    unified = next(x for x in out["contacts"] if x["identifier"] == uid)
    assert unified["container"] == "linked"
    assert {(p["identifier"], p["container"]) for p in unified["linked"]} == {
        (ic, "iCloud"), (gg, "Google")}
    assert unified["groups"] == ["Family"]
    plain = next(x for x in out["contacts"] if x["identifier"] == a)
    assert plain["container"] == "iCloud" and "linked" not in plain


def test_linked_cards_tool():
    fake, a, b, c, gid, uid, ic, gg = with_linked_pair()
    out = json.loads(contacts.linked_cards(identifier=uid))
    assert out["count"] == 2
    byid = {x["identifier"]: x for x in out["cards"]}
    assert byid[ic]["container"]["account"] == "iCloud"
    assert byid[gg]["container"]["account"] == "Google"
    assert "family_name" in out["differing_fields"]
    assert "emails" in out["differing_fields"]
    # a piece identifier resolves to the same pair
    via_piece = json.loads(contacts.linked_cards(identifier=gg))
    assert {x["identifier"] for x in via_piece["cards"]} == {ic, gg}
    # a plain card reports itself, one piece, nothing differing
    solo = json.loads(contacts.linked_cards(identifier=a))
    assert solo["count"] == 1 and solo["differing_fields"] == []


def test_writes_refuse_unified_identifier():
    fake, a, b, c, gid, uid, ic, gg = with_linked_pair()
    for call in (
        lambda: contacts.update_contact(uid, {"given_name": "X"}, confirm=True),
        lambda: contacts.delete_contact(uid, confirm=True),
        lambda: contacts.move_to_container(uid, "iCloud", confirm=True),
        lambda: contacts.add_to_group(uid, "Family", confirm=True),
        lambda: contacts.remove_from_group(uid, "Family", confirm=True),
        lambda: contacts.merge_contacts([uid, a], keep=a, confirm=True),
    ):
        out = json.loads(call())
        assert out["ok"] is False and "linked card" in out["error"]
        assert {(p["identifier"], p["container"]) for p in out["pieces"]} == {
            (ic, "iCloud"), (gg, "Google")}
    assert uid in fake.unified and len(fake.cards) == 5  # nothing changed


def test_merge_linked_pieces_into_icloud():
    fake, a, b, c, gid, uid, ic, gg = with_linked_pair()
    out = json.loads(contacts.merge_contacts([ic, gg], keep=ic, confirm=True))
    assert out["ok"]
    merged = out["contact"]
    assert merged["identifier"] == ic
    assert merged["container"] == "iCloud"
    assert {e["value"] for e in merged["emails"]} == {
        "mg@example.com", "md@example.com"}
    assert merged["family_name"] == "Grey"  # keep's scalar wins
    assert gg not in fake.cards
    assert uid not in fake.unified  # the link dissolved with its 2nd piece


def test_find_duplicates_truncates_long_lists():
    fake, a, b, c, gid = fresh()
    fake.seed({"given_name": "Spam", "family_name": "Call",
               "phones": [{"label": "other", "value": f"555-000-{n:04d}"}
                          for n in range(12)] +
                         [{"label": "mobile", "value": "+1 (555) 000-1111"}]})
    out = json.loads(contacts.find_duplicates("phone"))
    cluster = next(cl for cl in out["clusters"]
                   if cl["matched_on"]["value"] == "5550001111")
    spam = next(x for x in cluster["cards"] if x["given_name"] == "Spam")
    assert len(spam["phones"]) == 6
    assert spam["phones"][-1] == {"label": "truncated", "value": "+8 more"}
    other = next(x for x in cluster["cards"] if x["identifier"] == a)
    assert other["phones"][-1]["value"] != "+8 more"  # short lists untouched


def test_export_includes_linked_pieces():
    fake, a, b, c, gid, uid, ic, gg = with_linked_pair()
    outdir = os.path.join(_TMP, "backups-linked")
    out = json.loads(contacts.export_contacts(path=outdir))
    assert out["count"] == 4
    assert out["containers"] == {"iCloud": 2, "Google": 1, "linked": 1}
    assert out["linked"] == {"cards": 1,
                             "pieces_by_account": {"iCloud": 1, "Google": 1}}
    doc = json.load(open(out["json"], encoding="utf-8"))
    unified = next(x for x in doc["contacts"] if x["identifier"] == uid)
    assert unified["container"] == "linked"
    pieces = {p["identifier"]: p for p in unified["linked_pieces"]}
    assert pieces[ic]["container"]["account"] == "iCloud"
    assert pieces[gg]["container"]["account"] == "Google"
    assert pieces[gg]["family_name"] == "Dotson"


def test_single_piece_unified_resolves_through_its_piece():
    # The real store holds unified ids that differ from their lone
    # underlying card (residue of old links) - seen as 19 null-container
    # cards on 2026-09-02, all single-piece iCloud underneath.
    fake, a, b, c, gid = fresh()
    uid, (pid,) = fake.seed_linked(
        {"given_name": "Janet", "family_name": "Keys"},
        [({"given_name": "Janet", "family_name": "Keys"}, "ICLOUD-1")],
    )
    out = json.loads(contacts.list_contacts(query="janet"))
    card = out["contacts"][0]
    assert card["container"] == "iCloud"
    assert card["piece_identifier"] == pid
    assert "linked" not in card
    exported = json.loads(contacts.export_contacts(
        path=os.path.join(_TMP, "backups-single")))
    doc = json.load(open(exported["json"], encoding="utf-8"))
    unified = next(x for x in doc["contacts"] if x["identifier"] == uid)
    assert unified["container"]["account"] == "iCloud"
    assert unified["piece_identifier"] == pid
    assert exported["containers"] == {"iCloud": 3, "Google": 1}


def test_export_writes_vcf_and_json():
    fake, a, b, c, gid = fresh()
    outdir = os.path.join(_TMP, "backups")
    log = os.environ["CONTACTS_CHANGES_LOG"]
    lines_before = (
        sum(1 for _ in open(log, encoding="utf-8"))
        if os.path.exists(log) else 0
    )
    out = json.loads(contacts.export_contacts(path=outdir))
    assert out["count"] == 3
    assert os.path.isfile(out["vcf"]) and os.path.isfile(out["json"])
    assert out["vcf"].endswith(".vcf") and out["json"].endswith(".json")
    assert out["containers"] == {"iCloud": 2, "Google": 1}
    assert out["bytes"]["vcf"] == os.path.getsize(out["vcf"])
    vcf = open(out["vcf"], encoding="utf-8").read()
    assert vcf.count("BEGIN:VCARD") == 3 and "Alex Fixture" in vcf
    doc = json.load(open(out["json"], encoding="utf-8"))
    assert len(doc["contacts"]) == 3
    byid = {x["identifier"]: x for x in doc["contacts"]}
    assert byid[a]["container"]["account"] == "iCloud"
    assert byid[a]["container"]["id"] == "ICLOUD-1"
    assert byid[a]["groups"] == [{"id": gid, "name": "Family"}]
    assert byid[b]["container"]["account"] == "Google"
    assert byid[b]["groups"] == []
    assert {c["account"] for c in doc["containers"]} >= {"iCloud", "Google"}
    assert doc["groups"][0]["name"] == "Family"
    assert doc["groups"][0]["container_id"] == "ICLOUD-1"
    lines = list(open(log, encoding="utf-8"))
    assert len(lines) == lines_before + 1
    last = json.loads(lines[-1])
    assert last["tool"] == "export_contacts"
    assert last["after"]["count"] == 3
    # container-scoped export
    out = json.loads(contacts.export_contacts(container="Google", path=outdir))
    assert out["count"] == 1 and out["containers"] == {"Google": 1}
    vcf = open(out["vcf"], encoding="utf-8").read()
    assert vcf.count("BEGIN:VCARD") == 1


def test_authorization_status():
    fresh()
    out = json.loads(contacts.authorization_status())
    assert out["status"] == 3 and out["meaning"] == "authorized"
    assert out["python"]


def test_safe_wrapper_surfaces_real_errors():
    def boom():
        raise RuntimeError("CNAuthorizationStatusDenied: grant Contacts")

    out = json.loads(contacts._safe(boom)())
    assert "CNAuthorizationStatusDenied" in out["error"]
    assert out["error"].startswith("RuntimeError:")
    assert json.loads(contacts._safe(lambda: '{"ok": true}')()) == {"ok": True}


def _make_png(w, h):
    import struct
    import zlib

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + b"\x80" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _png_size(data):
    import struct

    return struct.unpack(">II", data[16:24])


def test_extended_fields_roundtrip():
    fake, a, b, c, gid = fresh()
    fields = {
        "given_name": "Robin", "family_name": "Builder",
        "job_title": "AI & Cloud Operations", "nickname": "Rob",
        "department": "Ops",
        "urls": [{"label": "homepage", "value": "https://example.dev"},
                 {"label": "GitHub", "value": "https://github.com/example"}],
        "social_profiles": [{"service": "GitHub", "username": "example",
                             "url": "https://github.com/example"}],
    }
    out = json.loads(contacts.create_contact(fields, confirm=True))
    card = out["contact"]
    assert card["job_title"] == "AI & Cloud Operations"
    assert card["nickname"] == "Rob" and card["department"] == "Ops"
    assert card["urls"][1] == {"label": "GitHub",
                               "value": "https://github.com/example"}
    assert card["social_profiles"][0]["service"] == "GitHub"
    out = json.loads(contacts.update_contact(
        card["identifier"], {"job_title": "Owner"}, confirm=True))
    assert out["contact"]["job_title"] == "Owner"
    exported = json.loads(contacts.export_contacts(
        path=os.path.join(_TMP, "backups-fields")))
    doc = json.load(open(exported["json"], encoding="utf-8"))
    robin = next(x for x in doc["contacts"]
                 if x["identifier"] == card["identifier"])
    assert robin["job_title"] == "Owner"
    assert robin["urls"] and robin["social_profiles"]


def test_merge_unions_urls_and_social_profiles():
    fake, a, b, c, gid = fresh()
    fake.update(a, {"urls": [{"label": "homepage",
                              "value": "https://example.dev"}]})
    fake.update(c, {"urls": [{"label": "work",
                              "value": "HTTPS://EXAMPLE.DEV"},  # dupe, case
                             {"label": "blog",
                              "value": "https://blog.example.dev"}],
                    "social_profiles": [{"service": "GitHub",
                                         "username": "example", "url": ""}]})
    out = json.loads(contacts.merge_contacts([a, c], keep=a, confirm=True))
    merged = out["contact"]
    assert [u["value"] for u in merged["urls"]] == [
        "https://example.dev", "https://blog.example.dev"]
    assert merged["social_profiles"][0]["username"] == "example"


def test_birthday_and_dates_roundtrip():
    fake, a, b, c, gid = fresh()
    # birthday with a year, plus an anniversary and an 'other' date
    out = json.loads(contacts.create_contact(
        {"given_name": "Dana", "family_name": "Dates",
         "birthday": {"year": 1990, "month": 3, "day": 27},
         "dates": [{"label": "anniversary", "year": 2008, "month": 5, "day": 23},
                   {"label": "other", "month": 6, "day": 5}]},
        confirm=True))
    card = out["contact"]
    assert card["birthday"] == {"year": 1990, "month": 3, "day": 27}
    assert card["dates"][0] == {"label": "anniversary", "year": 2008,
                                "month": 5, "day": 23}
    assert card["dates"][1] == {"label": "other", "month": 6, "day": 5}
    # birthday without a year
    out = json.loads(contacts.update_contact(
        card["identifier"], {"birthday": {"month": 12, "day": 7}}, confirm=True))
    assert out["contact"]["birthday"] == {"month": 12, "day": 7}
    got = json.loads(contacts.get_contact(identifier=card["identifier"]))
    assert got["birthday"] == {"month": 12, "day": 7}
    assert len(got["dates"]) == 2
    # every listed card carries both fields, empty or not
    plain = json.loads(contacts.get_contact(identifier=a))
    assert plain["birthday"] is None and plain["dates"] == []
    # clearing
    out = json.loads(contacts.update_contact(
        card["identifier"], {"birthday": None, "dates": []}, confirm=True))
    assert out["contact"]["birthday"] is None and out["contact"]["dates"] == []


def test_merge_unions_dates_and_keeps_birthday():
    fake, a, b, c, gid = fresh()
    fake.update(a, {"birthday": {"month": 3, "day": 27},
                    "dates": [{"label": "anniversary", "month": 5, "day": 23}]})
    fake.update(b, {"birthday": {"year": 1990, "month": 1, "day": 1},
                    "dates": [{"label": "anniversary", "month": 5, "day": 23},
                              {"label": "other", "year": 2007, "month": 6,
                               "day": 5}]})
    fake.update(c, {"birthday": {"month": 9, "day": 9}})
    out = json.loads(contacts.merge_contacts([a, b], keep=a, confirm=True))
    merged = out["contact"]
    assert merged["birthday"] == {"month": 3, "day": 27}  # keep's wins
    assert merged["dates"] == [
        {"label": "anniversary", "month": 5, "day": 23},
        {"label": "other", "year": 2007, "month": 6, "day": 5}]
    # a keep card without a birthday inherits the other's
    fake.update(a, {"birthday": None})
    out = json.loads(contacts.merge_contacts([a, c], keep=a, confirm=True))
    assert out["contact"]["birthday"] == {"month": 9, "day": 9}


def test_export_includes_birthday_and_dates():
    fake, a, b, c, gid = fresh()
    fake.update(a, {"birthday": {"year": 2013, "month": 3, "day": 27},
                    "dates": [{"label": "anniversary", "month": 5, "day": 23}]})
    exported = json.loads(contacts.export_contacts(
        path=os.path.join(_TMP, "backups-dates")))
    doc = json.load(open(exported["json"], encoding="utf-8"))
    byid = {x["identifier"]: x for x in doc["contacts"]}
    assert byid[a]["birthday"] == {"year": 2013, "month": 3, "day": 27}
    assert byid[a]["dates"] == [{"label": "anniversary", "month": 5, "day": 23}]
    assert byid[c]["birthday"] is None and byid[c]["dates"] == []


def test_real_adapter_date_components_and_keys():
    """The framework side of birthday/dates: NSDateComponents round-trips
    with and without a year, the fetch keys include both date keys, and
    date labels map back to Apple's built-ins. No store access."""
    import Contacts as C

    real = contacts._RealCN()
    real._note_available = False
    keys = real._keys(with_note=False)
    assert C.CNContactBirthdayKey in keys and C.CNContactDatesKey in keys

    with_year = real._date_components({"year": 2008, "month": 5, "day": 23})
    assert real._date_dict(with_year) == {"year": 2008, "month": 5, "day": 23}
    no_year = real._date_components({"month": 6, "day": 17})
    assert real._date_dict(no_year) == {"year": None, "month": 6, "day": 17}
    assert real._date_components(None) is None
    assert real._date_dict(None) is None

    mc = C.CNMutableContact.alloc().init()
    real._apply_fields(mc, {
        "birthday": {"month": 6, "day": 17},
        "dates": [{"label": "anniversary", "year": 2008, "month": 5, "day": 23},
                  {"label": "Other", "month": 1, "day": 2},
                  {"label": "graduation", "month": 5, "day": 30}],
    })
    assert real._date_dict(mc.birthday()) == {"year": None, "month": 6, "day": 17}
    labels = [str(lv.label()) for lv in mc.dates()]
    assert labels == [C.CNLabelDateAnniversary, C.CNLabelOther, "graduation"]
    assert [contacts._clean_label(l) for l in labels] == [
        "anniversary", "other", "graduation"]
    real._apply_fields(mc, {"birthday": None, "dates": []})
    assert mc.birthday() is None and list(mc.dates()) == []


def test_set_photo():
    fake, a, b, c, gid = fresh()
    big = os.path.join(_TMP, "big.png")
    with open(big, "wb") as f:
        f.write(_make_png(2048, 512))
    out = json.loads(contacts.set_photo(a, big))
    assert out["ok"] is False and "confirm" in out["error"]
    txt = os.path.join(_TMP, "not-an-image.txt")
    with open(txt, "w") as f:
        f.write("plain text")
    out = json.loads(contacts.set_photo(a, txt, confirm=True))
    assert out["ok"] is False and "not a JPEG or PNG" in out["error"]
    out = json.loads(contacts.set_photo(a, big, confirm=True))
    assert out["ok"] is True
    w, h = _png_size(fake.images[a])
    assert max(w, h) <= 1024 and w > h  # downscaled, aspect kept
    small = os.path.join(_TMP, "small.png")
    with open(small, "wb") as f:
        f.write(_make_png(300, 300))
    out = json.loads(contacts.set_photo(c, small, confirm=True))
    assert out["ok"] and fake.images[c] == open(small, "rb").read()  # untouched
    got = json.loads(contacts.get_contact(identifier=c, include_photo=True))
    assert got["has_image"] is True
    assert os.path.isfile(got["photo_path"])
    assert open(got["photo_path"], "rb").read() == fake.images[c]
    log = [json.loads(l) for l in
           open(os.environ["CONTACTS_CHANGES_LOG"], encoding="utf-8")]
    photo_lines = [l for l in log if l["tool"] == "set_photo"]
    assert photo_lines[-1]["before"] == {"has_image": False}
    assert photo_lines[-1]["after"]["source"] == small


def test_set_image_fetches_the_image_keys():
    """Regression: setting imageData on a contact fetched without
    CNContactImageDataKey throws CNPropertyNotFetchedException. The real
    adapter must request the image keys when fetching for set_image."""
    import Contacts as C

    real = contacts._RealCN()
    real._note_available = False  # skip the store probe in _keys()
    seen = {}

    def capture_piece(identifier, keys=None):
        seen["keys"] = list(keys or [])
        raise RuntimeError("stop before touching the real store")

    real._piece_obj = capture_piece
    try:
        real.set_image("X:ABPerson", b"\x89PNGfake")
    except RuntimeError:
        pass
    assert C.CNContactImageDataKey in seen["keys"]
    assert C.CNContactImageDataAvailableKey in seen["keys"]

    # ordinary writes keep the standard key set - no image key
    def capture_update(identifier, keys=None):
        seen["update_keys"] = list(keys or [])
        raise RuntimeError("stop")

    real._piece_obj = capture_update
    try:
        real.update("X:ABPerson", {"given_name": "Y"})
    except RuntimeError:
        pass
    assert C.CNContactImageDataKey not in seen["update_keys"]


def test_me_card():
    fake, a, b, c, gid = fresh()
    out = json.loads(contacts.me_card())
    assert "no 'me' card" in out["error"]
    fake.me_id = a
    out = json.loads(contacts.me_card())
    assert out["identifier"] == a
    assert out["name"] == "Alex Fixture" and out["container"] == "iCloud"


def test_helpers():
    assert contacts._norm_phone("(423) 946-2258") == "4239462258"
    assert contacts._clean_label("_$!<Mobile>!$_") == "mobile"
    assert contacts._clean_label("custom") == "custom"
    assert contacts._account_label(
        {"id": "x", "name": "Card", "type": "cardDAV"}) == "iCloud"
    assert contacts._account_label(
        {"id": "x", "name": "Google (a@b.c)", "type": "cardDAV"}) == "Google"
    assert contacts._account_label(
        {"id": "x", "name": "On My Mac", "type": "local"}) == "local"
    assert contacts._date_label("anniversary") == "_$!<Anniversary>!$_"
    assert contacts._date_label("Other") == "_$!<Other>!$_"
    assert contacts._date_label("") == "_$!<Other>!$_"
    assert contacts._date_label("graduation") == "graduation"
    assert contacts._norm_date({"label": "x", "month": 5, "day": 23}) == \
        contacts._norm_date({"label": "y", "year": None, "month": 5, "day": 23})


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
