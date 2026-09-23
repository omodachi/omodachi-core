# Upstream issue draft — Quickshell segfaults when an output it has a window on is removed

Status: **draft, not filed.** Written for `quickshell-mirror/quickshell`; the
null dereference itself is in qtbase, so a second, smaller report belongs at
`bugreports.qt.io` (Qt Wayland client). Nothing here has been sent anywhere.

---

## Title

Segfault in `QWaylandWindow::setGeometry` when a `wl_output` is removed while a
toplevel is being reconfigured onto another one

## Environment

```
Quickshell 0.3.1 (revision unset, distributed by Arch Linux, package 0.3.1-1)
Qt          6.11.2 (built against 6.11.2), package qt6-base 6.11.2-3
Build type  RelWithDebInfo, GCC 16.2.1, jemalloc ON, crash handling ON
Compositor  Hyprland 0.56.2 (efb50993…)
OS          Omarchy 4.0.4 (Arch), kernel 7.2.6-arch2
Config      /usr/share/omarchy/shell/shell.qml (Omarchy's own shell)
GPU         amdgpu (1002:7340) + i915 (8086:3e9b)
```

## What happens

With two outputs present, removing the one a Quickshell window lives on kills
the process with SIGSEGV. Quickshell's crash handler catches it and reloads the
configuration **inside the same process**, which is worse than exiting: the
shell answers IPC again, but every `IpcHandler` from the previous load is still
registered, so each one logs

```
QML IpcHandler at …/Panel.qml[…]: Handler was registered but will not be used
because another handler is registered for target omarchy.<name>
```

and the second registration is the one that is ignored. The result is a shell
that pings but does not work: the menu's Apps list comes up empty, the polkit
agent no longer answers, panels do not open. `omarchy-restart-shell` is the
only way back.

Reproduced 22 times out of 22 on this machine (see **Reproduction**).

## Backtrace

From `coredumpctl` + `debuginfod`, `quickshell` 0.3.1, Qt 6.11.2:

```
#3  qs::crash::(anonymous namespace)::signalHandler (sig=11)
      at quickshell/src/crash/handler.cpp:93
#4  <signal handler called>
#5  QtWaylandClient::QWaylandWindow::setGeometry (this=0x7f58fe8d4980, r=...)
      at qtbase/src/plugins/platforms/wayland/qwaylandwindow.cpp:491
#6  QtWaylandClient::QWaylandWindow::resizeFromApplyConfigure (sizeWithMargins=..., offset=...)
      at qtbase/src/plugins/platforms/wayland/qwaylandwindow.cpp:603
#7  QtWaylandClient::QWaylandXdgSurface::Toplevel::applyConfigure (this=0x7f58c962a680)
      at …/shellintegration/xdg-shell/qwaylandxdgshell.cpp:126
#8  QtWaylandClient::QWaylandXdgSurface::applyConfigure (this=0x7f58e7c6b4c0)
      at …/shellintegration/xdg-shell/qwaylandxdgshell.cpp:450
#9  ffi_call_unix64
#12 wl_closure_invoke   (wayland-1.26.0/src/connection.c:1243)
#13 dispatch_event      (wayland-1.26.0/src/wayland-client.c:1732)
#15 wl_display_dispatch_queue_pending
#16 QtWaylandClient::QWaylandDisplay::qt_static_metacall
      at qtbase/src/plugins/platforms/wayland/qwaylanddisplay.cpp:231
#19 QEventDispatcherGlib::processEvents
#21 QEventLoop::exec
#22 QCoreApplication::exec
#23 qs::launch::launch (…)  at quickshell/src/launch/launch.cpp:320
#24 qs::launch::(anonymous namespace)::launchFromCommand at quickshell/src/launch/command.cpp:482
#25 qs::launch::runCommand  at quickshell/src/launch/command.cpp:580
#26 qs::launch::main        at quickshell/src/launch/main.cpp:130
#27 main                    at quickshell/src/main.cpp:3
```

The faulting line:

```cpp
// qtbase/src/plugins/platforms/wayland/qwaylandwindow.cpp
void QWaylandWindow::setGeometry(const QRect &r)
{
    auto rect = r;
    if (fixedToplevelPositions && !QPlatformWindow::parent() && window()->type() != Qt::Popup
        && window()->type() != Qt::ToolTip && window()->type() != Qt::Tool) {
        rect.moveTo(screen()->geometry().topLeft());   // <-- line 491, screen() is null
    }
```

`QPlatformWindow::screen()` is documented to return null:

```cpp
QPlatformScreen *QPlatformWindow::screen() const
{
    QScreen *scr = window()->screen();
    return scr ? scr->handle() : nullptr;
}
```

and line 491 does not check it. In the core:

```
(gdb) p fixedToplevelPositions
$1 = true                       # Qt's default; QT_WAYLAND_DISABLE_FIXED_POSITIONS is unset
(gdb) p this->mDisplay->mScreens
$2 = {… size = 1}               # only the *other* output is left
(gdb) p *(QWindow*)0x7f5904c0f0f0
$3 = {<QObject> = {_vptr.QObject = 0x55f9f1e52c60 <vtable for ProxiedWindow+328>, …,
      d_ptr = {d = 0x0}}, …}     # Quickshell's ProxiedWindow, private already gone
```

