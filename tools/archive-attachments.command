#!/bin/bash
# Add the Messages attachments folder to today's read-only iMessage archive
# as a single tar file. Runs in Terminal.app (double-click) for Full Disk
# Access, same as archive-messages.command, which must have run first.
# Owner and maintainer: Ryan Grey.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

DAY="${1:-$(date +%Y-%m-%d)}"
DEST="$HOME/Documents/imessage-archive/$DAY"
SRC="$HOME/Library/Messages/Attachments"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ -d "$DEST" ] || { echo "no archive at $DEST - run archive-messages.command first"; exit 1; }

step "Unlock $DEST for the addition"
chmod u+w "$DEST" "$DEST/RESTORE.md" "$DEST/SHA256SUMS"

step "Tar $SRC (uncompressed: media is already compressed)"
tar -cf "$DEST/attachments.tar" -C "$HOME/Library/Messages" Attachments
ls -lh "$DEST/attachments.tar"
echo "entries: $(tar -tf "$DEST/attachments.tar" | wc -l | tr -d ' ')"

step "Update the restore notes"
python3 - "$DEST/RESTORE.md" <<'EOF'
import sys
p = sys.argv[1]
s = open(p).read()
s = s.replace(
    "| `attachments-size.txt` | size of ~/Library/Messages/Attachments, which is NOT in this archive |",
    "| `attachments.tar` | ~/Library/Messages/Attachments, complete (uncompressed tar; media is already compressed) |\n"
    "| `attachments-size.txt` | size of that folder at snapshot time |")
s = s.replace("## Off-machine copy",
    "## Attachments\n\n    tar -xf attachments.tar -C <somewhere>   # restores an Attachments/ folder\n\n## Off-machine copy")
open(p, "w").write(s)
EOF

step "Checksums and relock"
cd "$DEST"
shasum -a 256 chat.db chat.db.report.json imessage-mcp-index.db RESTORE.md attachments-size.txt attachments.tar $( [ -f sent.log ] && echo sent.log ) > SHA256SUMS
shasum -a 256 -c SHA256SUMS
chmod 444 ./*
chmod 555 .
du -sh .

cat <<EOF

Local attachments archive complete. Ryan Grey manages off-machine backups
separately; this script has not uploaded or verified a remote copy.
Press return to close this window.
EOF
read -r _
