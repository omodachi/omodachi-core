"""Voice uplink and dictation: no real Voxtype, no real audio device."""
import asyncio
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

import aiohttp

from omodachi_core.audio_uplink import AudioUplinkError, FORMAT, MicrophoneArbiter, SOURCE_NAME
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.voice import (LEVEL_FRAME, VoiceError, VoxtypeHost, audio_device, read_levels,
                                 set_audio_device)
from omodachi_core.voice_service import VoiceService

CONFIG = b"""# Voxtype Configuration
state_file = "auto"
engine = "sensevoice"

[hotkey]
enabled = false

[audio]
# Audio input device ("default" uses system default)
device = "default"

# Sample rate in Hz (whisper expects 16000)
sample_rate = 16000

[output]
mode = "clipboard"
device = "not-the-audio-one"
"""


class ConfigRewriteTests(unittest.TestCase):
    def test_only_the_audio_device_line_changes_and_restore_is_byte_identical(self):
        self.assertEqual(audio_device(CONFIG), "default")
        updated = set_audio_device(CONFIG, SOURCE_NAME)
        self.assertEqual(audio_device(updated), SOURCE_NAME)
        # The `device` under [output] is a different key in a different table.
        self.assertIn(b'device = "not-the-audio-one"', updated)
        self.assertEqual(len(CONFIG.splitlines()), len(updated.splitlines()))
        differing = [(a, b) for a, b in zip(CONFIG.splitlines(), updated.splitlines()) if a != b]
        self.assertEqual(differing, [(b'device = "default"', b'device = "omodachi_mic"')])
        self.assertEqual(set_audio_device(updated, "default"), CONFIG)

    def test_a_file_without_an_audio_device_line_is_left_alone(self):
        for document in (b"[output]\ndevice = \"x\"\n", b"[audio]\nsample_rate = 16000\n", b""):
            with self.assertRaises(VoiceError):
                set_audio_device(document, SOURCE_NAME)

    def test_the_device_name_is_not_a_place_to_put_anything(self):
        for name in ("a b", 'x"\ndevice = "y', "", "x" * 200):
            with self.assertRaises(VoiceError):
                set_audio_device(CONFIG, name)

    def test_backup_and_restore_round_trip_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_bytes(CONFIG)
            host = VoxtypeHost(config_path=path, runtime_dir=Path(directory) / "rt",
                               cache_dir=Path(directory) / "cache")
            result = host.point_at(SOURCE_NAME)
            self.assertEqual(result["previous_device"], "default")
            self.assertTrue(result["changed"])
            self.assertEqual(audio_device(path.read_bytes()), SOURCE_NAME)
            with self.assertRaisesRegex(VoiceError, "voice_dictation_already_active"):
                host.point_at(SOURCE_NAME)
            self.assertTrue(host.restore()["restored"])
            self.assertEqual(path.read_bytes(), CONFIG)
            self.assertFalse(host.backup_path.exists())
            self.assertEqual(host.restore(), {"restored": False, "reason": "no_backup"})


