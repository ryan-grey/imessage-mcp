#!/usr/bin/env python3
"""Apple Contacts, as a local MCP server for the Claude desktop app.

Sibling to server.py (iMessage) in the same repo and venv. List, search,
create, update, merge and organize contacts - through Contacts.framework
(CNContactStore) via PyObjC, exactly the way Contacts.app does it. It NEVER
writes to the AddressBook SQLite files directly: framework writes are the
only supported path, they sync through iCloud like edits made in
Contacts.app, and a direct database write could corrupt the store.

Setup (shares the imessage-mcp venv):

    ~/.local/imessage-mcp/.venv/bin/pip install mcp pyobjc-core pyobjc-framework-Contacts

then register in claude_desktop_config.json:

    "contacts": {
      "command": "/Users/<you>/.local/imessage-mcp/.venv/bin/python",
      "args": ["/Users/<you>/Documents/imessage-mcp/contacts.py"]
    }

macOS will prompt for Contacts access for the app that runs this server on
first use (System Settings > Privacy & Security > Contacts).

Safety, by construction:
  - Every write tool refuses unless confirm=true.
  - Every write appends a before/after record to contacts-changes.log in
    this directory (gitignored, local-only).
  - Cards are only ever created in an explicitly resolved container. The
    iCloud container is identified by CardDAV type plus iCloud's container
    signature - never "whatever the default account is" - so a card can
    never silently land in a Google account.
  - Contact notes need Apple's com.apple.developer.contacts.notes
    entitlement; without it (the normal case) the note field reads as
    unavailable and note writes are refused, rather than failing weirdly.
"""

import datetime
import functools
import json
import os
import re
import sys
import threading
from collections import Counter
from pathlib import Path

from mcp.server.mcpserver import MCPServer

REPO_DIR = Path(__file__).resolve().parent
CHANGES_LOG = Path(
    os.environ.get("CONTACTS_CHANGES_LOG", REPO_DIR / "contacts-changes.log")
)
ICLOUD_OVERRIDE = os.environ.get("CONTACTS_ICLOUD_CONTAINER_ID", "")
BACKUP_DIR = Path(
    os.environ.get(
        "CONTACTS_BACKUP_DIR",
        Path.home() / "Documents/Claude/contacts-backups",
    )
)

mcp = MCPServer(
    name="contacts",
    instructions=(
        "Local access to Apple Contacts on this Mac via Contacts.framework: "
        "list, search, deduplicate, create, update, merge, group, and move "
        "cards between accounts. Writes require confirm=true and are logged "
        "locally. Changes sync through iCloud like edits in Contacts.app."
    ),
)


# ------------------------------------------------------------ pure helpers --


def _norm_phone(v):
    digits = re.sub(r"\D", "", v or "")
    return digits[-10:] if len(digits) >= 7 else digits


def _norm_email(v):
    return (v or "").strip().lower()


def _norm_name(v):
    return re.sub(r"\s+", " ", (v or "").strip().casefold())


def _clean_label(label):
    """CNLabeledValue labels look like '_$!<Mobile>!$_' for the built-ins."""
    if not label:
        return ""
    m = re.fullmatch(r"_\$!<(.+)>!\$_", str(label))
    return (m.group(1) if m else str(label)).lower()


def _display_name(card):
    name = " ".join(
        p
        for p in (card.get("given_name"), card.get("middle_name"),
                  card.get("family_name"))
        if p
    )
    return name or card.get("organization") or "(no name)"


def _union_labeled(*lists, norm):
    """Union of [{label, value}] lists, first occurrence wins per value."""
    seen, out = set(), []
    for lst in lists:
        for item in lst or []:
            key = norm(item.get("value", "")) if "value" in item else json.dumps(
                {k: v for k, v in item.items() if k != "label"}, sort_keys=True
            )
            if key and key not in seen:
                seen.add(key)
                out.append(item)
    return out


