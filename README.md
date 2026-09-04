# imessage-mcp

Local [MCP](https://modelcontextprotocol.io) servers that give the Claude
desktop app access to Apple Messages and Apple Contacts on your Mac — all
locally, with no network calls.

- **`server.py` (iMessage)** — search your message history, list chats, read
  threads, resolve contact names to handles, and send iMessages.
- **`contacts.py` (Contacts)** — list, search, deduplicate, create, update,
  merge, group, and move contacts between accounts, through Apple's
  Contacts.framework (CNContactStore) — never by touching the AddressBook
  database files directly.

## What it does

- **Reads** `~/Library/Messages/chat.db` strictly read-only (SQLite
  `mode=ro&immutable=1`, so Messages.app is never locked or blocked).
- **Decodes** message bodies from the `attributedBody` typedstream blob that
  modern macOS uses (with the plain `text` column as fallback), and converts
  Apple's 2001-epoch nanosecond timestamps to local ISO time.
- **Indexes** message text into a local SQLite FTS5 database (`index.db`, in
  this directory, gitignored) for fast full-text search. Built on first
  search, then refreshed incrementally by ROWID.
- **Sends** through Messages.app itself via AppleScript. Sending requires an
  explicit `confirm=true`, and every send is appended to a local audit log
  (`sent.log`, gitignored).

It cannot create new group chats or send reactions/tapbacks — AppleScript has
no way to do either. It does not control your screen.

## Tools

| Tool | What it does |
| --- | --- |
| `search_messages(query, contact?, chat_id?, since?, until?, limit=50)` | Full-text search over decoded message bodies, newest first. |
| `list_chats(limit=30, since?)` | Recent chats with display name, participants, last-message preview/time, unread count. |
| `get_thread(chat_id \| handle, since?, until?, limit=200)` | Ordered messages for one chat (guid, chat identifier, or display name) or one 1:1 handle. |
| `get_contact_handles(name)` | Resolve a contact name to phone/email handles via the AddressBook database; falls back to handles seen in chat.db. |
| `refresh_contacts()` | Rebuild the handle→name map right now (it otherwise caches for five minutes) — use after renaming a contact. |
| `index_status()` | Row counts, newest message timestamp, FTS index state, and whether attributedBody decoding is working. |
| `send_message(to, text, attachment_path?, confirm)` | Send to a handle or an existing chat id (group chats included). Refuses unless `confirm` is `true`. |

Messages come back with sender, direction (`from_me`), local ISO timestamp,
chat name, body text, and attachments as filenames + paths only (the files
themselves are never opened). When the AddressBook database is readable,
handles are joined to contact names everywhere they appear — `sender_name`
on messages (your own card's name on `from_me` rows, resolved from the
Messages account handle; `IMESSAGE_OWNER_NAME` overrides), names on chat
participants, and 1:1 chats titled by contact instead of raw number. Names
resolve through Contacts.framework (CNContactStore) when the process holds
the Contacts grant — the same unified view Contacts.app shows, with the
owner's name taken from the "me card". Without the grant it falls back to
reading the AddressBook SQLite directly: only the per-account stores under
`Sources/` (newest first — the pre-Sources root store is legacy data),
opened plain read-only rather than immutable so renames synced from iCloud
are picked up from the WAL immediately. `index_status` reports which source
fed the join.

## Contacts tools (`contacts.py`)

| Tool | What it does |
| --- | --- |
| `list_contacts(query?, container?, group?, limit=100)` | Contacts with name, org, phones, emails, addresses, notes, account (iCloud/Google/local), and group memberships. |
| `get_contact(identifier \| name)` | One fully annotated card; an ambiguous name returns the candidates. |
| `find_duplicates(strategy)` | Clusters sharing a phone, email, or normalized name, with the fields that differ between the cards (phones/emails capped at 5 per card with a `+N more` marker). |
| `linked_cards(identifier \| name)` | The per-account cards behind one unified contact — identifier, container, full fields, groups, and which fields differ. Accepts the unified id or any piece id. |
| `export_contacts(container?, path?)` | Restorable backup pair: an Apple-serialized vCard 3.0 file (photos included) plus a JSON file carrying what vCard drops — per-card container, group memberships, and linked pieces. Defaults to `~/Documents/Claude/contacts-backups/`. |
| `authorization_status()` | The raw Contacts TCC state (`notDetermined`/`denied`/`authorized`/…) without triggering the permission prompt — tells a denied grant apart from a code bug. |
| `create_contact(fields, container="iCloud", confirm)` | New card in an explicit container. |
| `update_contact(identifier, fields, confirm)` | Replace the given fields on a card. |
| `delete_contact(identifier, confirm)` | Delete a card permanently. |
| `me_card()` | The owner's own "me" contact, fully annotated — no guessing which card is the owner. |
| `set_photo(identifier, path, confirm)` | Set a card's photo from a local JPEG/PNG, downscaled to ≤1024px on the long side before saving. |
| `create_group(name, container="iCloud", confirm)` / `add_to_group` / `remove_from_group` | Group management. |
| `move_to_container(identifier, "iCloud", confirm)` | Copy the card into the target account with all fields, re-add group memberships that exist there, then delete the source card. |
| `merge_contacts(identifiers[], keep, confirm)` | Union of phones/emails/addresses/dates/notes/groups onto the kept card, delete the rest, return the merged card. |

Card fields cover names, organization, `job_title`, `department`,
`nickname`, `note`, `phones`/`emails`/`urls` as `{label, value}` lists,
`addresses` as `{label, street, city, state, postal_code, country}`,
`social_profiles` as `{service, username, url}`, `birthday` as
`{year?, month, day}` (year optional), `dates` as `{label, year?, month, day}`
(label `anniversary` or `other`), `contact_type` (`person` or
`organization` — an organization card displays by company name even when it
carries a given/family name, so converting a business card to a person means
setting this too), and `has_image` (with
`get_contact(..., include_photo=true)` returning a `photo_path` temp file).

Cards that CNContactStore presents as unified (linked) contacts are labeled
`"container": "linked"` with their per-account pieces listed; write tools
refuse a multi-piece unified identifier and name the pieces to use instead.
A unified card over exactly one underlying card — residue of past linking —
resolves to that card's container and carries a `piece_identifier` field
naming the real card for writes.

Contacts safety rules:

- **Every write tool refuses unless `confirm` is `true`**, and every change
  is appended to `contacts-changes.log` (in this directory, gitignored)
  with the full before/after card.
- The iCloud container is resolved **structurally** — the CardDAV container
  Apple names `Card` — never as "the default account", so an iCloud-targeted
  card can never silently land in a Google account. If your setup is
  ambiguous, pin it with `CONTACTS_ICLOUD_CONTAINER_ID`.
- All access goes through Contacts.framework, the same path Contacts.app
  uses. Nothing ever writes to the `AddressBook-v22.abcddb` SQLite files.
- Changes propagate through iCloud exactly like edits made in Contacts.app.
  Messages display names on your other devices update after Contacts has
  been opened once there.
- Contact **notes** require Apple's `com.apple.developer.contacts.notes`
  entitlement, which normal processes don't have: without it the note field
  reads as unavailable and note writes are refused cleanly.

## Install

Requires macOS and Python 3.10+.

```sh
git clone https://github.com/ryan-grey/imessage-mcp ~/Documents/imessage-mcp
python3 -m venv ~/.local/imessage-mcp/.venv
~/.local/imessage-mcp/.venv/bin/pip install mcp pyobjc-core pyobjc-framework-Contacts
```

(`pyobjc-*` is only needed for the Contacts server; the iMessage server has
no dependencies beyond `mcp`.)

Note on the Contacts grant: macOS attributes it to the **app that launches
the server**, not to the venv's python binary — so when the Claude desktop
app spawns it, "Claude" is what appears (and must be enabled) under System
Settings → Privacy & Security → Contacts; run it from a terminal and the
grant belongs to the terminal app instead. The `authorization_status` tool
reports the live state for whichever context the server is running in.

Then register them in the Claude desktop app's MCP config
(`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "imessage": {
      "command": "/Users/YOU/.local/imessage-mcp/.venv/bin/python",
      "args": ["/Users/YOU/Documents/imessage-mcp/server.py"]
    },
    "contacts": {
      "command": "/Users/YOU/.local/imessage-mcp/.venv/bin/python",
      "args": ["/Users/YOU/Documents/imessage-mcp/contacts.py"]
    }
  }
}
```

Restart the Claude desktop app and the `imessage` and `contacts` tools
appear.

## The macOS permission grants

Nothing works until you grant these — all are manual, by Apple's design:

1. **Full Disk Access** for the process that runs the server. Open
   **System Settings → Privacy & Security → Full Disk Access** and add the
   app that spawns the server — for the setup above that is the **Claude**
   desktop app (the Python interpreter inherits the grant from the app that
   launches it). If you run the server from a terminal instead, grant your
   terminal app. Without this, opening `chat.db` fails with
   "authorization denied".
2. **Automation** for Messages, on first send. The first time
   `send_message` runs, macOS shows a prompt asking to allow the controlling
   app to control **Messages**. Click Allow. If you dismissed it, re-enable
   under **System Settings → Privacy & Security → Automation**.
3. **Contacts** access, for the Contacts server. The first Contacts tool
   call triggers the system prompt for the app that runs the server (the
   Claude desktop app, or your terminal/python if run by hand). If you
   dismissed it, re-enable under **System Settings → Privacy & Security →
   Contacts**.

Messages must be signed in to your Apple account for sending to work. The
app can be closed — AppleScript launches it as needed.

## Privacy notes

- The message database is opened read-only and immutable; there is no code
  path that writes to anything under `~/Library/Messages`.
- Everything is local. The server makes no network calls of any kind.
- The FTS index (`index.db`), send log (`sent.log`), and contact change log
  (`contacts-changes.log`) live in this directory, are gitignored, and never
  leave your machine. Delete them any time; the index rebuilds on the next
  search.
- Test fixtures are fully synthetic — generated by `tests/make_fixture.py`
  and an in-memory fake of the Contacts adapter — so the repository never
  contains real message or contact data.

## Tests

```sh
~/.local/imessage-mcp/.venv/bin/python tests/test_server.py
~/.local/imessage-mcp/.venv/bin/python tests/test_contacts.py
```

## License

MIT — see [LICENSE](LICENSE).
