"""Wire protocol identity shared by the Unix IPC, HTTPS/WSS and discovery paths."""
from __future__ import annotations

CONTRACT_REVISION = "omodachi.v1"

# Published state for "no Remote desktop is presenting a bar". Seeding it in the
# hub's initial state keeps the first desktop maintenance pass from emitting a
# remote_bar.changed event that reports no actual change.
IDLE_REMOTE_BAR = {"active": False, "session_id": None, "output_name": None, "viewport": None,
                   "orientation": None, "logical_size": None, "workspaces": [], "revision": None}

# The panel destinations a host-side shortcut can recall. `overview` is the
# root panel; `keybindings` is the shortcuts overlay SUPER+K opens locally, so a
# recall under Remote lands on the same place the user would have got.
#
# `settings` is the third (ARCH-1 / Study 04 A-59). The Omodachi plugin's own
# bar widget is the one the user clicks to reach *this app's* preferences, and
# under Remote that click has to land on the device holding the stream rather
# than on a QML panel behind the picture. It is a destination, not a new
# endpoint: the same `panel.summon` carries it.
PANEL_VIEWS = ("overview", "keybindings", "settings")

# PAIR-2. How this host answers a pairing request that carries no invitation:
# `open` accepts it as pending (the approval on the computer is the boundary,
# and always was), `invite` refuses it with `pairing_invitation_required`.
PAIRING_MODES = ("open", "invite")

# The Omarchy plugin's own device credential, issued locally by
# `omodachid --issue-token com.omodachi.host`. It is the one row in `devices
# list` that is not a companion, and revoking it takes away the panel the user
# would revoke from - so it is labelled, not offered a button.
PLUGIN_DEVICE_ID = "com.omodachi.host"