def _merged_fields(cards, keep_id):
    """The union card: keep's scalars, everything's phones/emails/addresses."""
    keep = next(c for c in cards if c["identifier"] == keep_id)
    others = [c for c in cards if c["identifier"] != keep_id]
    fields = {
        k: keep.get(k) or next(
            (c.get(k) for c in others if c.get(k)), None
        )
        for k in ("given_name", "middle_name", "family_name", "organization")
    }
    fields["phones"] = _union_labeled(
        keep.get("phones"), *[c.get("phones") for c in others], norm=_norm_phone
    )
    fields["emails"] = _union_labeled(
        keep.get("emails"), *[c.get("emails") for c in others], norm=_norm_email
    )
    fields["addresses"] = _union_labeled(
        keep.get("addresses"), *[c.get("addresses") for c in others],
        norm=lambda v: v,
    )
    notes = [c.get("note") for c in cards if c.get("note")]
    if notes:
        fields["note"] = "\n".join(dict.fromkeys(notes))
    return {k: v for k, v in fields.items() if v}


def _account_label(container):
    """Human name for where a card lives: iCloud / Google / local / name."""
    if container is None:
        return None
    name = (container.get("name") or "").lower()
    if container["type"] == "cardDAV" and name == "card":
        return "iCloud"
    if "google" in name or "gmail" in name:
        return "Google"
    if container["type"] == "local":
        return "local"
    return container.get("name") or container["id"]


# --------------------------------------------------- Contacts.framework --


