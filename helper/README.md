# Ryan Contacts

A native, locally signed macOS app owns the Contacts permission. It launches
the existing Contacts MCP server for each client through an owner-only Unix
socket. There is no TCP listener and no contact cache or content log in the
transport. The existing server's explicit write confirmations and local audit
log remain in place. All processes running as your macOS user can connect;
this is a local user boundary, not an application identity allowlist.

## Install and use

Build on Apple Silicon with `bash helper/build.sh`. The app runs hidden without
a Dock icon or menu bar item. For permission setup, quit the running helper
using Activity Monitor, then run
`open -a "$HOME/Applications/Ryan Contacts.app" --args --settings`
and choose **Request Contacts Access**.
Approve the macOS prompt for **Ryan Contacts**. A denied grant can be changed
in System Settings > Privacy & Security > Contacts. Revoking it blocks future
server access. Notes remain subject to Apple's separate restricted entitlement.

Configure the MCP command as the existing virtual environment's Python and
its argument as the absolute path to `helper/bridge.py`. The bridge opens the
app on demand through LaunchServices; it never starts the permissioned service
under a different client identity. Normal launches and reopen events never show
the settings window. Closing an explicitly opened settings window leaves it running;
**Quit Helper** stops the service and child servers. Existing clients must
reload their MCP registration after changing the command.

The local LaunchAgent `~/Library/LaunchAgents/dev.ryangrey.contacts-helper.plist`
opens the app in the background at login through LaunchServices. The app stays
running after the launcher exits. It creates no menu bar status item.
To disable automatic startup, unload that LaunchAgent with `launchctl bootout`
and remove its plist; on-demand MCP launches remain available.

The app requires Python at `~/.local/imessage-mcp/.venv/bin/python` and the
repository at `~/Developer/imessage-mcp`. Build artifacts stay in Applications,
outside Git. Rebuilding changes the local signature and may require granting
access again. Quit the helper before rebuilding.

## Verification

Use the existing `authorization_status` tool to verify the actual server's
grant without retrieving any contacts. Test simultaneous independent MCP
sessions through the bridge. Synthetic contact tests are run with
`python -m pytest tests/test_contacts.py tests/test_contacts_bridge.py`.

Rollback: restore the previous client registrations, quit Ryan Contacts, and
revoke its grant in System Settings. Direct server launches use their original
host permissions; they never trigger permission prompts implicitly.