So the QWindow behind the platform window is a Quickshell `ProxiedWindow` that
is already being destroyed (`QObjectPrivate` is null) while its
`QWaylandWindow` is still in the wayland dispatch loop applying a pending
`xdg_toplevel.configure`. `window()->screen()` reads that dead object,
`screen()` returns null, and line 491 dereferences it.

Two things look wrong, and they can be fixed independently:

1. **qtbase**: `QWaylandWindow::setGeometry` must not assume `screen()`. A
   one-line guard (`if (auto *s = screen()) rect.moveTo(s->geometry().topLeft());`)
   turns this crash into a harmless no-op.
2. **Quickshell**: the `ProxiedWindow` for a screen that has gone away is
   destroyed while its platform window still has a configure in flight. Whatever
   the ordering guarantee is meant to be, an app should not be able to observe a
   half-destroyed `QWindow` from `wl_display_dispatch_queue_pending`.

Note the window is an **xdg toplevel**, not a layer surface, which is why this
is reachable at all: the layer-shell path does not go through
`QWaylandXdgSurface::Toplevel::applyConfigure`.

## Reproduction

Minimal shape, on Hyprland 0.56.2 with one physical output `eDP-1`:

```bash
export HYPRLAND_INSTANCE_SIGNATURE=$(ls -t /run/user/$UID/hypr | head -1)

# 1. a second output for the shell to move onto
hyprctl output create headless HEADLESS-1
hyprctl keyword monitor HEADLESS-1,1280x894@60,2304x0,1     # or hl.monitor via `hyprctl eval`

# 2. move everything onto it
for id in $(hyprctl -j workspaces | jq -r '.[].id'); do
  hyprctl dispatch moveworkspacetomonitor "$id HEADLESS-1"
done

# 3. take the first one away — immediately, while the shell is still
#    building its surfaces on HEADLESS-1
hyprctl eval 'hl.monitor({ output = "eDP-1", disabled = true })'
```

The crash lands 1.4–2.2 s after step 1 begins. Steps 2 and 3 are not needed:
creating the second output, sleeping **five seconds**, and then running step 3
alone — no workspace moved, the shell's surfaces long since built on
`HEADLESS-1` — crashes it just the same.

On this machine, as the takeover feature of another program drives it:

| what the caller did | takeovers | shell crashes |
| --- | --- | --- |
| create output → move workspaces → disable `eDP-1`, back to back | 18 | 18 |
| the same, waiting for the shell's layer surfaces to settle on the new output first | 6 | 6 |
| the same, with a further 1 s and 3 s pause before the disable | 3 + 3 | 3 + 3 |
| create output → move workspaces → `dpms off eDP-1` instead of disabling | 10 | 0 |

Waiting does not help: the shell had already built its own layer surfaces on
the new output (measured: complete and stable 0.5 s after the output appears)
and the removal still killed it. Only not removing the output avoids it.

The Hyprland event order at the crash, from the shell's own log
(`quickshell.hyprland.ipc.events`):

```
moveworkspace>>1,HEADLESS-1 … moveworkspace>>5,HEADLESS-1
createworkspace>>12 / destroyworkspace>>12
monitorremoved>>eDP-1
monitorremovedv2>>0,eDP-1,Apple Computer Inc Color LCD
openlayer>>omarchy-bar / openlayer>>omarchy-background / openlayer>>omarchy-keyboard-panel-dismiss
closelayer>>omarchy-background / closelayer>>omarchy-herdr-pin / closelayer>>omarchy-keyboard-panel
destroyworkspace>>3 / destroyworkspace>>4 / destroyworkspace>>5
closelayer>>omarchy-bar
windowtitlev2>>…,quickshell
<segfault>
```

The `xdg_toplevel.configure` being applied carried `894` for its height — the
logical height of the *new* output — so the compositor was configuring the
window onto the screen that stays, in the same batch as the removal of the one
it was on.

## Workaround

Do not remove the output at all. Turning the screen off keeps the `wl_output`
and the `QScreen` alive, and nothing is reconfigured onto a dead one:

```bash
hyprctl repl 'local r = hl.dispatch(hl.dsp.dpms({ monitor = hl.get_monitor("eDP-1") })); return tostring(r.ok)'
```

Note for whoever writes that call: on 0.56.2 `hl.dsp.dpms` **toggles**. The
monitor is honoured, the state is not — `mode="on"`, `state="on"`, `on=true`,
`power=true`, `enabled=true`, a positional `"off"` and a positional
`{"off", monitor}` all flip whatever the screen was. Read `dpmsStatus` from
`hyprctl -j monitors all` first and only dispatch when it would change
something.

Ten full cycles of create-output → move-workspaces → DPMS off → DPMS on →
move back → remove output produced no crash and left `hyprctl monitors -j`
byte-identical.

Removing a *headless* output the shell also draws on is fine: 30 removals, no
crash. It is removing the physical output the shell started on that is fatal,
which is consistent with Qt reassigning windows off a screen that is going
away — including, on this host, the primary one.

## Artefacts available

- three `report.txt` + `log.qslog` pairs from `~/.cache/quickshell/crashes/`
- `systemd-coredump` cores for two of them (37 MB each), symbolised above
- the full `quickshell.hyprland.ipc.events` log around each crash