class _RealCN:
    """Every Contacts.framework touch lives behind this adapter, returning
    plain Python data, so the test suite can swap in a fake and never go
    near real cards."""

    _note_available = None

    def _fw(self):
        import Contacts

        return Contacts

    def _store(self):
        C = self._fw()
        status = C.CNContactStore.authorizationStatusForEntityType_(
            C.CNEntityTypeContacts
        )
        store = C.CNContactStore.alloc().init()
        if status == 0:  # not determined - trigger the system prompt
            done = threading.Event()
            result = {}

            def handler(granted, error):
                result["granted"] = bool(granted)
                done.set()

            store.requestAccessForEntityType_completionHandler_(
                C.CNEntityTypeContacts, handler
            )
            done.wait(120)
            status = 3 if result.get("granted") else 2
        if status not in (3, 4):  # authorized / limited
            raise RuntimeError(
                "Contacts access is not granted. Allow it under System "
                "Settings > Privacy & Security > Contacts for the app that "
                "runs this server (the Claude desktop app), then retry."
            )
        return store

    def _keys(self, with_note):
        C = self._fw()
        keys = [
            C.CNContactGivenNameKey,
            C.CNContactMiddleNameKey,
            C.CNContactFamilyNameKey,
            C.CNContactOrganizationNameKey,
            C.CNContactPhoneNumbersKey,
            C.CNContactEmailAddressesKey,
            C.CNContactPostalAddressesKey,
        ]
        if with_note:
            keys.append(C.CNContactNoteKey)
        return keys

    def _shape(self, c):
        C = self._fw()
        card = {
            "identifier": str(c.identifier()),
            "given_name": str(c.givenName() or ""),
            "middle_name": str(c.middleName() or ""),
            "family_name": str(c.familyName() or ""),
            "organization": str(c.organizationName() or ""),
            "phones": [
                {
                    "label": _clean_label(lv.label()),
                    "value": str(lv.value().stringValue()),
                }
                for lv in c.phoneNumbers()
            ],
            "emails": [
                {"label": _clean_label(lv.label()), "value": str(lv.value())}
                for lv in c.emailAddresses()
            ],
            "addresses": [
                {
                    "label": _clean_label(lv.label()),
                    "street": str(lv.value().street() or ""),
                    "city": str(lv.value().city() or ""),
                    "state": str(lv.value().state() or ""),
                    "postal_code": str(lv.value().postalCode() or ""),
                    "country": str(lv.value().country() or ""),
                }
                for lv in c.postalAddresses()
            ],
        }
        card["note"] = (
            str(c.note() or "")
            if c.isKeyAvailable_(C.CNContactNoteKey)
            else None  # needs Apple's contacts.notes entitlement
        )
        return card

    def _with_note(self):
        """Whether fetches including the note key work (entitlement)."""
        if self._note_available is None:
            C = self._fw()
            store = self._store()
            req = C.CNContactFetchRequest.alloc().initWithKeysToFetch_(
                self._keys(with_note=True)
            )
            ok, _err = store.enumerateContactsWithFetchRequest_error_usingBlock_(
                req, None, lambda c, stop: None
            )
            self._note_available = bool(ok)
        return self._note_available

    def fetch(self, container_id=None, group_id=None, name_query=None):
        C = self._fw()
        store = self._store()
        keys = self._keys(with_note=self._with_note())
        if group_id:
            pred = C.CNContact.predicateForContactsInGroupWithIdentifier_(group_id)
        elif container_id:
            pred = C.CNContact.predicateForContactsInContainerWithIdentifier_(
                container_id
            )
        elif name_query:
            pred = C.CNContact.predicateForContactsMatchingName_(name_query)
        else:
            out = []
            req = C.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
            store.enumerateContactsWithFetchRequest_error_usingBlock_(
                req, None, lambda c, stop: out.append(self._shape(c))
            )
            return out
        found, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            pred, keys, None
        )
        if err is not None:
            raise RuntimeError(str(err))
        return [self._shape(c) for c in found]

    def get(self, identifier):
        C = self._fw()
        store = self._store()
        c, err = store.unifiedContactWithIdentifier_keysToFetch_error_(
            identifier, self._keys(with_note=self._with_note()), None
        )
        return self._shape(c) if c is not None else None

    def _raw(self, identifier):
        """The mutable CNContact behind an identifier, for save requests."""
        C = self._fw()
        store = self._store()
        c, err = store.unifiedContactWithIdentifier_keysToFetch_error_(
            identifier, self._keys(with_note=self._with_note()), None
        )
        if c is None:
            raise RuntimeError(f"no contact with identifier {identifier!r}")
        return c.mutableCopy()

    def containers(self):
        C = self._fw()
        store = self._store()
        types = {0: "unassigned", 1: "local", 2: "exchange", 3: "cardDAV"}
        found, err = store.containersMatchingPredicate_error_(None, None)
        return [
            {
                "id": str(c.identifier()),
                "name": str(c.name() or ""),
                "type": types.get(int(c.type()), str(c.type())),
            }
            for c in found or []
        ]

    def container_of(self, identifier):
        C = self._fw()
        store = self._store()
        pred = C.CNContainer.predicateForContainerOfContactWithIdentifier_(
            identifier
        )
        found, err = store.containersMatchingPredicate_error_(pred, None)
        if not found:
            return None
        types = {0: "unassigned", 1: "local", 2: "exchange", 3: "cardDAV"}
        c = found[0]
        return {
            "id": str(c.identifier()),
            "name": str(c.name() or ""),
            "type": types.get(int(c.type()), str(c.type())),
        }

    def groups(self, container_id=None):
        C = self._fw()
        store = self._store()
        pred = (
            C.CNGroup.predicateForGroupsInContainerWithIdentifier_(container_id)
            if container_id
            else None
        )
        found, err = store.groupsMatchingPredicate_error_(pred, None)
        return [
            {"id": str(g.identifier()), "name": str(g.name() or "")}
            for g in found or []
        ]

    def group_member_ids(self, group_id):
        C = self._fw()
        store = self._store()
        pred = C.CNContact.predicateForContactsInGroupWithIdentifier_(group_id)
        found, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            pred, [C.CNContactIdentifierKey], None
        )
        return [str(c.identifier()) for c in found or []]

    def _apply_fields(self, mc, fields):
        C = self._fw()
        scalars = {
            "given_name": mc.setGivenName_,
            "middle_name": mc.setMiddleName_,
            "family_name": mc.setFamilyName_,
            "organization": mc.setOrganizationName_,
        }
        for key, setter in scalars.items():
            if key in fields:
                setter(fields[key] or "")
        if "note" in fields:
            if not self._with_note():
                raise RuntimeError(
                    "cannot write notes: this process lacks Apple's "
                    "com.apple.developer.contacts.notes entitlement"
                )
            mc.setNote_(fields["note"] or "")
        if "phones" in fields:
            mc.setPhoneNumbers_(
                [
                    C.CNLabeledValue.labeledValueWithLabel_value_(
                        p.get("label") or C.CNLabelPhoneNumberMobile,
                        C.CNPhoneNumber.phoneNumberWithStringValue_(p["value"]),
                    )
                    for p in fields["phones"]
                ]
            )
        if "emails" in fields:
            mc.setEmailAddresses_(
                [
                    C.CNLabeledValue.labeledValueWithLabel_value_(
                        e.get("label") or C.CNLabelHome, e["value"]
                    )
                    for e in fields["emails"]
                ]
            )
        if "addresses" in fields:
            vals = []
            for a in fields["addresses"]:
                pa = C.CNMutablePostalAddress.alloc().init()
                pa.setStreet_(a.get("street", ""))
                pa.setCity_(a.get("city", ""))
                pa.setState_(a.get("state", ""))
                pa.setPostalCode_(a.get("postal_code", ""))
                pa.setCountry_(a.get("country", ""))
                vals.append(
                    C.CNLabeledValue.labeledValueWithLabel_value_(
                        a.get("label") or C.CNLabelHome, pa
                    )
                )
            mc.setPostalAddresses_(vals)

    def _execute(self, req):
        store = self._store()
        ok, err = store.executeSaveRequest_error_(req, None)
        if not ok:
            raise RuntimeError(f"save failed: {err}")

    def create(self, fields, container_id):
        C = self._fw()
        mc = C.CNMutableContact.alloc().init()
        self._apply_fields(mc, fields)
        req = C.CNSaveRequest.alloc().init()
        req.addContact_toContainerWithIdentifier_(mc, container_id)
        self._execute(req)
        return str(mc.identifier())

    def update(self, identifier, fields):
        C = self._fw()
        mc = self._raw(identifier)
        self._apply_fields(mc, fields)
        req = C.CNSaveRequest.alloc().init()
        req.updateContact_(mc)
        self._execute(req)

    def delete(self, identifier):
        C = self._fw()
        mc = self._raw(identifier)
        req = C.CNSaveRequest.alloc().init()
        req.deleteContact_(mc)
        self._execute(req)

    def create_group(self, name, container_id):
        C = self._fw()
        g = C.CNMutableGroup.alloc().init()
        g.setName_(name)
        req = C.CNSaveRequest.alloc().init()
        req.addGroup_toContainerWithIdentifier_(g, container_id)
        self._execute(req)
        return str(g.identifier())

    def add_member(self, identifier, group_id):
        self._member_op(identifier, group_id, add=True)

    def remove_member(self, identifier, group_id):
        self._member_op(identifier, group_id, add=False)

    def _member_op(self, identifier, group_id, add):
        C = self._fw()
        store = self._store()
        pred = C.CNGroup.predicateForGroupsWithIdentifiers_([group_id])
        found, err = store.groupsMatchingPredicate_error_(pred, None)
        if not found:
            raise RuntimeError(f"no group with identifier {group_id!r}")
        group = found[0]
        mc = self._raw(identifier)
        req = C.CNSaveRequest.alloc().init()
        if add:
            req.addMember_toGroup_(mc, group)
        else:
            req.removeMember_fromGroup_(mc, group)
        self._execute(req)

    def authorization_status(self):
        """Raw CNContactStore authorization status. Never triggers the
        system prompt - it only reads the current TCC state."""
        C = self._fw()
        return int(
            C.CNContactStore.authorizationStatusForEntityType_(
                C.CNEntityTypeContacts
            )
        )

    def vcard_export(self, container_id=None):
        """Every card (optionally one container) as Apple-serialized vCard
        bytes, photos included."""
        C = self._fw()
        store = self._store()
        keys = [
            C.CNContactVCardSerialization.descriptorForRequiredKeys(),
            C.CNContactImageDataKey,
        ]
        if container_id:
            pred = C.CNContact.predicateForContactsInContainerWithIdentifier_(
                container_id
            )
            found, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
                pred, keys, None
            )
            if err is not None:
                raise RuntimeError(str(err))
        else:
            found = []
            req = C.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
            store.enumerateContactsWithFetchRequest_error_usingBlock_(
                req, None, lambda c, stop: found.append(c)
            )
        data, err = C.CNContactVCardSerialization.dataWithContacts_error_(
            list(found or []), None
        )
        if data is None:
            raise RuntimeError(f"vCard serialization failed: {err}")
        return bytes(data)


