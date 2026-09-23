"""Reviewed live menu state adapters; no free-form shell/expression evaluator.

The registries below are authored from the installed Omarchy menu on
2026-09-16. Entry IDs and exact source expressions/action fields pin readers.
Unknown/changed source stays unavailable. The existing live bootstrap hook
registers only this reviewed finite menu surface. Registration reads state;
mutations only occur through a later authenticated, user-selected invocation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import threading
import time

from .graphical import graphical_environment
from .routes import RouteDescriptor

REVIEWED_GUARDS = {'install.ai.chatgpt': '! omarchy-pkg-present openai-codex-desktop',
 'install.ai.dictation': '! omarchy-pkg-present voxtype-bin',
 'install.ai.grok-bot': '! omarchy-pkg-present grok-bot',
 'install.ai.hermes': '! omarchy-pkg-present hermes-desktop',
 'install.ai.lm-studio': '! omarchy-pkg-present lmstudio-bin',
 'install.ai.ollama': '! omarchy-cmd-present ollama',
 'install.ai.openclaw': '! omarchy-pkg-present openclaw',
 'install.ai.perplexity': '! omarchy-pkg-present perplexity',
 'install.ai.t3-code': '! omarchy-pkg-present t3code-bin',
 'install.browser.brave': '! omarchy-pkg-present brave-bin',
 'install.browser.brave-origin': '! omarchy-pkg-present brave-origin-bin',
 'install.browser.chrome': '! omarchy-pkg-present google-chrome',
 'install.browser.edge': '! omarchy-pkg-present microsoft-edge-stable-bin',
 'install.browser.firefox': '! omarchy-pkg-present firefox',
 'install.browser.zen': '! omarchy-pkg-present zen-browser-bin',
 'install.development.php.php': '! omarchy-pkg-present php',
 'install.development.php.symfony': '! omarchy-pkg-present symfony-cli',
 'install.editor.cursor': '! omarchy-pkg-present cursor-bin',
 'install.editor.emacs': '! omarchy-pkg-present omarchy-emacs',
 'install.editor.helix': '! omarchy-pkg-present helix',
 'install.editor.sublime': '! omarchy-pkg-present sublime-text-4',
 'install.editor.vim': '! omarchy-pkg-present vim',
 'install.editor.vscode': '! omarchy-pkg-present visual-studio-code-bin',
 'install.editor.zed': '! omarchy-pkg-present zed',
 'install.gaming.heroic': '! omarchy-pkg-present heroic-games-launcher-bin',
 'install.gaming.lutris': '! omarchy-pkg-present lutris',
 'install.gaming.minecraft': '! omarchy-pkg-present minecraft-launcher',
 'install.gaming.retroarch': '! omarchy-pkg-present retroarch',
 'install.gaming.steam': '! omarchy-pkg-present steam',
 'install.gaming.xbox-controllers': '! omarchy-pkg-present xpadneo-dkms',
 'install.service.1password': '! omarchy-pkg-present 1password',
 'install.service.bitwarden': '! omarchy-pkg-present bitwarden',
 'install.service.dropbox': '! omarchy-pkg-present dropbox',
 'install.service.nordvpn': '! omarchy-pkg-present nordvpn-bin',
 'install.service.once': '! omarchy-pkg-present once-bin',
 'install.service.signal': '! omarchy-pkg-present signal-desktop',
 'install.service.spotify': '! omarchy-pkg-present spotify',
 'install.service.tailscale': '! omarchy-pkg-present tailscale',
 'install.terminal.alacritty': '! omarchy-pkg-present alacritty',
 'install.terminal.foot': '! omarchy-pkg-present foot',
 'install.terminal.ghostty': '! omarchy-pkg-present ghostty',
 'install.terminal.kitty': '! omarchy-pkg-present kitty',
 'remove.ai.hermes': 'omarchy-pkg-present hermes-desktop',
 'remove.ai.openclaw': 'omarchy-pkg-present openclaw',
 'remove.ai.perplexity': 'omarchy-pkg-present perplexity',
 'remove.ai.t3-code': 'omarchy-pkg-present t3code-bin',
 'remove.browser.brave': 'omarchy-pkg-present brave-bin',
 'remove.browser.brave-origin': 'omarchy-pkg-present brave-origin-bin',
 'remove.browser.chrome': 'omarchy-pkg-present google-chrome',
 'remove.browser.edge': 'omarchy-pkg-present microsoft-edge-stable-bin',
 'remove.browser.firefox': 'omarchy-pkg-present firefox',
 'remove.browser.zen': 'omarchy-pkg-present zen-browser-bin',
 'remove.development.php.php': 'omarchy-pkg-present php',
 'remove.development.php.symfony': 'omarchy-pkg-present symfony-cli',
 'remove.dictation': 'omarchy-pkg-present voxtype-bin',
 'remove.gaming.heroic': 'omarchy-pkg-present heroic-games-launcher-bin',
 'remove.gaming.lutris': 'omarchy-pkg-present lutris',
 'remove.gaming.minecraft': 'omarchy-pkg-present minecraft-launcher',
 'remove.gaming.retroarch': 'omarchy-pkg-present retroarch',
 'remove.gaming.steam': 'omarchy-pkg-present steam',
 'remove.gaming.xbox-controllers': 'omarchy-pkg-present xpadneo-dkms',
 'remove.security.fido2': 'omarchy-pkg-present pam-u2f',
 'remove.security.fingerprint': 'omarchy-pkg-present fprintd',
 'remove.service.dropbox': 'omarchy-pkg-present dropbox',
 'remove.service.tailscale': 'omarchy-pkg-present tailscale',
 'setup.default.browser.brave': 'omarchy-cmd-present brave',
 'setup.default.browser.brave-origin': 'omarchy-cmd-present brave-origin',
 'setup.default.browser.chrome': 'omarchy-cmd-present google-chrome-stable',
 'setup.default.browser.chromium': 'omarchy-cmd-present chromium',
 'setup.default.browser.edge': 'omarchy-cmd-present microsoft-edge-stable',
 'setup.default.browser.firefox': 'omarchy-cmd-present firefox',
 'setup.default.browser.zen': 'omarchy-cmd-present zen-browser',
 'setup.default.editor.cursor': 'omarchy-cmd-present cursor',
 'setup.default.editor.emacs': 'omarchy-cmd-present emacs',
 'setup.default.editor.helix': 'omarchy-cmd-present helix',
 'setup.default.editor.neovim': 'omarchy-cmd-present nvim',
 'setup.default.editor.sublime': 'omarchy-cmd-present sublime_text',
 'setup.default.editor.vim': 'omarchy-cmd-present vim',
 'setup.default.editor.vscode': 'omarchy-cmd-present code',
 'setup.default.editor.zed': 'omarchy-cmd-present zeditor',
 'setup.default.terminal.alacritty': 'omarchy-cmd-present alacritty',
 'setup.default.terminal.foot': 'omarchy-cmd-present foot',
 'setup.default.terminal.ghostty': 'omarchy-cmd-present ghostty',
 'setup.default.terminal.kitty': 'omarchy-cmd-present kitty',
 'setup.security.fingerprint': 'omarchy-hw-fingerprint',
 'trigger.capture.screenrecord.webcam': 'omarchy-hw-webcam',
 'trigger.hardware.hybrid-gpu': 'omarchy-hw-hybrid-gpu',
 'trigger.hardware.laptop-display': 'omarchy-hw-laptop',
 'trigger.hardware.mirror-display': 'omarchy-hw-laptop',
 'trigger.hardware.touchpad': 'omarchy-hw-touchpad',
 'trigger.hardware.touchpad-haptics.high': 'omarchy-hw-dell-xps-haptic-touchpad && omarchy-cmd-present '
                                           'dell-xps-touchpad-haptics',
 'trigger.hardware.touchpad-haptics.low': 'omarchy-hw-dell-xps-haptic-touchpad && omarchy-cmd-present '
                                          'dell-xps-touchpad-haptics',
 'trigger.hardware.touchpad-haptics.mid': 'omarchy-hw-dell-xps-haptic-touchpad && omarchy-cmd-present '
                                          'dell-xps-touchpad-haptics',
 'trigger.hardware.touchscreen': 'omarchy-hw-touchscreen',
 'trigger.toggle.battery-percentage': 'omarchy-hw-laptop'}

REVIEWED_CHECKS = {'setup.default.agent.claude': {'action': 'omarchy-default-agent claude',
                                'expression': '[[ "$(omarchy-default-agent)" == "claude" ]]',
                                'when': ''},
 'setup.default.agent.codex': {'action': 'omarchy-default-agent codex',
                               'expression': '[[ "$(omarchy-default-agent)" == "codex" ]]',
                               'when': ''},
 'setup.default.agent.copilot': {'action': 'omarchy-default-agent copilot',
                                 'expression': '[[ "$(omarchy-default-agent)" == "copilot" ]]',
                                 'when': ''},
 'setup.default.agent.crush': {'action': 'omarchy-default-agent crush',
                               'expression': '[[ "$(omarchy-default-agent)" == "crush" ]]',
                               'when': ''},
 'setup.default.agent.cursor-agent': {'action': 'omarchy-default-agent cursor-agent',
                                      'expression': '[[ "$(omarchy-default-agent)" == "cursor-agent" ]]',
                                      'when': ''},
 'setup.default.agent.gemini': {'action': 'omarchy-default-agent gemini',
                                'expression': '[[ "$(omarchy-default-agent)" == "gemini" ]]',
                                'when': ''},
 'setup.default.agent.grok': {'action': 'omarchy-default-agent grok',
                              'expression': '[[ "$(omarchy-default-agent)" == "grok" ]]',
                              'when': ''},
 'setup.default.agent.hermes': {'action': 'omarchy-default-agent hermes',
                                'expression': '[[ "$(omarchy-default-agent)" == "hermes" ]]',
                                'when': ''},
 'setup.default.agent.muse': {'action': 'omarchy-default-agent muse',
                              'expression': '[[ "$(omarchy-default-agent)" == "muse" ]]',
                              'when': ''},
 'setup.default.agent.omp': {'action': 'omarchy-default-agent omp',
                             'expression': '[[ "$(omarchy-default-agent)" == "omp" ]]',
                             'when': ''},
 'setup.default.agent.openclaw': {'action': 'omarchy-default-agent openclaw',
                                  'expression': '[[ "$(omarchy-default-agent)" == "openclaw" ]]',
                                  'when': ''},
 'setup.default.agent.opencode': {'action': 'omarchy-default-agent opencode',
                                  'expression': '[[ "$(omarchy-default-agent)" == "opencode" ]]',
                                  'when': ''},
 'setup.default.agent.pi': {'action': 'omarchy-default-agent pi',
                            'expression': '[[ "$(omarchy-default-agent)" == "pi" ]]',
                            'when': ''},
 'setup.default.browser.brave': {'action': 'omarchy-default-browser brave',
                                 'expression': '[[ "$(omarchy-default-browser)" == "brave" ]]',
                                 'when': 'omarchy-cmd-present brave'},
 'setup.default.browser.brave-origin': {'action': 'omarchy-default-browser brave-origin',
                                        'expression': '[[ "$(omarchy-default-browser)" == "brave-origin" ]]',
                                        'when': 'omarchy-cmd-present brave-origin'},
 'setup.default.browser.chrome': {'action': 'omarchy-default-browser chrome',
                                  'expression': '[[ "$(omarchy-default-browser)" == "chrome" ]]',
                                  'when': 'omarchy-cmd-present google-chrome-stable'},
 'setup.default.browser.chromium': {'action': 'omarchy-default-browser chromium',
                                    'expression': '[[ "$(omarchy-default-browser)" == "chromium" ]]',
                                    'when': 'omarchy-cmd-present chromium'},
 'setup.default.browser.edge': {'action': 'omarchy-default-browser edge',
                                'expression': '[[ "$(omarchy-default-browser)" == "edge" ]]',
                                'when': 'omarchy-cmd-present microsoft-edge-stable'},
 'setup.default.browser.firefox': {'action': 'omarchy-default-browser firefox',
                                   'expression': '[[ "$(omarchy-default-browser)" == "firefox" ]]',
                                   'when': 'omarchy-cmd-present firefox'},
 'setup.default.browser.zen': {'action': 'omarchy-default-browser zen',
                               'expression': '[[ "$(omarchy-default-browser)" == "zen" ]]',
                               'when': 'omarchy-cmd-present zen-browser'},
 'setup.default.editor.cursor': {'action': 'omarchy-default-editor cursor',
                                 'expression': '[[ "$(omarchy-default-editor)" == "cursor" ]]',
                                 'when': 'omarchy-cmd-present cursor'},
 'setup.default.editor.emacs': {'action': 'omarchy-default-editor emacs',
                                'expression': '[[ "$(omarchy-default-editor)" == "emacs" ]]',
                                'when': 'omarchy-cmd-present emacs'},
 'setup.default.editor.helix': {'action': 'omarchy-default-editor helix',
                                'expression': '[[ "$(omarchy-default-editor)" == "helix" ]]',
                                'when': 'omarchy-cmd-present helix'},
 'setup.default.editor.neovim': {'action': 'omarchy-default-editor nvim',
                                 'expression': '[[ "$(omarchy-default-editor)" == "nvim" ]]',
                                 'when': 'omarchy-cmd-present nvim'},
 'setup.default.editor.sublime': {'action': 'omarchy-default-editor sublime_text',
                                  'expression': '[[ "$(omarchy-default-editor)" == "sublime_text" ]]',
                                  'when': 'omarchy-cmd-present sublime_text'},
 'setup.default.editor.vim': {'action': 'omarchy-default-editor vim',
                              'expression': '[[ "$(omarchy-default-editor)" == "vim" ]]',
                              'when': 'omarchy-cmd-present vim'},
 'setup.default.editor.vscode': {'action': 'omarchy-default-editor code',
                                 'expression': '[[ "$(omarchy-default-editor)" == "code" ]]',
                                 'when': 'omarchy-cmd-present code'},
 'setup.default.editor.zed': {'action': 'omarchy-default-editor zed',
                              'expression': '[[ "$(omarchy-default-editor)" == "zeditor" ]]',
                              'when': 'omarchy-cmd-present zeditor'},
 'setup.default.terminal.alacritty': {'action': 'omarchy-default-terminal alacritty',
                                      'expression': '[[ "$(omarchy-default-terminal)" == "alacritty" ]]',
                                      'when': 'omarchy-cmd-present alacritty'},
 'setup.default.terminal.foot': {'action': 'omarchy-default-terminal foot',
                                 'expression': '[[ "$(omarchy-default-terminal)" == "foot" ]]',
                                 'when': 'omarchy-cmd-present foot'},
 'setup.default.terminal.ghostty': {'action': 'omarchy-default-terminal ghostty',
                                    'expression': '[[ "$(omarchy-default-terminal)" == "ghostty" ]]',
                                    'when': 'omarchy-cmd-present ghostty'},
 'setup.default.terminal.kitty': {'action': 'omarchy-default-terminal kitty',
                                  'expression': '[[ "$(omarchy-default-terminal)" == "kitty" ]]',
                                  'when': 'omarchy-cmd-present kitty'},
 'setup.network.dns.cloudflare': {'action': 'omarchy-dns Cloudflare',
                                  'expression': '[[ "$(omarchy-dns)" == "Cloudflare" ]]',
                                  'when': ''},
 'setup.network.dns.custom': {'action': "omarchy-launch-floating-terminal-with-presentation 'omarchy-dns "
                                        "Custom'",
                              'expression': '[[ "$(omarchy-dns)" == "Custom" ]]',
                              'when': ''},
 'setup.network.dns.dhcp': {'action': 'omarchy-dns DHCP',
                            'expression': '[[ "$(omarchy-dns)" == "DHCP" ]]',
                            'when': ''},
 'setup.network.dns.google': {'action': 'omarchy-dns Google',
                              'expression': '[[ "$(omarchy-dns)" == "Google" ]]',
                              'when': ''},
 'update.channel.dev': {'action': "omarchy-launch-floating-terminal-with-presentation 'omarchy-channel-set "
                                  "dev'",
                        'expression': '[[ "$(omarchy-channel-current)" == "dev" ]]',
                        'when': ''},
 'update.channel.edge': {'action': "omarchy-launch-floating-terminal-with-presentation 'omarchy-channel-set "
                                   "edge'",
                         'expression': '[[ "$(omarchy-channel-current)" == "edge" ]]',
                         'when': ''},
 'update.channel.rc': {'action': "omarchy-launch-floating-terminal-with-presentation 'omarchy-channel-set "
                                 "rc'",
                       'expression': '[[ "$(omarchy-channel-current)" == "rc" ]]',
                       'when': ''},
 'update.channel.stable': {'action': 'omarchy-launch-floating-terminal-with-presentation '
                                     "'omarchy-channel-set stable'",
                           'expression': '[[ "$(omarchy-channel-current)" == "stable" ]]',
                           'when': ''}}

QUICK_SOURCES = {'trigger.toggle.battery-percentage': {'action': 'omarchy-shell omarchy.power togglePercentage',
                                       'checked': '',
                                       'provider': '',
                                       'surface': '',
                                       'target': '',
                                       'when': 'omarchy-hw-laptop'},
 'trigger.toggle.crash-capture': {'action': 'omarchy-toggle-crash-capture',
                                  'checked': '',
                                  'provider': '',
                                  'surface': '',
                                  'target': '',
                                  'when': ''},
 'trigger.toggle.idle-lock': {'action': 'omarchy-toggle-idle',
                              'checked': '',
                              'provider': '',
                              'surface': '',
                              'target': '',
                              'when': ''},
 'trigger.toggle.nightlight': {'action': 'omarchy-toggle-nightlight',
                               'checked': '',
                               'provider': '',
                               'surface': '',
                               'target': '',
                               'when': ''},
 'trigger.toggle.notifications': {'action': 'omarchy-toggle-notification-silencing',
                                  'checked': '',
                                  'provider': '',
                                  'surface': '',
                                  'target': '',
                                  'when': ''},
 'trigger.toggle.one-window-ratio': {'action': 'omarchy-hyprland-window-single-square-aspect-toggle',
                                     'checked': '',
                                     'provider': '',
                                     'surface': '',
                                     'target': '',
                                     'when': ''},
 'trigger.toggle.screensaver': {'action': 'omarchy-toggle-screensaver',
                                'checked': '',
                                'provider': '',
                                'surface': '',
                                'target': '',
                                'when': ''},
 'trigger.toggle.top-bar': {'action': 'omarchy-toggle-bar',
                            'checked': '',
                            'provider': '',
                            'surface': '',
                            'target': '',
                            'when': ''},
 'trigger.toggle.window-gaps': {'action': 'omarchy-hyprland-window-gaps-toggle',
                                'checked': '',
                                'provider': '',
                                'surface': '',
                                'target': '',
                                'when': ''},
 'trigger.toggle.workspace-layout': {'action': 'omarchy-hyprland-workspace-layout-toggle',
                                     'checked': '',
                                     'provider': '',
                                     'surface': '',
                                     'target': '',
                                     'when': ''}}

GETTERS = {
    'agent': '/usr/share/omarchy/bin/omarchy-default-agent',
    'browser': '/usr/share/omarchy/bin/omarchy-default-browser',
    'terminal': '/usr/share/omarchy/bin/omarchy-default-terminal',
    'editor': '/usr/share/omarchy/bin/omarchy-default-editor',
    'dns': '/usr/share/omarchy/bin/omarchy-dns',
    'channel': '/usr/share/omarchy/bin/omarchy-channel-current',
}
PACKAGES = tuple(sorted({expression.split()[-1] for expression in REVIEWED_GUARDS.values()
                         if 'omarchy-pkg-present ' in expression}))
COMMANDS = frozenset(expression.split()[-1] for expression in REVIEWED_GUARDS.values()
                     if 'omarchy-cmd-present ' in expression)
HARDWARE = frozenset({'omarchy-hw-laptop','omarchy-hw-touchpad','omarchy-hw-touchscreen',
                     'omarchy-hw-hybrid-gpu','omarchy-hw-fingerprint','omarchy-hw-webcam',
                     'omarchy-hw-dell-xps-haptic-touchpad'})
FLAGS = {
    'idle-lock': ('indicators/stay-awake',False),
    'crash-capture': ('toggles/crash-capture-off',True),
    'screensaver': ('toggles/screensaver-off',True),
    'top-bar': ('toggles/bar-off',True),
    'window-gaps': ('toggles/hypr/window-no-gaps.lua',True),
    'one-window-ratio': ('toggles/hypr/single-window-aspect-ratio.lua',False),
}
NOTIFICATION_READ = ('/usr/share/omarchy/bin/omarchy-shell','notifications','isDnd')
# CORE-2 §3: the least time between two `qs` spawns that only read DND.
NOTIFICATION_ASK_SECONDS = 30.0
NOTIFICATION_TOGGLE = ('/usr/share/omarchy/bin/omarchy-shell','notifications','toggleDnd')
NOTIFICATION_REFRESH = ('/usr/share/omarchy/bin/omarchy-shell','-q','omarchy.indicators','refresh')
# `qs` is Qt: under LANG=C it logs its locale fallback to the user journal on
# every run (CORE-2 §3), so the three `omarchy-shell` calls get a UTF-8 C locale.
UTF8_COMMANDS = frozenset({NOTIFICATION_READ, NOTIFICATION_TOGGLE, NOTIFICATION_REFRESH})
IDLE_COMMAND = ('/usr/share/omarchy/bin/omarchy-toggle-idle',)
SCREENSAVER_COMMAND = ('/usr/share/omarchy/bin/omarchy-toggle-screensaver',)
BAR_COMMAND = ('/usr/share/omarchy/bin/omarchy-toggle-bar',)
BAR_REFRESH = ('/usr/share/omarchy/bin/omarchy-shell','-q','omarchy.bar','syncHidden')
HYPR_FLAG_SETTER = '/usr/share/omarchy/bin/omarchy-hyprland-toggle'
HYPR_QUICK_FLAGS = {
    'trigger.toggle.window-gaps': 'window-no-gaps',
    'trigger.toggle.one-window-ratio': 'single-window-aspect-ratio',
}
HYPR_QUICK_COMMANDS = {
    'trigger.toggle.window-gaps': ('/usr/share/omarchy/bin/omarchy-hyprland-window-gaps-toggle',),
    'trigger.toggle.one-window-ratio': ('/usr/share/omarchy/bin/omarchy-hyprland-window-single-square-aspect-toggle',),
}
HYPR_PACKAGED_BIN_LINKS = {path: '/usr/bin/'+Path(path).name for path in
    (HYPR_FLAG_SETTER, *(command[0] for command in HYPR_QUICK_COMMANDS.values()))}
FINITE_QUICK_COMMANDS = {
    'trigger.toggle.notifications': NOTIFICATION_TOGGLE,
    'trigger.toggle.idle-lock': IDLE_COMMAND,
    'trigger.toggle.screensaver': SCREENSAVER_COMMAND,
    'trigger.toggle.top-bar': BAR_COMMAND,
    **HYPR_QUICK_COMMANDS,
}
MUTATION_COMMANDS = frozenset({NOTIFICATION_TOGGLE, NOTIFICATION_REFRESH, BAR_REFRESH,
    *(IDLE_COMMAND + (mode,) for mode in ('stay-awake','allow-idle')),
    *(('/usr/share/omarchy/bin/omarchy-toggle',flag,mode)
      for flag in ('screensaver-off','bar-off') for mode in ('on','off')),
    *((HYPR_FLAG_SETTER,flag,mode) for flag in HYPR_QUICK_FLAGS.values() for mode in ('on','off'))})
NIGHTLIGHT_READ = ('/usr/share/omarchy/bin/omarchy-toggle-nightlight','--status')
WORKSPACE_READ = ('/usr/bin/hyprctl','-j','activeworkspace')
DEVICE_READ = ('/usr/bin/hyprctl','-j','devices')
PACKAGE_READ = ('/usr/bin/pacman','-T',*PACKAGES)
READ_COMMANDS = frozenset({*(tuple([path]) for path in GETTERS.values()),
    NOTIFICATION_READ,NIGHTLIGHT_READ,WORKSPACE_READ,DEVICE_READ,PACKAGE_READ,
    *(('/usr/share/omarchy/bin/'+name,) for name in HARDWARE if name not in {'omarchy-hw-touchpad','omarchy-hw-touchscreen'})})


class MenuReadUnavailable(ValueError):
    def __init__(self,code='live_state_unavailable'):
        self.code=code
        super().__init__(code)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ''
    error: str | None = None


def bounded_command(argv, environment, *, mutation=False, timeout=2.5, max_bytes=262144):
    """Fixed argv, bounded output/time, discarded stderr and no shell expansion."""
    allowed=MUTATION_COMMANDS if mutation else READ_COMMANDS
    if argv not in allowed:raise MenuReadUnavailable('command_not_reviewed')
    if mutation:
        from .catalog_providers import validate_action_context
        validate_action_context(environment,os.getuid(),Path.home())
        try:info=Path(argv[0]).stat()
        except OSError:raise MenuReadUnavailable('mutation_command_missing') from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or not os.access(argv[0],os.X_OK):
            raise MenuReadUnavailable('mutation_command_untrusted')
    process=None;selector=selectors.DefaultSelector();data=bytearray()
    deadline=time.monotonic()+timeout
    try:
        process=subprocess.Popen(argv,env=environment,shell=False,stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
        selector.register(process.stdout,selectors.EVENT_READ)
        while selector.get_map():
            remaining=deadline-time.monotonic()
            if remaining<=0:raise MenuReadUnavailable('reader_timeout')
            for key,_ in selector.select(remaining):
                chunk=os.read(key.fd,min(65536,max_bytes-len(data)+1))
                if not chunk:selector.unregister(key.fileobj)
                else:
                    data.extend(chunk)
                    if len(data)>max_bytes:raise MenuReadUnavailable('reader_output_limit')
        process.wait(timeout=max(.001,deadline-time.monotonic()))
        return CommandResult(process.returncode,data.decode('utf-8'))
    except FileNotFoundError:return CommandResult(127,error='reader_missing')
    except MenuReadUnavailable as exc:return CommandResult(124,error=exc.code)
    except (OSError,UnicodeError,subprocess.SubprocessError):return CommandResult(1,error='reader_failed')
    finally:
        selector.close()
        if process and process.poll() is None:
            try:os.killpg(process.pid,9)
            except (ProcessLookupError,PermissionError):
                if process.poll() is None:process.kill()
            process.wait(timeout=1)
        if process and process.stdout:process.stdout.close()


def available(value,reason=None):
    return {'status':'available','value':value,**({'reason':reason} if reason else {})}


def unavailable(code):return {'status':'unavailable','value':None,'reason':code}


class LiveMenuReaders:
    def __init__(self, *, home: Path | None = None, runner=None, mutation_runner=None,
                 graphical=None, clock=time.monotonic, cache_seconds=1.5):
        if type(cache_seconds) not in {int,float} or not math.isfinite(cache_seconds) or not 0<=cache_seconds<=10:
            raise ValueError('invalid cache TTL')
        self.home=home or Path.home();self.runner=runner or bounded_command
        self._default_mutation_runner = mutation_runner is None
        self.mutation_runner=mutation_runner or (lambda argv,env:bounded_command(argv,env,mutation=True))
        self.graphical=graphical or graphical_environment
        self.clock,self.cache_seconds=clock,cache_seconds;self._cache={}
        # CORE-2 §3: (when, answer-or-error) of the last time the shell itself
        # had to be asked for DND because its state file could not say.
        self._notification_asked=None
        # PERF-4 §0. Two dozen `when` expressions in the Omarchy menu run the
        # same handful of scripts - fourteen of them are
        # `[[ "$(omarchy-default-browser)" == "…" ]]` with a different name on
        # the right - and this cache is what makes that one process instead of
        # fourteen. Since the catalog runtime reads the cold expressions
        # concurrently, they would otherwise all miss it at once and each spawn
        # its own. One lock per key: the first caller reads, the rest wait for
        # it and take the answer.
        self._locks={};self._locks_guard=threading.Lock()
        self.command_path=':'.join(str(self.home/path) for path in ['.local/share/mise/shims','.local/bin'])+':/usr/local/bin:/usr/bin:/bin:/usr/share/omarchy/bin'

    def environment(self, *, required_graphical=False, utf8=False):
        locale='C.UTF-8' if utf8 else 'C'
        env={'HOME':str(self.home),'PATH':'/usr/local/bin:/usr/bin:/bin:/usr/share/omarchy/bin',
             'LANG':locale,'LC_ALL':locale,'OMARCHY_PATH':'/usr/share/omarchy'}
        try:env.update(self.graphical())
        except (OSError,ValueError):
            if required_graphical:raise MenuReadUnavailable('graphical_session_unavailable')
        env['PATH']='/usr/local/bin:/usr/bin:/bin:/usr/share/omarchy/bin'
        env['OMARCHY_PATH']='/usr/share/omarchy';env['LANG']=env['LC_ALL']=locale
        if 'XDG_RUNTIME_DIR' in env:
            env['DBUS_SESSION_BUS_ADDRESS']='unix:path='+env['XDG_RUNTIME_DIR']+'/bus'
            env['XDG_CURRENT_DESKTOP']='Hyprland';env['XDG_SESSION_TYPE']='wayland'
        return env

    def invalidate(self):self._cache.clear()

    def _lock_for(self,key):
        with self._locks_guard:
            lock=self._locks.get(key)
            if lock is None:lock=self._locks[key]=threading.Lock()
            return lock

    def cached(self,key,callback):
        cached=self._cache.get(key)
        if cached and self.clock()-cached[0]<=self.cache_seconds:
            value=cached[1]
        else:
            with self._lock_for(key):
                cached=self._cache.get(key)          # somebody may have read it while we waited
                if cached and self.clock()-cached[0]<=self.cache_seconds:
                    value=cached[1]
                else:
                    try:value=callback()
                    except Exception as exc:value=MenuReadUnavailable(exc.code if isinstance(exc,MenuReadUnavailable) else 'reader_failed')
                    self._cache[key]=(self.clock(),value)
        if isinstance(value,Exception):raise value
        return deepcopy(value)

    def read(self,argv,*,graphical=False):
        if argv not in READ_COMMANDS:raise MenuReadUnavailable('command_not_reviewed')
        result=self.runner(argv,self.environment(required_graphical=graphical,utf8=argv in UTF8_COMMANDS))
        if (not isinstance(result,CommandResult) or type(result.returncode) is not int
                or not isinstance(result.stdout,str) or len(result.stdout.encode())>262144 or result.error):
            raise MenuReadUnavailable(getattr(result,'error',None) or 'reader_failed')
        return result

    def getter(self,family):
        if family not in GETTERS:raise MenuReadUnavailable('getter_not_reviewed')
        def read():
            result=self.read((GETTERS[family],))
            if result.returncode:raise MenuReadUnavailable('getter_unavailable')
            value=result.stdout.strip()
            if len(value)>128 or value and not re.fullmatch(r'[A-Za-z0-9_.:+-]+',value):
                raise MenuReadUnavailable('getter_invalid')
            if family=='channel' and value not in {'stable','rc','edge','dev'}:
                raise MenuReadUnavailable('channel_unknown')
            if family=='dns' and value not in {'DHCP','Cloudflare','Google','Custom'}:
                raise MenuReadUnavailable('dns_unknown')
            return value
        return self.cached('getter:'+family,read)

    def package_presence(self):
        def read():
            result=self.read(PACKAGE_READ)
            # pacman -T uses dependency satisfaction (including Provides),
            # unlike querying installed package names alone. 127 lists unmet
            # dependencies; a missing executable is separately an error.
            if result.returncode not in {0,127}:raise MenuReadUnavailable('package_probe_failed')
            missing=set(result.stdout.splitlines())
            if not missing<=set(PACKAGES) or result.returncode==0 and missing or result.returncode==127 and not missing:
                raise MenuReadUnavailable('package_probe_invalid')
            return {package:package not in missing for package in PACKAGES}
        return self.cached('packages',read)

    def command_present(self,name):
        if name not in COMMANDS:raise MenuReadUnavailable('command_not_reviewed')
        # Only existence/executable metadata is inspected. The discovered
        # executable is never invoked by this predicate.
        return self.cached('command:'+name,lambda:shutil.which(name,path=self.command_path) is not None)

    def hardware(self,name):
        if name not in HARDWARE:raise MenuReadUnavailable('hardware_not_reviewed')
        def read():
            if name in {'omarchy-hw-touchpad','omarchy-hw-touchscreen'}:
                result=self.read(DEVICE_READ,graphical=True)
                if result.returncode:raise MenuReadUnavailable('devices_unavailable')
                value=json.loads(result.stdout)
                if not isinstance(value,dict) or not isinstance(value.get('mice'),list):
                    raise MenuReadUnavailable('devices_invalid')
                if name=='omarchy-hw-touchpad':
                    return any(isinstance(row,dict) and isinstance(row.get('name'),str)
                               and re.search('touchpad|trackpad',row['name'],re.I) for row in value['mice'])
                for field in ('touch','tablets'):
                    if field in value and not isinstance(value[field],list):raise MenuReadUnavailable('devices_invalid')
                return bool(value.get('touch') or value.get('tablets'))
            result=self.read(('/usr/share/omarchy/bin/'+name,))
            if result.returncode not in {0,1}:raise MenuReadUnavailable('hardware_probe_failed')
            return result.returncode==0
        return self.cached('hardware:'+name,read)

    def guard(self,entry_id,expression):
        if REVIEWED_GUARDS.get(entry_id)!=expression:raise MenuReadUnavailable('guard_not_reviewed')
        if expression=='omarchy-hw-dell-xps-haptic-touchpad && omarchy-cmd-present dell-xps-touchpad-haptics':
            return self.hardware('omarchy-hw-dell-xps-haptic-touchpad') and self.command_present('dell-xps-touchpad-haptics')
        if expression in HARDWARE:return self.hardware(expression)
        tokens=expression.split();negative=tokens[0]=='!'
        if negative:tokens=tokens[1:]
        if len(tokens)!=2:raise MenuReadUnavailable('guard_not_reviewed')
        family,name=tokens
        if family=='omarchy-pkg-present':result=self.package_presence()[name]
        elif family=='omarchy-cmd-present':result=self.command_present(name)
        else:raise MenuReadUnavailable('guard_not_reviewed')
        return not result if negative else result

    def default_checked(self,entry_id,expression):
        reviewed=REVIEWED_CHECKS.get(entry_id)
        if reviewed is None or reviewed['expression']!=expression:raise MenuReadUnavailable('checked_not_reviewed')
        match=re.fullmatch(r'\[\[ "\$\((omarchy-[a-z-]+)\)" == "([A-Za-z0-9_-]+)" \]\]',expression)
        if not match:raise MenuReadUnavailable('checked_not_reviewed')
        family=next((family for family,path in GETTERS.items() if Path(path).name==match[1]),None)
        if family is None:raise MenuReadUnavailable('getter_not_reviewed')
        return self.getter(family)==match[2]

    def notification_enabled(self, *, fresh=False):
        """Notifications on = DND off, read from the shell's own state file.

        CORE-2 §3 (G17): this was `omarchy-shell notifications isDnd` - one `qs`
        process, under LANG=C, for every refresh of the menu's checked states,
        which on Leo's host was a spawn and four journal lines every ~16 s. The
        shell writes the value to `notifications.json` 200 ms after it changes
        (`notifications.read_dnd_file`), so the file answers. Only when it
        cannot (never toggled on this install, unreadable) is the shell asked,
        and then at most every 30 s and with a UTF-8 locale.
        """
        if fresh:self._cache.pop('notification',None)
        def read():
            from .notifications import read_dnd_file
            value=read_dnd_file(self.home/'.local/state/omarchy/notifications.json')
            if value is not None:return not value
            # A fresh read is a person's tap, not the poll: it may ask.
            return self._asked_notification_enabled(force=fresh)
        return self.cached('notification',read)

    def _asked_notification_enabled(self,*,force=False):
        asked=self._notification_asked
        if not force and asked is not None and self.clock()-asked[0]<NOTIFICATION_ASK_SECONDS:
            if isinstance(asked[1],Exception):raise asked[1]
            return asked[1]
        try:
            result=self.read(NOTIFICATION_READ,graphical=True)
            value=result.stdout.strip()
            if result.returncode or value not in {'on','off'}:raise MenuReadUnavailable('notifications_unavailable')
            answer=value=='off' # enabled notifications are the inverse of DND.
        except MenuReadUnavailable as exc:
            self._notification_asked=(self.clock(),exc)
            raise
        self._notification_asked=(self.clock(),answer)
        return answer

    def _flag(self,suffix):
        path=self.home/'.local/state/omarchy'/suffix
        try:info=path.stat()
        except FileNotFoundError:return False
        return stat.S_ISREG(info.st_mode)

    def _percentage(self):
        path=self.home/'.config/omarchy/shell.json'
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NONBLOCK',0))
        with os.fdopen(fd,'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise MenuReadUnavailable('shell_config_invalid')
            raw=stream.read(65537)
        if len(raw)>65536:raise MenuReadUnavailable('shell_config_too_large')
        config=json.loads(raw);layout=config.get('bar',{}).get('layout',{})
        entries=[]
        for section in ('left','center','right'):
            value=layout.get(section,[])
            if not isinstance(value,list):raise MenuReadUnavailable('shell_config_invalid')
            entries.extend(value)
        if isinstance(config.get('plugins',[]),list):entries.extend(config.get('plugins',[]))
        values=[]
        for row in entries:
            if row=='omarchy.power':values.append(False)
            elif isinstance(row,dict) and row.get('id')=='omarchy.power':
                # QML setting('showPercentage',false) === true.
                values.append(row.get('showPercentage') is True)
        if not values:raise MenuReadUnavailable('power_widget_unavailable')
        if len(set(values))!=1:raise MenuReadUnavailable('power_widget_ambiguous')
        return values[0]

    def quick_state(self,entry_id):
        if entry_id not in QUICK_SOURCES:return unavailable('state_not_reviewed')
        name=entry_id.removeprefix('trigger.toggle.')
        try:
            if name in FLAGS:
                suffix,invert=FLAGS[name];value=self.cached('flag:'+name,lambda:self._flag(suffix))
                return available(not value if invert else value,'configuration_intent')
            if name=='notifications':return available(self.notification_enabled())
            if name=='battery-percentage':return available(self.cached('percentage',self._percentage))
            if name=='nightlight':
                def read():
                    result=self.read(NIGHTLIGHT_READ,graphical=True)
                    if result.returncode:raise MenuReadUnavailable('nightlight_unavailable')
                    data=json.loads(result.stdout);temperature=data.get('temperature')
                    if type(temperature) not in {int,float} or not math.isfinite(temperature) or not 1000<=temperature<=25000:
                        raise MenuReadUnavailable('nightlight_temperature_unknown')
                    return temperature<6000
                return available(self.cached('nightlight',read))
            if name=='workspace-layout':
                def read():
                    result=self.read(WORKSPACE_READ,graphical=True)
                    if result.returncode:raise MenuReadUnavailable('workspace_unavailable')
                    value=json.loads(result.stdout).get('tiledLayout')
                    if value not in {'dwindle','scrolling'}:raise MenuReadUnavailable('workspace_layout_unknown')
                    return value
                layout=self.cached('workspace-layout',read)
                # Existing checked contract is bool|null: keep this enum honest
                # in a bounded reason, not a made-up checked bool or top-level map.
                return available(None,'workspace_layout_'+layout)
        except Exception as exc:
            return unavailable(exc.code if isinstance(exc,MenuReadUnavailable) else 'state_reader_failed')
        return unavailable('state_not_reviewed')

    def can_execute(self, entry_id):
        if entry_id not in FINITE_QUICK_COMMANDS:return False
        try:
            environment=self.environment(required_graphical=True)
            if self._default_mutation_runner:
                from .catalog_providers import validate_action_context
                validate_action_context(environment,os.getuid(),Path.home())
                executable=FINITE_QUICK_COMMANDS[entry_id][0]
                info=Path(executable).stat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or not os.access(executable,os.X_OK):return False
            name=entry_id.removeprefix('trigger.toggle.')
            if name in FLAGS:self._validate_flag_target(FLAGS[name][0])
            if entry_id in HYPR_QUICK_FLAGS:self._validate_hypr_assets(entry_id)
            return True
        except (OSError,ValueError):return False

    def _validate_hypr_assets(self, entry_id):
        # The fixed packaged setter copies one reviewed template and reloads
        # Hyprland. No client-supplied flag name, Lua text or path is accepted.
        flag=HYPR_QUICK_FLAGS.get(entry_id)
        if flag is None:raise MenuReadUnavailable('command_not_reviewed')
        if not self._default_mutation_runner:return
        for path,executable in ((Path(HYPR_QUICK_COMMANDS[entry_id][0]),True),
                (Path(HYPR_FLAG_SETTER),True),
                (Path('/usr/share/omarchy/default/hypr/toggles')/(flag+'.lua'),False)):
            self._validate_packaged_hypr_file(path,executable=executable)

    @staticmethod
    def _validate_packaged_hypr_file(path, *, executable):
        def inspect(candidate):
            try:
                # A trusted leaf under a writable or redirected directory is
                # insufficient: verify every containing directory as well.
                for parent in reversed(candidate.parents):
                    info=parent.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
                        raise MenuReadUnavailable('hypr_toggle_asset_untrusted')
                return candidate.lstat()
            except OSError:raise MenuReadUnavailable('hypr_toggle_asset_missing') from None
        info=inspect(path)
        if stat.S_ISLNK(info.st_mode):
            # Arch packages install exactly these aliases into Omarchy's bin
            # directory. Only a root-owned link to the same /usr/bin name is
            # accepted; no arbitrary resolution, link chain or template link.
            expected=HYPR_PACKAGED_BIN_LINKS.get(str(path)) if executable else None
            if info.st_uid!=0 or not expected:
                raise MenuReadUnavailable('hypr_toggle_asset_untrusted')
            try:target=os.readlink(path)
            except OSError:raise MenuReadUnavailable('hypr_toggle_asset_missing') from None
            if target!=expected:raise MenuReadUnavailable('hypr_toggle_asset_untrusted')
            path=Path(expected);info=inspect(path)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022
                or executable and not os.access(path,os.X_OK)):
            raise MenuReadUnavailable('hypr_toggle_asset_untrusted')

    def _validate_flag_target(self, suffix):
        # Packaged helpers touch/remove a fixed flag. Reject symlink or foreign
        # ownership anywhere below HOME before letting those scripts write.
        target=self.home/'.local/state/omarchy'/suffix
        relative=target.relative_to(self.home)
        cursor=self.home
        for part in relative.parts:
            cursor=cursor/part
            try:info=cursor.lstat()
            except FileNotFoundError:continue
            if stat.S_ISLNK(info.st_mode) or info.st_uid!=os.getuid():
                raise MenuReadUnavailable('toggle_target_untrusted')
            if cursor!=target and not stat.S_ISDIR(info.st_mode):
                raise MenuReadUnavailable('toggle_target_untrusted')
            if cursor==target and not stat.S_ISREG(info.st_mode):
                raise MenuReadUnavailable('toggle_target_untrusted')

    def execute_quick(self, entry_id, argv):
        if FINITE_QUICK_COMMANDS.get(entry_id)!=tuple(argv):raise MenuReadUnavailable('command_not_reviewed')
        if entry_id=='trigger.toggle.notifications':return self.toggle_notifications(tuple(argv))
        self.invalidate()
        before=self.quick_state(entry_id)
        if before['status']!='available' or type(before['value']) is not bool:
            raise MenuReadUnavailable('toggle_state_unavailable')
        name=entry_id.removeprefix('trigger.toggle.')
        suffix,_=FLAGS[name]
        self._validate_flag_target(suffix)
        desired=not before['value']
        if name=='idle-lock':
            command=IDLE_COMMAND+(('stay-awake' if desired else 'allow-idle'),)
        elif entry_id in HYPR_QUICK_FLAGS:
            self._validate_hypr_assets(entry_id)
            # Gaps On means the no-gaps flag is absent; square-ratio On means
            # its flag is present. Explicit on/off avoids another blind toggle.
            flag_present=not desired if FLAGS[name][1] else desired
            command=(HYPR_FLAG_SETTER,HYPR_QUICK_FLAGS[entry_id],'on' if flag_present else 'off')
        else:
            flag='screensaver-off' if name=='screensaver' else 'bar-off'
            command=('/usr/share/omarchy/bin/omarchy-toggle',flag,'off' if desired else 'on')
        result=self.mutation_runner(command,self.environment(required_graphical=True))
        self.invalidate()
        if not isinstance(result,CommandResult) or result.returncode or result.error:
            raise MenuReadUnavailable('toggle_set_failed')
        after=self.quick_state(entry_id)
        if after['status']!='available' or after['value'] is not desired:
            raise MenuReadUnavailable('toggle_readback_failed')
        if name=='top-bar':
            refreshed=self.mutation_runner(BAR_REFRESH,self.environment(required_graphical=True))
            if not isinstance(refreshed,CommandResult) or refreshed.returncode or refreshed.error:
                raise MenuReadUnavailable('bar_refresh_failed')
        return {'checked_state':desired}

    def toggle_notifications(self,argv):
        if argv!=NOTIFICATION_TOGGLE:raise MenuReadUnavailable('command_not_reviewed')
        before=self.notification_enabled(fresh=True)
        result=self.mutation_runner(argv,self.environment(required_graphical=True,utf8=True))
        self._cache.pop('notification',None)
        if not isinstance(result,CommandResult) or result.returncode or result.error:
            raise MenuReadUnavailable('notification_toggle_failed')
        # CORE-2 §3: `toggleDnd` answers with the state it just set. The state
        # file lags that by the shell's 200 ms save timer, so reading it back
        # here would see the old value; the answer is the read-back.
        answer=(result.stdout or '').strip()
        if answer in {'on','off'}:
            after=answer=='off'
            self._cache['notification']=(self.clock(),after)
            self._notification_asked=(self.clock(),after)
        else:
            after=self._asked_notification_enabled(force=True)
        if after==before:raise MenuReadUnavailable('notification_readback_failed')
        # Refresh only the official indicator; no notification history/content.
        self.mutation_runner(NOTIFICATION_REFRESH,self.environment(required_graphical=True,utf8=True))
        return {'checked_state':after}


def install_live_menu_adapters(service, *, readers=None):
    """One bootstrap handoff call; does not toggle, launch, install, or remove.

    The existing source refresh continues to use the same CatalogRuntime. The
    hooks participate in its normal cache/invalidation and service WSS revision.
    Calling this function again with the same service is a no-op.
    """
    if hasattr(service,'live_menu_readers'):return service.live_menu_readers
    readers=readers or LiveMenuReaders()
    runtime=service.runtime
    for entry in runtime.catalog.entries:
        row=entry.as_dict();entry_id=row['id'];expression=row.get('when')
        if expression and REVIEWED_GUARDS.get(entry_id)==expression:
            runtime.register_condition(expression,lambda entry_id=entry_id,expression=expression:readers.guard(entry_id,expression))
        reviewed=REVIEWED_CHECKS.get(entry_id)
        if reviewed and row.get('checked')==reviewed['expression']:
            expression=reviewed['expression']
            def read_checked(entry_id=entry_id,expression=expression,reviewed=deepcopy(reviewed)):
                current=runtime.catalog.by_id(entry_id)
                current=current.as_dict() if current else {}
                if (current.get('checked')!=expression or current.get('action')!=reviewed['action']
                        or (current.get('when') or '')!=reviewed['when']
                        or any(current.get(field) for field in ('target','provider','surface'))):
                    raise MenuReadUnavailable('state_adapter_source_changed')
                return readers.default_checked(entry_id,expression)
            runtime.register_condition(expression,read_checked)
    for entry_id,source in QUICK_SOURCES.items():
        runtime.register_checked_state(entry_id,lambda entry_id=entry_id:readers.quick_state(entry_id),reviewed_source=source)
    for entry_id,command in FINITE_QUICK_COMMANDS.items():
        def available_to_invoke(row,entry_id=entry_id):
            state=row.get('conditions',{}).get('checked',{})
            return None if state.get('status')=='available' and type(state.get('value')) is bool and readers.can_execute(entry_id) else 'unknown'
        service.policy.register(entry_id,RouteDescriptor('host',True,argv=command),
                                source_action=QUICK_SOURCES[entry_id]['action'],
                                reviewed_source=QUICK_SOURCES[entry_id],availability=available_to_invoke)
        service.register_executor(entry_id,lambda argv,entry_id=entry_id:readers.execute_quick(entry_id,argv))
    from .workspace_layout import install_workspace_layout_adapter
    install_workspace_layout_adapter(service,readers=readers)
    service.live_menu_readers=readers
    return readers