class FakeHost:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.runtime_dir = self.directory / "voxtype"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.config = self.directory / "config.toml"
        self.config.write_bytes(CONFIG)
        self.calls = []
        self.state_value = "idle"
        self.stop_result = {"status": "ok", "text": "make the bar taller", "chars": 19, "exit_code": 0}
        self.pointed = False
        self.device_names = ["default", "pipewire", SOURCE_NAME]
        self.default_name = "alsa_input.host_mic"
        self.routed = []

    def capabilities(self):
        return {"installed": True, "supported": True, "wait_supported": True, "service_active": True,
                "state": self.state_value, "device": audio_device(self.config.read_bytes()),
                "source_name": SOURCE_NAME, "devices": list(self.device_names),
                "names_sources": SOURCE_NAME in self.device_names,
                "levels": True, "install_command": None, "reason": None}

    def devices(self):
        return list(self.device_names)

    def route_capture(self, source_name):
        self.calls.append(("route_capture", source_name))
        self.routed.append(source_name)
        return {"moved": True, "index": 1}

    def default_source(self):
        return self.default_name

    def set_default_source(self, source_name):
        self.calls.append(("set_default_source", source_name))
        self.default_name = source_name

    def state(self):
        return self.state_value

    def point_at(self, device):
        self.calls.append(("point_at", device))
        original = self.config.read_bytes()
        self.config.write_bytes(set_audio_device(original, device))
        self.pointed = True
        return {"previous_device": audio_device(original), "device": device, "changed": True}

    def restore(self):
        self.calls.append(("restore",))
        if not self.pointed:
            return {"restored": False, "reason": "no_backup"}
        self.config.write_bytes(CONFIG)
        self.pointed = False
        return {"restored": True, "changed": True}

    def restart(self):
        self.calls.append(("restart",))

    def transcript_path(self):
        path = self.directory / "transcript.txt"
        path.write_text("")
        return path

    def record_start(self, transcript):
        self.calls.append(("record_start", transcript.name))
        self.state_value = "recording"

    def record_stop(self, transcript, timeout_seconds=120):
        self.calls.append(("record_stop", transcript.name))
        self.state_value = "idle"
        return dict(self.stop_result)

    def record_cancel(self):
        self.calls.append(("record_cancel",))


class FakeBackend:
    def __init__(self):
        self.frames = []
        self.ended = None
    def begin(self, *, generation):
        return {"state": "active", "generation": generation}
    def accept(self, frame):
        self.frames.append(frame)
        return len(self.frames) <= 3
    def end(self, *, generation, reason="session_end"):
        self.ended = reason
        return {"state": "closed", "cleanup_complete": True}


class Preferences:
    def __init__(self, enabled=True): self.enabled = enabled
    def get(self): return {"values": {"voice_uplink": self.enabled}}


class VoiceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = FakeHost(self.temp.name)
        self.backends = []
        def factory():
            backend = FakeBackend()
            self.backends.append(backend)
            return backend
        self.hub = Hub()
        self.preferences = Preferences()
        self.arbiter = MicrophoneArbiter()
        self.voice = VoiceService(self.hub, factory=factory, host=self.host, arbiter=self.arbiter,
                                  preferences=self.preferences, typing=self)
        self.typed = []

    async def asyncTearDown(self):
        await self.voice.close()
        self.temp.cleanup()

    def type(self, text):  # stands in for HostTyping
        self.typed.append(text)
        return {"delivered": "wtype", "chars": len(text)}

    async def test_dictation_requires_the_setting_and_a_live_uplink(self):
        self.preferences.enabled = False
        with self.assertRaisesRegex(VoiceError, "voice_uplink_disabled"):
            await self.voice.dictation_start("phone", {})
        self.preferences.enabled = True
        with self.assertRaisesRegex(VoiceError, "voice_uplink_required"):
            await self.voice.dictation_start("phone", {})
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.config.read_bytes(), CONFIG)

    async def test_uplink_then_dictation_restores_the_config_byte_for_byte(self):
        started = await self.voice.begin("phone", "chan-1", 1)
        self.assertEqual(started["source_name"], SOURCE_NAME)
        self.assertEqual(self.arbiter.holder, "voice")
        result = await self.voice.dictation_start("phone", {"target": "client"})
        self.assertEqual(result["previous_device"], "default")
        self.assertEqual(audio_device(self.host.config.read_bytes()), SOURCE_NAME)
        self.assertEqual(result["route"], "config")
        self.assertEqual([call[0] for call in self.host.calls],
                         ["point_at", "restart", "record_start"])
        stopped = await self.voice.dictation_stop("phone", {})
        self.assertEqual(stopped["text"], "make the bar taller")
        self.assertEqual(stopped["chars"], 19)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.host.config.read_bytes(), CONFIG)
        events = [event for event in self.hub.events_since(0, device_id="phone")
                  if event.type == "voice.transcript"]
        self.assertEqual(events[-1].payload["text"], "make the bar taller")
        await self.voice.end("chan-1", "session_end")
        self.assertIsNone(self.arbiter.holder)
        self.assertEqual(self.backends[0].ended, "session_end")

    async def test_a_host_whose_voxtype_cannot_name_sources_moves_the_stream(self):
        # cpal on a PipeWire host advertises no source names, so the config is
        # left untouched and Voxtype's capture stream is moved instead.
        self.host.device_names = ["default", "pipewire"]
        await self.voice.begin("phone", "chan-1", 1)
        result = await self.voice.dictation_start("phone", {"target": "client"})
        self.assertEqual(result["route"], "default_source")
        self.assertFalse(result["config_changed"])
        self.assertEqual(self.host.routed, [SOURCE_NAME])
        self.assertEqual(self.host.default_name, SOURCE_NAME)
        self.assertNotIn("point_at", [call[0] for call in self.host.calls])
        self.assertNotIn("restart", [call[0] for call in self.host.calls])
        self.assertEqual(self.host.config.read_bytes(), CONFIG)
        await self.voice.dictation_stop("phone", {})
        self.assertEqual(self.host.config.read_bytes(), CONFIG)
        # The host's own default source is put back exactly as it was.
        self.assertEqual(self.host.default_name, "alsa_input.host_mic")

    async def test_host_target_types_the_transcript_and_never_returns_it(self):
        await self.voice.begin("phone", "chan-1", 1)
        await self.voice.dictation_start("phone", {"target": "host"})
        stopped = await self.voice.dictation_stop("phone", {})
        self.assertEqual(self.typed, ["make the bar taller"])
        self.assertEqual(stopped["text"], "")
        self.assertEqual(stopped["delivered_to_host"], {"delivered": "wtype", "chars": 19})

    async def test_a_failed_start_restores_the_config_before_it_raises(self):
        await self.voice.begin("phone", "chan-1", 1)
        def explode(transcript):
            raise VoiceError("voxtype_command_failed", 503)
        self.host.record_start = explode
        with self.assertRaisesRegex(VoiceError, "voxtype_command_failed"):
            await self.voice.dictation_start("phone", {})
        self.assertEqual(self.host.config.read_bytes(), CONFIG)
        self.assertIsNone(self.voice.dictation)

    async def test_the_remote_uplink_and_the_voice_uplink_never_share_the_microphone(self):
        await self.voice.begin("phone", "chan-1", 1)
        self.arbiter.release("voice")
        self.arbiter.claim("remote")
        with self.assertRaisesRegex(AudioUplinkError, "audio_input_busy"):
            self.arbiter.claim("voice")
        capabilities = await self.voice.capabilities("phone")
        self.assertFalse(capabilities["uplink"]["available"])
        self.assertEqual(capabilities["uplink"]["reason"], "audio_input_busy")

    async def test_generations_only_move_forward_and_frames_need_a_begin(self):
        await self.voice.begin("phone", "chan-1", 5)
        with self.assertRaisesRegex(AudioUplinkError, "audio_input_already_active"):
            await self.voice.begin("phone", "chan-2", 6)
        self.assertTrue(self.voice.accept("phone", "chan-1", 5, b"\0" * 1920)["accepted"])
        with self.assertRaisesRegex(AudioUplinkError, "audio_input_frame_invalid"):
            self.voice.accept("phone", "chan-1", 5, b"\0" * 100)
        with self.assertRaisesRegex(AudioUplinkError, "audio_input_generation_mismatch"):
            self.voice.accept("phone", "chan-1", 6, b"\0" * 1920)
        await self.voice.end("chan-1", "session_end")
        with self.assertRaisesRegex(AudioUplinkError, "audio_input_generation_stale"):
            await self.voice.begin("phone", "chan-3", 5)