CN = _RealCN()  # tests replace this with a fake adapter


# ----------------------------------------------------------- shared logic --


def _resolve_container(ref):
    """A container from an id, a name, or an account label like 'iCloud'.

    iCloud is resolved structurally - the CardDAV container Apple names
    'Card' - never as 'the default account', so an explicit iCloud target
    can never land a card in Google. CONTACTS_ICLOUD_CONTAINER_ID overrides
    the heuristic if a setup ever needs it.
    """
    containers = CN.containers()
    if ref.lower() == "icloud":
        if ICLOUD_OVERRIDE:
            match = [c for c in containers if c["id"] == ICLOUD_OVERRIDE]
            if match:
                return match[0]
        icloud = [c for c in containers if _account_label(c) == "iCloud"]
        if len(icloud) == 1:
            return icloud[0]
        carddav = [c for c in containers if c["type"] == "cardDAV"]
        non_google = [c for c in carddav if _account_label(c) != "Google"]
        if len(non_google) == 1:
            return non_google[0]
        raise RuntimeError(
            "cannot identify the iCloud container unambiguously; candidates: "
            + json.dumps(containers)
            + ". Set CONTACTS_ICLOUD_CONTAINER_ID to pin it."
        )
    for c in containers:
        if ref in (c["id"], c["name"]) or ref.lower() == _account_label(c).lower():
            return c
    raise RuntimeError(f"no container matching {ref!r}; have {json.dumps(containers)}")


