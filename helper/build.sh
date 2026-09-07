#!/bin/bash
set -euo pipefail
source_dir="$(cd "$(dirname "$0")" && pwd)"
app_dir="$HOME/Applications/Ryan Contacts.app"
mkdir -p "$app_dir/Contents/MacOS"
xcrun swiftc -target arm64-apple-macosx14.0 "$source_dir/main.swift" \
  -o "$app_dir/Contents/MacOS/RyanContacts" -framework AppKit -framework Contacts
cp "$source_dir/Info.plist" "$app_dir/Contents/Info.plist"
codesign --force --sign - --options runtime \
  --entitlements "$source_dir/entitlements.plist" "$app_dir"
codesign --verify --strict "$app_dir"