class LevelTests(unittest.TestCase):
    def test_frames_decode_and_throttle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            def write():
                connection, _ = server.accept()
                with connection:
                    try:
                        for seq in range(200):
                            connection.sendall(LEVEL_FRAME.pack(seq, -0.5, 0.75, -6.0 if seq % 2 else -40.0))
                    except OSError:
                        pass  # the reader stops first; that is the point
            thread = threading.Thread(target=write, daemon=True)
            thread.start()
            rows = []
            try:
                for row in read_levels(path, interval=0.0):
                    rows.append(row)
                    if len(rows) >= 3:
                        break
            finally:
                server.close()
                thread.join(timeout=2)
            self.assertTrue(rows)
            self.assertEqual(rows[0]["rms"], 0.75)
            self.assertIn(rows[0]["vad"], (True, False))
            self.assertEqual({type(row["peak"]) for row in rows}, {float})

    def test_a_missing_socket_is_a_capability_answer_not_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VoiceError, "voice_levels_unavailable"):
                next(read_levels(Path(directory) / "missing.sock"))


class VoiceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = FakeHost(self.temp.name)
        self.backend = FakeBackend()
        self.hub = Hub()
        self.token = self.hub.register_device("phone")
        self.service = create_service(self.hub, demo=True)
        self.service.voice = VoiceService(self.hub, factory=lambda: self.backend, host=self.host,
                                          preferences=Preferences(), typing=None)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.client = aiohttp.ClientSession()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.headers = {"Authorization": "Bearer " + self.token}

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        self.temp.cleanup()

    async def test_capabilities_and_a_full_uplink_round_trip(self):
        async with self.client.get(self.url + "/v1/voice/capabilities", headers=self.headers) as response:
            self.assertEqual(response.status, 200)
            capabilities = await response.json()
        self.assertTrue(capabilities["voice_uplink_enabled"])
        self.assertEqual(capabilities["uplink"]["source_name"], SOURCE_NAME)
        ws = await self.client.ws_connect(self.url + "/v1/voice/uplink", headers=self.headers)
        await ws.send_json({"generation": 1, **{key: FORMAT[key] for key in
                                                ("format", "rate", "channels", "frame_samples")}})
        begun = await ws.receive_json()
        self.assertEqual(begun["type"], "begun")
        self.assertEqual(begun["source_name"], SOURCE_NAME)
        await ws.send_bytes(b"\0" * FORMAT["frame_bytes"])
        self.assertEqual((await ws.receive_json())["type"], "accepted")
        async with self.client.post(self.url + "/v1/voice/dictation:start", headers=self.headers,
                                    json={"target": "client"}) as response:
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["state"], "recording")
        async with self.client.post(self.url + "/v1/voice/dictation:stop", headers=self.headers,
                                    json={}) as response:
            self.assertEqual(response.status, 200, await response.text())
            stopped = await response.json()
        self.assertEqual(stopped["text"], "make the bar taller")
        self.assertEqual(self.host.config.read_bytes(), CONFIG)
        await ws.send_json({"type": "end", "generation": 1})
        self.assertEqual((await ws.receive_json())["type"], "ended")
        await ws.close()
        self.assertEqual(self.backend.ended, "session_end")

    async def test_a_frame_before_begin_is_refused(self):
        ws = await self.client.ws_connect(self.url + "/v1/voice/uplink", headers=self.headers)
        await ws.send_bytes(b"\0" * FORMAT["frame_bytes"])
        rejected = await ws.receive_json()
        self.assertEqual((rejected["type"], rejected["reason"]), ("rejected", "audio_input_begin_required"))
        await ws.close()

    async def test_dictation_without_an_uplink_is_a_409_that_changes_nothing(self):
        async with self.client.post(self.url + "/v1/voice/dictation:start", headers=self.headers,
                                    json={}) as response:
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())["error"]["code"], "voice_uplink_required")
        self.assertEqual(self.host.config.read_bytes(), CONFIG)


if __name__ == "__main__":
    unittest.main()