def _resolve_group(ref, container_id=None):
    groups = CN.groups(container_id)
    for g in groups:
        if ref in (g["id"], g["name"]):
            return g
    raise RuntimeError(f"no group matching {ref!r}; have {json.dumps(groups)}")


def _resolve_contact(identifier="", name=""):
    if identifier:
        card = CN.get(identifier)
        if card is None:
            raise RuntimeError(f"no contact with identifier {identifier!r}")
        return card
    matches = CN.fetch(name_query=name)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(f"no contact matching {name!r}")
    raise RuntimeError(
        f"{len(matches)} contacts match {name!r}; pass an identifier: "
        + json.dumps(
            [{"identifier": m["identifier"], "name": _display_name(m)}
             for m in matches]
        )
    )


def _annotate(card):
    """Add display name, container label, and group names to a card."""
    card = dict(card)
    card["name"] = _display_name(card)
    container = CN.container_of(card["identifier"])
    card["container"] = _account_label(container)
    card["groups"] = [
        g["name"]
        for g in CN.groups()
        if card["identifier"] in CN.group_member_ids(g["id"])
    ]
    return card


def _log_change(tool, target, before, after, result):
    CHANGES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CHANGES_LOG, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "time": datetime.datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                    "tool": tool,
                    "target": target,
                    "before": before,
                    "after": after,
                    "result": result,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def _refuse(tool):
    return json.dumps(
        {
            "ok": False,
            "error": f"refused: {tool} requires confirm=true. Show the user "
            "exactly what will change first.",
        }
    )


# ------------------------------------------------------------- read tools --


def list_contacts(
    query: str = "", container: str = "", group: str = "", limit: int = 100
) -> str:
    """Contacts with names, org, phones, emails, addresses, notes, account
    (iCloud/Google/local), and group memberships. query matches name, org,
    phone, or email substrings; container and group filter by account/group
    name or id."""
    container_id = _resolve_container(container)["id"] if container else None
    group_id = _resolve_group(group)["id"] if group else None
    cards = CN.fetch(container_id=container_id, group_id=group_id)
    if query:
        q = query.lower()
        qp = _norm_phone(query)

        def hit(c):
            hay = " ".join(
                [c.get("given_name", ""), c.get("middle_name", ""),
                 c.get("family_name", ""), c.get("organization", "")]
                + [e["value"] for e in c.get("emails", [])]
            ).lower()
            phones = [_norm_phone(p["value"]) for p in c.get("phones", [])]
            return q in hay or (qp and any(qp in p for p in phones))

        cards = [c for c in cards if hit(c)]
    cards = cards[: max(1, min(int(limit), 1000))]
    return json.dumps(
        {"count": len(cards), "contacts": [_annotate(c) for c in cards]},
        ensure_ascii=False,
    )


