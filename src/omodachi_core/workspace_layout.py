"""Finite, workspace-bound layout actions; no current-workspace shell toggle.

The service validates captured state revision before acceptance. This adapter
checks the captured workspace/layout again, invokes one of 20 fixed commands,
and persists only known generated content after a target-specific readback.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time

from .routes import RouteDescriptor

ENTRY = 'trigger.toggle.workspace-layout'
LAYOUTS = ('dwindle', 'scrolling')
WORKSPACE_IDS = tuple(range(1, 11))
MARKER = 'omodachi.workspace-layout'
ACTIVE_READ = ('/usr/bin/hyprctl', '-j', 'activeworkspace')
WORKSPACES_READ = ('/usr/bin/hyprctl', '-j', 'workspaces')
LAYOUT_COMMANDS = {(number, layout): ('/usr/bin/hyprctl', 'eval',
    f'hl.workspace_rule({{ workspace = "{number}", layout = "{layout}" }})')
    for number in WORKSPACE_IDS for layout in LAYOUTS}
READ_COMMANDS = frozenset((ACTIVE_READ, WORKSPACES_READ))
MUTATION_COMMANDS = frozenset(LAYOUT_COMMANDS.values())
DIRECTORY_PARTS = ('.local', 'state', 'omarchy', 'workspace-layouts')
SOURCE = {'action': 'omarchy-hyprland-workspace-layout-toggle', 'when': '',
          'checked': '', 'target': '', 'provider': '', 'surface': ''}


class WorkspaceLayoutError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WorkspaceCommandResult:
    returncode: int
    stdout: str = ''
    error: str | None = None


def run_workspace_command(argv, environment, *, mutation=False, timeout=.8, max_bytes=262144):
    allowed = MUTATION_COMMANDS if mutation else READ_COMMANDS
    if tuple(argv) not in allowed:
        raise WorkspaceLayoutError('workspace_layout_command_unreviewed')
    process = None; selector = selectors.DefaultSelector(); data = bytearray()
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(argv, env=environment, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline-time.monotonic()
            if remaining <= 0:raise WorkspaceLayoutError('workspace_layout_command_timeout')
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, min(65536, max_bytes-len(data)+1))
                if not chunk:selector.unregister(key.fileobj)
                else:
                    data.extend(chunk)
                    if len(data) > max_bytes:raise WorkspaceLayoutError('workspace_layout_output_limit')
        process.wait(timeout=max(.001, deadline-time.monotonic()))
        return WorkspaceCommandResult(process.returncode, data.decode('utf-8'))
    except WorkspaceLayoutError as exc:return WorkspaceCommandResult(124, error=exc.code)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return WorkspaceCommandResult(1, error='workspace_layout_command_failed')
    finally:
        selector.close()
        if process and process.poll() is None:
            try:os.killpg(process.pid, 9)
            except (ProcessLookupError, PermissionError):
                if process.poll() is None:process.kill()
            process.wait(timeout=1)
        if process and process.stdout:process.stdout.close()


def known_body(workspace_id, layout):
    if type(workspace_id) is not int or workspace_id not in WORKSPACE_IDS or layout not in LAYOUTS:
        raise WorkspaceLayoutError('workspace_layout_invalid_target')
    return (f'hl.workspace_rule({{ workspace = "{workspace_id}", layout = "{layout}" }})\n').encode()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode))


def atomic_exchange(directory_fd, first, second):
    """Swap two names without discarding either inode, using the host libc."""
    if not all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name) for name in (first, second)):
        raise WorkspaceLayoutError('workspace_layout_invalid_target')
    name = 'renameat2' if sys.platform.startswith('linux') else 'renameatx_np' if sys.platform == 'darwin' else None
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, name, None) if name else None
    if function is None:raise WorkspaceLayoutError('workspace_layout_atomic_exchange_unavailable')
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    # Linux RENAME_EXCHANGE and macOS RENAME_SWAP are both 0x2.
    if function(directory_fd, os.fsencode(first), directory_fd, os.fsencode(second), 2) != 0:
        raise WorkspaceLayoutError('workspace_layout_atomic_exchange_failed')


class WorkspaceLayoutAdapter:
    def __init__(self, *, readers=None, runner=None, home=None):
        if readers is None:
            from .live_menu_adapter import LiveMenuReaders
            readers = LiveMenuReaders()
        self.readers = readers
        self.home = Path(home) if home is not None else readers.home
        self.runner = runner or run_workspace_command
        self._default_runner = runner is None

    def _environment(self):
        environment = self.readers.environment(required_graphical=True)
        if self._default_runner:
            from .catalog_providers import validate_action_context
            validate_action_context(environment, os.getuid(), self.home)
            self.readers._validate_packaged_hypr_file(Path('/usr/bin/hyprctl'), executable=True)
        return environment

    def _command(self, argv, *, mutation=False):
        allowed = MUTATION_COMMANDS if mutation else READ_COMMANDS
        if tuple(argv) not in allowed:raise WorkspaceLayoutError('workspace_layout_command_unreviewed')
        result = self.runner(tuple(argv), self._environment(), mutation=mutation)
        if (not isinstance(result, WorkspaceCommandResult) or type(result.returncode) is not int
                or not isinstance(result.stdout, str) or len(result.stdout.encode()) > 262144):
            raise WorkspaceLayoutError('workspace_layout_command_invalid')
        if result.error or result.returncode:
            raise WorkspaceLayoutError('workspace_layout_command_failed')
        return result.stdout

    def active_workspace(self):
        """Fresh single read: do not split ID/layout across compositor requests."""
        try:value = json.loads(self._command(ACTIVE_READ))
        except (ValueError, TypeError):raise WorkspaceLayoutError('workspace_layout_state_unavailable') from None
        if (not isinstance(value, dict) or type(value.get('id')) is not int
                or value['id'] not in WORKSPACE_IDS or value.get('tiledLayout') not in LAYOUTS):
            raise WorkspaceLayoutError('workspace_layout_state_unavailable')
        return {'workspace_id': value['id'], 'layout': value['tiledLayout']}

    def _target_layout(self, workspace_id):
        try:rows = json.loads(self._command(WORKSPACES_READ))
        except (ValueError, TypeError):raise WorkspaceLayoutError('workspace_layout_readback_unavailable') from None
        if not isinstance(rows, list) or len(rows) > 4096:
            raise WorkspaceLayoutError('workspace_layout_readback_unavailable')
        matches = [row for row in rows if isinstance(row, dict) and type(row.get('id')) is int and row['id'] == workspace_id]
        if len(matches) != 1 or matches[0].get('tiledLayout') not in LAYOUTS:
            raise WorkspaceLayoutError('workspace_layout_readback_unavailable')
        return matches[0]['tiledLayout']

    def _directory(self, *, create=False):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = None
        try:
            fd = os.open(self.home, flags)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
                raise WorkspaceLayoutError('workspace_layout_path_untrusted')
            for part in DIRECTORY_PARTS:
                try:child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        os.close(fd);return None
                    os.mkdir(part, 0o700, dir_fd=fd)
                    child = os.open(part, flags, dir_fd=fd)
                info = os.fstat(child)
                if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
                    os.close(child);raise WorkspaceLayoutError('workspace_layout_path_untrusted')
                os.close(fd);fd = child
            return fd
        except WorkspaceLayoutError:
            if fd is not None:os.close(fd)
            raise
        except OSError:
            if fd is not None:os.close(fd)
            raise WorkspaceLayoutError('workspace_layout_path_untrusted') from None

    def _snapshot(self, fd, workspace_id, *, filename=None):
        if fd is None:return None
        filename = filename or str(workspace_id)+'.lua'
        handle = None
        try:
            handle = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            before = os.fstat(handle)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o022 or before.st_nlink != 1):
                raise WorkspaceLayoutError('workspace_layout_configuration_conflict')
            body = os.read(handle, 1025)
            after = os.fstat(handle)
            if _identity(before) != _identity(after) or body not in [known_body(workspace_id, layout) for layout in LAYOUTS]:
                raise WorkspaceLayoutError('workspace_layout_configuration_conflict')
            return (_identity(after), hashlib.sha256(body).hexdigest())
        except FileNotFoundError:return None
        except OSError:raise WorkspaceLayoutError('workspace_layout_configuration_conflict') from None
        finally:
            if handle is not None:os.close(handle)

    def can_execute(self):
        fd = None
        try:
            current = self.active_workspace()
            fd = self._directory()
            self._snapshot(fd, current['workspace_id'])
            return True
        except (OSError, ValueError):return False
        finally:
            if fd is not None:os.close(fd)

    def _prepare(self, fd, workspace_id, layout):
        name = '.omodachi-layout-'+secrets.token_hex(16)+'.tmp'
        handle = None; identity = None
        try:
            handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            opened = os.fstat(handle);identity = (opened.st_dev, opened.st_ino)
            body = known_body(workspace_id, layout);offset = 0
            while offset < len(body):
                count = os.write(handle, body[offset:])
                if count <= 0:raise OSError('short write')
                offset += count
            os.fchmod(handle, 0o644);os.fsync(handle)
            info = os.fstat(handle)
            return name, (info.st_dev, info.st_ino)
        except OSError:
            self._cleanup_temporary(fd,name,identity)
            raise WorkspaceLayoutError('workspace_layout_prepare_failed') from None
        finally:
            if handle is not None:os.close(handle)

    def _commit(self, fd, workspace_id, temporary, expected):
        current_fd = self._directory()
        filename = str(workspace_id)+'.lua'
        staged = os.stat(temporary, dir_fd=fd, follow_symlinks=False)
        staged_identity = (staged.st_dev, staged.st_ino)
        try:
            if current_fd is None or (os.fstat(current_fd).st_dev, os.fstat(current_fd).st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                raise WorkspaceLayoutError('workspace_layout_persistence_conflict')
            if self._snapshot(fd, workspace_id) != expected:
                raise WorkspaceLayoutError('workspace_layout_persistence_conflict')
            if expected is None:
                # Atomic no-replace creation. A new external file wins and is
                # preserved even when it appears after our final check.
                os.link(temporary, filename, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                self._cleanup_temporary(fd, temporary, staged_identity)
            else:
                # Exchange retains the actual displaced inode. If another
                # writer changed the target after the check, restore it when
                # our staged inode is still current; otherwise retain both
                # files and report partial failure. Never delete unknown data.
                atomic_exchange(fd, temporary, filename)
                try:
                    previous = self._snapshot(fd, workspace_id, filename=temporary)
                    current = os.stat(filename, dir_fd=fd, follow_symlinks=False)
                    if previous != expected or (current.st_dev, current.st_ino) != staged_identity:
                        raise WorkspaceLayoutError('workspace_layout_persistence_conflict')
                except (OSError, ValueError):
                    try:
                        current = os.stat(filename, dir_fd=fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == staged_identity:
                            atomic_exchange(fd, temporary, filename)
                    except (OSError, ValueError):pass
                    raise WorkspaceLayoutError('workspace_layout_persistence_conflict_preserved') from None
                self._cleanup_temporary(fd, temporary, (expected[0][0], expected[0][1]))
            os.fsync(fd)
        except FileExistsError:raise WorkspaceLayoutError('workspace_layout_persistence_conflict') from None
        except OSError:raise WorkspaceLayoutError('workspace_layout_persistence_failed') from None
        finally:
            if current_fd is not None:os.close(current_fd)

    @staticmethod
    def _cleanup_temporary(fd, temporary, identity):
        if fd is None or not temporary or identity is None:return
        try:
            info = os.stat(temporary, dir_fd=fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == identity:os.unlink(temporary, dir_fd=fd)
        except OSError:pass

    def execute(self, argv, context):
        workspace_id = getattr(context, 'workspace_id', None)
        from_layout = getattr(context, 'from_layout', None);layout = getattr(context, 'layout', None)
        state_revision = getattr(context, 'state_revision', None)
        if (type(workspace_id) is not int or workspace_id not in WORKSPACE_IDS
                or type(state_revision) is not int or state_revision < 0
                or from_layout not in LAYOUTS or layout not in LAYOUTS or from_layout == layout
                or tuple(argv) != (MARKER, str(workspace_id), from_layout, layout)):
            raise WorkspaceLayoutError('workspace_layout_invalid_target')
        effects = {'workspace_id': workspace_id, 'from_layout': from_layout, 'layout': layout,
            'runtime_applied': False, 'persistent_applied': False, 'readback_confirmed': False,
            'status': 'failed', 'code': 'workspace_layout_failed'}
        fd = None; temporary = None;identity = None;attempted = False
        try:
            if self.active_workspace() != {'workspace_id': workspace_id, 'layout': from_layout}:
                raise WorkspaceLayoutError('workspace_layout_stale_target')
            fd = self._directory()
            before = self._snapshot(fd, workspace_id)
            if fd is None:fd = self._directory(create=True)
            temporary, identity = self._prepare(fd, workspace_id, layout)
            # The callback was captured against an explicit workspace; never
            # consult another active workspace to choose the mutation target.
            attempted = True
            self._command(LAYOUT_COMMANDS[(workspace_id, layout)], mutation=True)
            for attempt in range(3):
                if self._target_layout(workspace_id) == layout:break
                if attempt == 2:raise WorkspaceLayoutError('workspace_layout_readback_mismatch')
                time.sleep(.02)
            effects.update(runtime_applied=True, readback_confirmed=True)
            self._commit(fd, workspace_id, temporary, before)
            saved = self._snapshot(fd, workspace_id)
            if saved is None or saved[1] != hashlib.sha256(known_body(workspace_id, layout)).hexdigest():
                raise WorkspaceLayoutError('workspace_layout_persistence_readback_failed')
            effects.update(persistent_applied=True, status='applied', code='workspace_layout_applied')
        except (OSError, ValueError) as exc:
            code = getattr(exc, 'code', 'workspace_layout_failed')
            if not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}', code):code = 'workspace_layout_failed'
            effects.update(status='partial' if attempted else 'failed', code=code)
        finally:
            self._cleanup_temporary(fd, temporary, identity)
            if fd is not None:os.close(fd)
            self.readers.invalidate()
        return effects


def install_workspace_layout_adapter(service, *, readers=None, adapter=None):
    if hasattr(service, 'workspace_layout_adapter'):return service.workspace_layout_adapter
    adapter = adapter or WorkspaceLayoutAdapter(readers=readers)
    service.policy.register(ENTRY, RouteDescriptor('host', True,
        argv=(MARKER, '{workspace_id}', '{from_layout}', '{layout}')),
        source_action=SOURCE['action'], reviewed_source=SOURCE,
        parameter_enums={'workspace_id':WORKSPACE_IDS, 'from_layout':LAYOUTS, 'layout':LAYOUTS},
        availability=lambda row: None if row.get('conditions', {}).get('checked', {}).get('reason') in
            {'workspace_layout_dwindle', 'workspace_layout_scrolling'} and adapter.can_execute() else 'unknown')
    service.register_executor(ENTRY, adapter.execute, requires_workspace=True,
                              workspace_preflight=adapter.active_workspace)
    service.workspace_layout_adapter = adapter
    return adapter
