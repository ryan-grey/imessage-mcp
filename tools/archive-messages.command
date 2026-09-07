#!/bin/bash
# Snapshot the entire iMessage history into a read-only archive folder.
# Runs in Terminal.app (double-click in Finder) because reading
# ~/Library/Messages needs Full Disk Access granted to the launching app.
# Owner and maintainer: Ryan Grey.
#
# Copies chat.db with SQLite's online backup API (consistent even while
# Messages.app is running), checks integrity, records counts, adds the
# imessage-mcp search index and send log, checksums everything, and makes
# the folder read-only. Attachments are NOT copied; their size is recorded
# so that can be a separate decision.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

DAY="$(date +%Y-%m-%d)"
DEST="$HOME/Documents/imessage-archive/$DAY"
SRC="$HOME/Library/Messages/chat.db"
MCP="$HOME/Developer/imessage-mcp"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Archive folder $DEST"
if [ -d "$DEST" ]; then chmod -R u+w "$DEST"; fi
mkdir -p "$DEST"

step "Copy chat.db (online backup) and verify"
python3 - "$SRC" "$DEST/chat.db" <<'EOF'
import sqlite3, sys, json
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
dst = sqlite3.connect(dst_path)
src.backup(dst)
src.close(); dst.close()
c = sqlite3.connect(dst_path)
ok = c.execute("pragma integrity_check").fetchone()[0]
counts = {t: c.execute(f"select count(*) from {t}").fetchone()[0]
          for t in ("message", "chat", "handle", "attachment")}
newest = c.execute("select max(date) from message").fetchone()[0]
c.close()
if ok != "ok":
    print("INTEGRITY FAILED:", ok); sys.exit(1)
import datetime
apple = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)
newest_iso = (apple + datetime.timedelta(seconds=newest / 1e9)).astimezone().isoformat(timespec="seconds") if newest else None
report = {"integrity": ok, "counts": counts, "newest_message": newest_iso}
print(json.dumps(report, indent=1))
open(dst_path.rsplit("/", 1)[0] + "/chat.db.report.json", "w").write(json.dumps(report, indent=1) + "\n")
EOF

step "Copy the imessage-mcp index and send log"
python3 - "$MCP/index.db" "$DEST/imessage-mcp-index.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); src.close()
print("index integrity:", dst.execute("pragma integrity_check").fetchone()[0],
      "rows:", dst.execute("select count(*) from msg").fetchone()[0])
dst.close()
EOF
[ -f "$MCP/sent.log" ] && cp -p "$MCP/sent.log" "$DEST/sent.log"

step "Attachments folder size (not copied)"
du -sh "$HOME/Library/Messages/Attachments" 2>/dev/null | tee "$DEST/attachments-size.txt" || echo "unreadable" | tee "$DEST/attachments-size.txt"

step "Restore notes"
cat > "$DEST/RESTORE.md" <<EOF
# iMessage archive — $DAY

Read-only snapshot of the complete Messages database on this Mac, taken with
SQLite's online backup API, integrity-checked and checksummed. Never edit
anything here; restore by copying OUT of it.

| File | What it is |
| --- | --- |
| \`chat.db\` | Apple's Messages database: every message, chat, handle and attachment record |
| \`chat.db.report.json\` | integrity result, row counts and newest message time at snapshot |
| \`imessage-mcp-index.db\` | imessage-mcp's decoded FTS5 search index (derivable from chat.db) |
| \`sent.log\` | imessage-mcp's send audit log |
| \`attachments-size.txt\` | size of ~/Library/Messages/Attachments, which is NOT in this archive |
| \`SHA256SUMS\` | checksums of everything above |

## Verify

    cd "$DEST" && shasum -a 256 -c SHA256SUMS

## Read the history without touching Messages.app

Point imessage-mcp at the copy: set \`IMESSAGE_CHAT_DB=$DEST/chat.db\`
in its env, or open it with any SQLite tool. Message text often lives in the
\`attributedBody\` blob rather than the \`text\` column; imessage-mcp decodes it.

## Restore into Messages.app (last resort)

Apple does not support replacing chat.db by hand; iCloud Messages will fight
it. If it is ever necessary: sign out of iCloud Messages, quit Messages, back
up the current \`~/Library/Messages\` folder, copy \`chat.db\` in with its
\`-wal\`/\`-shm\` sidecars removed, then reopen Messages. Prefer reading the
archive directly over restoring.

## Off-machine copy

    aws --profile infra s3 sync s3://ryangrey-imessage-archive/$DAY/ <dir>
EOF

step "Checksums and read-only"
cd "$DEST"
shasum -a 256 chat.db chat.db.report.json imessage-mcp-index.db RESTORE.md attachments-size.txt $( [ -f sent.log ] && echo sent.log ) > SHA256SUMS
shasum -a 256 -c SHA256SUMS
chmod 444 ./*
chmod 555 .
ls -la
du -sh .

cat <<EOF

Local archive complete. Ryan Grey manages off-machine backups separately;
this script has not uploaded or verified a remote copy.
Press return to close this window.
EOF
read -r _