def get_contact(identifier: str = "", name: str = "") -> str:
    """One contact by identifier or (unambiguous) name, fully annotated."""
    try:
        card = _resolve_contact(identifier, name)
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    return json.dumps(_annotate(card), ensure_ascii=False)


def find_duplicates(strategy: str = "phone") -> str:
    """Duplicate clusters by shared phone, email, or normalized name.
    Each cluster lists its cards and which fields differ between them."""
    keyers = {
        "phone": lambda c: [_norm_phone(p["value"]) for p in c.get("phones", [])],
        "email": lambda c: [_norm_email(e["value"]) for e in c.get("emails", [])],
        "name": lambda c: [_norm_name(_display_name(c))] if _display_name(c) != "(no name)" else [],
    }
    if strategy not in keyers:
        return json.dumps({"error": f"strategy must be one of {list(keyers)}"})
    cards = CN.fetch()
    buckets = {}
    for c in cards:
        for key in set(keyers[strategy](c)):
            if key:
                buckets.setdefault(key, []).append(c)
    clusters, seen = [], set()
    for key, members in buckets.items():
        ids = tuple(sorted(m["identifier"] for m in members))
        if len(members) < 2 or ids in seen:
            continue
        seen.add(ids)
        differing = [
            field
            for field in ("given_name", "family_name", "organization",
                          "phones", "emails", "addresses", "note")
            if len({json.dumps(m.get(field), sort_keys=True) for m in members}) > 1
        ]
        clusters.append(
            {
                "matched_on": {"strategy": strategy, "value": key},
                "differing_fields": differing,
                "cards": [_annotate(m) for m in members],
            }
        )
    return json.dumps(
        {"count": len(clusters), "clusters": clusters}, ensure_ascii=False
    )


def authorization_status() -> str:
    """The raw Contacts authorization state, to tell a TCC denial apart
    from a code bug. Never triggers the permission prompt."""
    labels = {0: "notDetermined", 1: "restricted", 2: "denied",
              3: "authorized", 4: "limited"}
    status = CN.authorization_status()
    return json.dumps(
        {
            "status": status,
            "meaning": labels.get(status, f"unknown({status})"),
            "python": sys.executable,
            "note": "macOS attributes the grant to the app that launches "
            "this server - the Claude desktop app when spawned from "
            "claude_desktop_config.json, or your terminal app when run by "
            "hand. Grant lives under System Settings > Privacy & Security "
            "> Contacts.",
        }
    )


def export_contacts(container: str = "", path: str = "") -> str:
    """Full restorable backup of Contacts: an Apple-serialized vCard 3.0
    file (importable by Contacts.app/iCloud.com, photos included) plus a
    JSON file carrying what vCard drops - each card's container and group
    memberships, and the full container/group lists. Read-only; files land
    in ~/Documents/Claude/contacts-backups/ (override the parent with
    path). Empty container exports everything."""
    target = _resolve_container(container) if container else None
    cid = target["id"] if target else None
    cards = CN.fetch(container_id=cid)

    containers = [
        {**c, "account": _account_label(c)} for c in CN.containers()
    ]
    groups = []
    membership = {}
    for c in containers:
        for g in CN.groups(c["id"]):
            g = {**g, "container_id": c["id"], "account": c["account"]}
            groups.append(g)
            membership[g["id"]] = set(CN.group_member_ids(g["id"]))

    def _card_doc(card):
        holder = CN.container_of(card["identifier"])
        return {
            **card,
            "name": _display_name(card),
            "container": (
                {**holder, "account": _account_label(holder)} if holder else None
            ),
            "groups": [
                {"id": g["id"], "name": g["name"]}
                for g in groups
                if card["identifier"] in membership[g["id"]]
            ],
        }

    docs = [_card_doc(c) for c in cards]
    counts = Counter(
        (d["container"] or {}).get("account") or "unknown" for d in docs
    )

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    parent = Path(path) if path else BACKUP_DIR
    parent.mkdir(parents=True, exist_ok=True)
    vcf_path = parent / f"contacts-{stamp}.vcf"
    json_path = parent / f"contacts-{stamp}.json"

    vcf_path.write_bytes(CN.vcard_export(cid))
    json_path.write_text(
        json.dumps(
            {
                "exported_at": datetime.datetime.now()
                .astimezone()
                .isoformat(timespec="seconds"),
                "scope": _account_label(target) if target else "all containers",
                "containers": containers,
                "groups": groups,
                "contacts": docs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    result = {
        "count": len(docs),
        "vcf": str(vcf_path),
        "json": str(json_path),
        "containers": dict(counts),
        "bytes": {
            "vcf": vcf_path.stat().st_size,
            "json": json_path.stat().st_size,
        },
    }
    _log_change("export_contacts", str(parent), None,
                {k: result[k] for k in ("count", "vcf", "json")}, "ok")
    return json.dumps(result, ensure_ascii=False)


# ------------------------------------------------------------ write tools --


def create_contact(fields: dict, container: str = "iCloud",
                   confirm: bool = False) -> str:
    """Create a card. fields: given_name, family_name, organization, note,
    phones/emails as [{label, value}], addresses as [{label, street, city,
    state, postal_code, country}]. Requires confirm=true."""
    if confirm is not True:
        return _refuse("create_contact")
    target = _resolve_container(container)
    new_id = CN.create(fields, target["id"])
    after = _annotate(CN.get(new_id))
    _log_change("create_contact", new_id, None, after, "ok")
    return json.dumps({"ok": True, "contact": after}, ensure_ascii=False)


def update_contact(identifier: str, fields: dict, confirm: bool = False) -> str:
    """Replace the given fields on a card (same shapes as create_contact).
    Requires confirm=true."""
    if confirm is not True:
        return _refuse("update_contact")
    before = CN.get(identifier)
    if before is None:
        return json.dumps({"ok": False, "error": f"no contact {identifier!r}"})
    CN.update(identifier, fields)
    after = _annotate(CN.get(identifier))
    _log_change("update_contact", identifier, before, after, "ok")
    return json.dumps({"ok": True, "contact": after}, ensure_ascii=False)


def delete_contact(identifier: str, confirm: bool = False) -> str:
    """Delete a card permanently. Requires confirm=true."""
    if confirm is not True:
        return _refuse("delete_contact")
    before = CN.get(identifier)
    if before is None:
        return json.dumps({"ok": False, "error": f"no contact {identifier!r}"})
    CN.delete(identifier)
    _log_change("delete_contact", identifier, before, None, "ok")
    return json.dumps({"ok": True, "deleted": _display_name(before)})


def create_group(name: str, container: str = "iCloud",
                 confirm: bool = False) -> str:
    """Create a contact group in a container. Requires confirm=true."""
    if confirm is not True:
        return _refuse("create_group")
    target = _resolve_container(container)
    gid = CN.create_group(name, target["id"])
    _log_change("create_group", gid, None, {"name": name,
                "container": _account_label(target)}, "ok")
    return json.dumps({"ok": True, "group": {"id": gid, "name": name}})


def add_to_group(identifier: str, group: str, confirm: bool = False) -> str:
    """Add a contact to a group (by group name or id). Requires confirm=true."""
    if confirm is not True:
        return _refuse("add_to_group")
    g = _resolve_group(group)
    CN.add_member(identifier, g["id"])
    _log_change("add_to_group", identifier, None, {"group": g["name"]}, "ok")
    return json.dumps({"ok": True, "added_to": g["name"]})


def remove_from_group(identifier: str, group: str, confirm: bool = False) -> str:
    """Remove a contact from a group. Requires confirm=true."""
    if confirm is not True:
        return _refuse("remove_from_group")
    g = _resolve_group(group)
    CN.remove_member(identifier, g["id"])
    _log_change("remove_from_group", identifier, {"group": g["name"]}, None, "ok")
    return json.dumps({"ok": True, "removed_from": g["name"]})


def move_to_container(identifier: str, container: str = "iCloud",
                      confirm: bool = False) -> str:
    """Move a card to another account: copy every field into a new card in
    the target container, re-add group memberships for groups that live in
    the target, then delete the source card. Requires confirm=true."""
    if confirm is not True:
        return _refuse("move_to_container")
    target = _resolve_container(container)
    before = CN.get(identifier)
    if before is None:
        return json.dumps({"ok": False, "error": f"no contact {identifier!r}"})
    source = CN.container_of(identifier)
    if source and source["id"] == target["id"]:
        return json.dumps({"ok": False, "error": "card is already in "
                           + _account_label(target)})
    old_groups = [
        g for g in CN.groups()
        if identifier in CN.group_member_ids(g["id"])
    ]
    fields = {
        k: v
        for k, v in before.items()
        if k in ("given_name", "middle_name", "family_name", "organization",
                 "phones", "emails", "addresses") and v
    }
    if before.get("note"):
        fields["note"] = before["note"]
    new_id = CN.create(fields, target["id"])
    target_groups = {g["id"] for g in CN.groups(target["id"])}
    kept, dropped = [], []
    for g in old_groups:
        if g["id"] in target_groups:
            CN.add_member(new_id, g["id"])
            kept.append(g["name"])
        else:
            dropped.append(g["name"])
    CN.delete(identifier)
    after = _annotate(CN.get(new_id))
    _log_change("move_to_container", identifier, before, after, "ok")
    return json.dumps(
        {
            "ok": True,
            "contact": after,
            "groups_kept": kept,
            "groups_dropped": dropped or None,
        },
        ensure_ascii=False,
    )


def merge_contacts(identifiers: list, keep: str, confirm: bool = False) -> str:
    """Merge duplicate cards: union of phones/emails/addresses/notes/groups
    lands on the kept card, the others are deleted. keep must be one of
    identifiers. Requires confirm=true. Returns the merged card."""
    if confirm is not True:
        return _refuse("merge_contacts")
    if keep not in identifiers:
        return json.dumps({"ok": False, "error": "keep must be one of identifiers"})
    if len(identifiers) < 2:
        return json.dumps({"ok": False, "error": "need at least two identifiers"})
    cards = []
    for ident in identifiers:
        card = CN.get(ident)
        if card is None:
            return json.dumps({"ok": False, "error": f"no contact {ident!r}"})
        cards.append(card)
    fields = _merged_fields(cards, keep)
    note = fields.pop("note", None)
    if note and note != (CN.get(keep).get("note") or ""):
        try:
            CN.update(keep, {"note": note})
        except RuntimeError:
            pass  # notes not writable without the entitlement; keep's stands
    CN.update(keep, fields)
    group_ids = set()
    for ident in identifiers:
        if ident == keep:
            continue
        for g in CN.groups():
            if ident in CN.group_member_ids(g["id"]):
                group_ids.add(g["id"])
    for gid in group_ids:
        if keep not in CN.group_member_ids(gid):
            CN.add_member(keep, gid)
    for ident in identifiers:
        if ident != keep:
            CN.delete(ident)
    after = _annotate(CN.get(keep))
    _log_change("merge_contacts", keep, cards, after, "ok")
    return json.dumps({"ok": True, "contact": after}, ensure_ascii=False)


def _safe(fn):
    """Return the real exception as a tool result instead of letting the
    MCP layer swallow it into a bare 'Error executing tool'. A TCC denial
    then reads as exactly that, not as a mystery."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # surfaced to the caller, never swallowed
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    return wrapper


# Registered here rather than via decorators so the functions above stay
# plain callables for the test suite.
for _fn in (
    authorization_status,
    list_contacts,
    get_contact,
    find_duplicates,
    export_contacts,
    create_contact,
    update_contact,
    delete_contact,
    create_group,
    add_to_group,
    remove_from_group,
    move_to_container,
    merge_contacts,
):
    mcp.tool()(_safe(_fn))


if __name__ == "__main__":
    mcp.run()
