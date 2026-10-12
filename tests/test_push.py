"""LITE-1b C6: Web Push from the host.

The sender against RFC 8291's own test vector and a full decrypt with the
browser's key; VAPID; the store; what is worth a push; the sender's
debounce/coalesce/retract/size/410/retry rules with a fake push service; the
HTTP routes with a real web credential; and the NEXT-1 §6 fixes that live in
core (dismissals reach every client, root icons, revoke drops the
subscription).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hmac import HMAC

from omodachi_core import push, webpush
from omodachi_core.hub import DeviceEvent, Hub
from omodachi_core.notifications import NotificationMirror
from omodachi_core.service import blocked_panes

from tests.test_odomo import _Base

APPLE = "https://web.push.apple.com/QGuXz3Tc-fixture"
FCM = "https://fcm.googleapis.com/fcm/send/fixture-token"


def browser_keys():
    """A browser's side of a subscription: its private key, and the JSON it sends."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return private, auth, {"p256dh": webpush.b64u(public), "auth": webpush.b64u(auth)}


def decrypt(body: bytes, private, auth: bytes) -> bytes:
    """RFC 8291 from the browser's side, written out independently here."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]
    self_public = private.public_key().public_bytes(serialization.Encoding.X962,
                                                    serialization.PublicFormat.UncompressedPoint)
    shared = private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))

    def hmac(key, data):
        h = HMAC(key, hashes.SHA256())
        h.update(data)
        return h.finalize()
    ikm = hmac(hmac(auth, shared), b"WebPush: info\x00" + self_public + as_public + b"\x01")[:32]
    prk = hmac(salt, ikm)
    cek = hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert rs == 4096 and plain.endswith(b"\x02")
    return plain[:-1]


# RFC 8291 §5 / Appendix A: the header and the ciphertext, as the RFC prints them.
RFC_HEADER = webpush.unb64u("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
                            "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
RFC_CIPHERTEXT = webpush.unb64u("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ")


class SenderTests(unittest.TestCase):
    def test_rfc_8291_vector_byte_for_byte(self):
        as_private = ec.derive_private_key(
            int.from_bytes(webpush.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        body = webpush.encrypt(
            webpush.unb64u("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
            webpush.unb64u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
            webpush.unb64u("BTBZMqHH6r4Tts7J_aSIgg"),
            as_private=as_private, salt=webpush.unb64u("DGv6ra1nlYgDCS1FRnbzlw"))
        self.assertEqual(body, RFC_HEADER + RFC_CIPHERTEXT)

    def test_rfc_8291_vector_decrypts_with_the_receiver_key(self):
        ua = ec.derive_private_key(
            int.from_bytes(webpush.unb64u("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"), "big"), ec.SECP256R1())
        body = RFC_HEADER + RFC_CIPHERTEXT
        self.assertEqual(decrypt(body, ua, webpush.unb64u("BTBZMqHH6r4Tts7J_aSIgg")),
                         b"When I grow up, I want to be a watermelon")

    def test_round_trip_and_limits(self):
        private, auth, keys = browser_keys()
        message = json.dumps({"web_push": 8030, "notification": {"title": "x"}}).encode()
        body = webpush.encrypt(message, webpush.unb64u(keys["p256dh"]), auth)
        self.assertEqual(decrypt(body, private, auth), message)
        self.assertEqual(len(webpush.encrypt(b"x" * webpush.MAX_PLAINTEXT, webpush.unb64u(keys["p256dh"]), auth)),
                         4096)
        with self.assertRaises(ValueError):
            webpush.encrypt(b"x" * (webpush.MAX_PLAINTEXT + 1), webpush.unb64u(keys["p256dh"]), auth)
        with self.assertRaises(ValueError):
            webpush.encrypt(b"x", b"\x04" + b"\x00" * 10, auth)

    def test_vapid_jwt_verifies_and_is_reused_for_an_hour_at_least(self):
        now = [1_800_000_000]
        vapid = webpush.Vapid.generate(clock=lambda: now[0])
        token = vapid.jwt("https://web.push.apple.com", "https://host.example.ts.net")
        header, claims, signature = token.split(".")
        raw = webpush.unb64u(signature)
        vapid.key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"),
                                                           int.from_bytes(raw[32:], "big")),
                                      f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        body = json.loads(webpush.unb64u(claims))
        self.assertEqual(body, {"aud": "https://web.push.apple.com", "exp": now[0] + 12 * 3600,
                                "sub": "https://host.example.ts.net"})
        now[0] += 10 * 3600
        self.assertEqual(vapid.jwt("https://web.push.apple.com", "https://host.example.ts.net"), token)
        now[0] += 3600 + 1     # less than an hour left: a new one
        self.assertNotEqual(vapid.jwt("https://web.push.apple.com", "https://host.example.ts.net"), token)
        self.assertNotEqual(vapid.jwt("https://fcm.googleapis.com", "https://host.example.ts.net"), token)

    def test_request_headers(self):
        _, _, keys = browser_keys()
        vapid = webpush.Vapid.generate()
        prepared = webpush.request({"endpoint": APPLE, "keys": keys}, b"{}", vapid,
                                   subject="https://h.example.ts.net", ttl=60, urgency="high", topic="agent-approval")
        headers = {key.lower(): value for key, value in prepared.header_items()}
        self.assertEqual(headers["content-encoding"], "aes128gcm")
        self.assertEqual(headers["ttl"], "60")
        self.assertEqual(headers["urgency"], "high")
        self.assertEqual(headers["topic"], "agent-approval")
        self.assertTrue(headers["authorization"].startswith("vapid t="))
        self.assertTrue(headers["authorization"].endswith(", k=" + vapid.public_b64u))
        self.assertEqual(prepared.get_method(), "POST")

    def test_key_file_is_private_created_once_and_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config/vapid.json"
            first = webpush.load_or_create(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(webpush.load_or_create(path).public_b64u, first.public_b64u)
            self.assertEqual(json.loads(path.read_text())["public"], first.public_b64u)
            path.chmod(0o644)
            webpush.load_or_create(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            other = Path(directory) / "other.json"
            other.write_text(path.read_text())
            link = Path(directory) / "link.json"
            link.symlink_to(other)
            with self.assertRaises(OSError):
                webpush.load_or_create(link)

    def test_send_reports_status_and_retry_after_without_following_redirects(self):
        import urllib.error

        class Opener:
            def __init__(self, error=None):
                self.error = error

            def open(self, request, timeout):
                if self.error:
                    raise self.error
                class Response:
                    status = 201
                    def read(self, n):
                        return b""
                    def __enter__(self):
                        return self
                    def __exit__(self, *a):
                        return False
                return Response()
        _, _, keys = browser_keys()
        vapid = webpush.Vapid.generate()
        sub = {"endpoint": FCM, "keys": keys}
        self.assertEqual(webpush.send(sub, b"{}", vapid, subject="https://h.ts.net", opener=Opener())["status"], 201)
        error = urllib.error.HTTPError(FCM, 429, "slow", {"Retry-After": "7"}, None)
        result = webpush.send(sub, b"{}", vapid, subject="https://h.ts.net", opener=Opener(error))
        self.assertEqual((result["status"], result["retry_after"]), (429, 7))
        result = webpush.send(sub, b"{}", vapid, subject="https://h.ts.net",
                              opener=Opener(urllib.error.URLError("down")))
        self.assertEqual(result["status"], 0)
        self.assertIsNone(webpush._NoRedirect().redirect_request(None, None, 302, "x", {}, "https://evil"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = push.PushStore(Path(self.temp.name) / "push-subscriptions.json", clock=lambda: 1_800_000_000)
        _, _, self.keys = browser_keys()

    def test_endpoints_must_be_https_on_a_push_service(self):
        good = [APPLE, FCM, "https://updates.push.services.mozilla.com/wpush/v2/abc",
                "https://wns2-par02p.notify.windows.com/w/?token=abc"]
        bad = ["http://web.push.apple.com/x", "https://web.push.apple.com:8443/x", "https://evil.example/x",
               "https://push.apple.com.evil.example/x", "https://user:pw@web.push.apple.com/x",
               "https://127.0.0.1/x", "https://web.push.apple.com", "https://web.push.apple.com/x#y",
               "https://web.push.apple.com/" + "a" * 2100, "https://web.push.apple.com/a b", 42, None]
        for endpoint in good:
            self.store.subscribe("odomo-a", {"endpoint": endpoint, "keys": self.keys})
        for endpoint in bad:
            with self.assertRaises(push.PushError, msg=endpoint):
                self.store.subscribe("odomo-a", {"endpoint": endpoint, "keys": self.keys})

    def test_keys_are_checked(self):
        for keys in ({"p256dh": self.keys["p256dh"]}, {"p256dh": "x" * 20, "auth": self.keys["auth"]},
                     {"p256dh": self.keys["p256dh"], "auth": webpush.b64u(b"\x00" * 15)},
                     {"p256dh": webpush.b64u(b"\x03" + b"\x00" * 64), "auth": self.keys["auth"]}):
            with self.assertRaises(push.PushError):
                self.store.subscribe("odomo-a", {"endpoint": APPLE, "keys": keys})
        with self.assertRaises(push.PushError):
            self.store.subscribe("odomo-a", {"endpoint": APPLE, "keys": self.keys, "extra": 1})

    def test_one_row_per_device_private_file_and_forget(self):
        record = self.store.subscribe("odomo-a", {"endpoint": APPLE, "keys": self.keys, "expirationTime": None})
        self.assertRegex(record["subscription_id"], r"^sub_[0-9a-f]{24}$")
        self.store.subscribe("odomo-a", {"endpoint": FCM, "keys": self.keys})
        [(device, sub, prefs)] = self.store.targets()
        self.assertEqual((device, sub["endpoint"]), ("odomo-a", FCM))
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertTrue(self.store.forget("odomo-a"))
        self.assertEqual(self.store.targets(), [])
        self.assertFalse(self.store.forget("odomo-a"))

    def test_prefs_revision_conflict_and_validation(self):
        prefs = self.store.prefs("odomo-a")
        self.assertEqual(prefs["revision"], 0)
        # NOTIFY-1: every kind on but "an agent in Herdr finished", and quiet at the desk.
        self.assertEqual({kind for kind, on in prefs["kinds"].items() if not on}, {"herdr_done"})
        self.assertTrue(prefs["quiet_at_desk"])
        self.assertFalse(prefs["subscribed"])
        changed = self.store.set_prefs("odomo-a", 0, {"kinds": {"agent_done": False}, "lang": "zh-Hans",
                                                    "snooze_until": 1_800_003_600})
        self.assertEqual((changed["revision"], changed["kinds"]["agent_done"], changed["lang"]), (1, False, "zh-Hans"))
        with self.assertRaises(push.PushError) as caught:
            self.store.set_prefs("odomo-a", 0, {"lang": "en"})
        self.assertEqual((caught.exception.code, caught.exception.status), ("stale_revision", 409))
        for changes in ({}, {"kinds": {"nope": True}}, {"kinds": {"agent_done": 1}}, {"lang": "fr"},
                        {"snooze_until": -1}, {"quiet_when_native_connected": "yes"}, {"host_enabled": False},
                        {"quiet_at_desk": 1}):
            with self.assertRaises(push.PushError, msg=changes):
                self.store.set_prefs("odomo-a", 1, changes)

    def test_host_switch_410_drop_and_summary(self):
        self.store.subscribe("odomo-a", {"endpoint": APPLE, "keys": self.keys})
        self.store.subscribe("odomo-b", {"endpoint": FCM, "keys": self.keys})
        self.store.set_host_enabled("odomo-a", False)
        self.assertEqual([row[0] for row in self.store.targets()], ["odomo-b"])
        self.assertEqual(self.store.summary(), {"odomo-a": {"subscribed": True, "enabled": False},
                                                "odomo-b": {"subscribed": True, "enabled": True}})
        self.assertFalse(self.store.drop_endpoint("odomo-b", "https://fcm.googleapis.com/other"))
        self.assertTrue(self.store.drop_endpoint("odomo-b", FCM))
        self.assertEqual(self.store.targets(), [])

    def test_live_count_is_subscribed_switched_on_and_still_paired(self):
        """LITE-1d: the number the native app reads, and nothing else."""
        self.assertEqual(self.store.live_count(), 0)
        self.store.subscribe("odomo-a", {"endpoint": APPLE, "keys": self.keys})
        self.store.subscribe("odomo-b", {"endpoint": FCM, "keys": self.keys})
        self.store.set_host_enabled("odomo-c", True)  # a row with no subscription
        self.assertEqual(self.store.live_count(), 2)
        self.store.set_host_enabled("odomo-a", False)
        self.assertEqual(self.store.live_count(), 1)
        self.assertEqual(self.store.live_count({"odomo-a", "odomo-c"}), 0)
        self.assertEqual(self.store.live_count({"odomo-b"}), 1)
        self.store.unsubscribe("odomo-b")
        self.assertEqual(self.store.live_count(), 0)


def event(kind, payload, seq=1):
    return DeviceEvent(seq, f"evt_{seq:08d}", kind, None, payload, time.time())


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.watcher = push.Watcher(started_at=1_800_000_000)
        self.watcher.prime(self.hub)

    def agent(self, status):
        published = self.hub.update_state({"agent": {"status": status, "default_agent": {"pane_id": "w1:p1"}}},
                                          event_type="agent.changed")
        return [(type(s).__name__, getattr(s, "kind", None), s.tag)
                for s in self.watcher.signals(published, self.hub)]

    def test_agent_transitions(self):
        # One tag for the agent: a newer state replaces the last on screen
        # (the only way off an iPhone); a retraction only when nothing replaces it.
        self.assertEqual(self.agent("working"), [])
        self.assertEqual(self.agent("waiting_on_approval"), [("Signal", "agent_approval", "agent")])
        self.assertEqual(self.agent("waiting_on_approval"), [])
        self.assertEqual(self.agent("working"), [("Retract", None, "agent")])
        self.assertEqual(self.agent("blocked"), [("Signal", "agent_needs_input", "agent")])
        self.assertEqual(self.agent("waiting_on_user_input"), [])
        self.assertEqual(self.agent("done"), [("Signal", "agent_done", "agent")])
        self.assertEqual(self.agent("waiting_on_approval"), [("Signal", "agent_approval", "agent")])
        self.assertEqual(self.agent("waiting_on_user_input"), [("Signal", "agent_needs_input", "agent")])
        self.assertEqual(self.agent("idle"), [("Retract", None, "agent")])  # the question went away
        self.assertEqual(self.agent("done"), [])  # idle -> done was not a turn ending here

    def test_herdr_blocked_is_news_after_the_first_look(self):
        self.hub.update_state({"agent": {"default_agent": {"pane_id": "w1:p1"}}})
        layout = lambda *panes: event("herdr.layout.changed", {"session": "omodachi", "revision": 1,
                                                               "blocked": [{"pane_id": p, "name": "codex"}
                                                                           for p in panes]})
        self.assertEqual(self.watcher.signals(layout("w1:p2"), self.hub), [])     # baseline
        out = self.watcher.signals(layout("w1:p2", "w2:p1", "w1:p1"), self.hub)  # w1:p1 is the default agent
        self.assertEqual([(type(s).__name__, s.tag) for s in out], [("Signal", "herdr-w2-p1")])
        self.assertEqual((out[0].ids, out[0].name, out[0].panel), ({"session": "omodachi", "pane": "w2:p1", "agent": "codex"}, "codex", "herdr"))
        out = self.watcher.signals(layout("w2:p1"), self.hub)
        self.assertEqual([(type(s).__name__, s.tag) for s in out], [("Retract", "herdr-w1-p2")])

    def test_host_notifications_new_only_and_their_retraction(self):
        row = {"id": "1800000005000-3", "app": "Omodachi", "summary": "Hi", "body": "there", "glyph": "",
               "urgency": "critical", "timestamp": 1_800_000_005_000, "has_action": False, "active": True}
        [signal] = self.watcher.signals(event("notification.posted", row), self.hub)
        self.assertEqual((signal.kind, signal.tag, signal.urgency, signal.ids), (
            "host_notification", "note-1800000005000-3", "high", {"note": "1800000005000-3"}))
        old = dict(row, timestamp=1_799_999_000_000)
        self.assertEqual(self.watcher.signals(event("notification.posted", old), self.hub), [])
        self.assertEqual(self.watcher.signals(event("notification.posted", dict(row, active=False)), self.hub), [])
        # Leaving the desktop's screen is not a retraction: a toast that timed
        # out looks the same, and the phone should keep it.
        self.assertEqual(self.watcher.signals(event("notification.updated", dict(row, active=False)), self.hub), [])

    def test_payloads_stay_small_and_carry_ids_only(self):
        long = push.Signal("host_notification", "note:x", "note-1", panel="notifications", ids={"note": "1-1"},
                           name="x" * 200, title="t" * 500, body="b" * 5000)
        raw = push.payload_for(long, origin="https://h.example.ts.net", lang="en", host="h")
        self.assertLessEqual(len(raw), push.PAYLOAD_LIMIT)
        note = json.loads(raw)["notification"]
        self.assertLessEqual(len(note["title"]), push.TITLE_LIMIT)
        self.assertLessEqual(len(note["body"]), push.BODY_LIMIT)
        self.assertEqual(note["navigate"], "https://h.example.ts.net/odomo/?open=notifications&note=1-1")
        zh = json.loads(push.payload_for(push.Signal("agent_done", "agent", "agent-done", panel="agent",
                                                     ids={"ref": "done"}),
                                         origin="https://h.example.ts.net:8443", lang="zh-Hans", host="omarchy",
                                         more=2))["notification"]
        self.assertEqual(zh["title"], "Agent 完成了")
        self.assertIn("另有 2 条", zh["body"])
        self.assertEqual(zh["lang"], "zh-Hans")
        self.assertTrue(zh["navigate"].startswith("https://h.example.ts.net:8443/odomo/?open=agent"))
        self.assertEqual(json.loads(push.retract_payload("agent-approval")), {"omodachi": "retract", "tag": "agent-approval"})

    def test_blocked_panes_from_a_layout(self):
        layout = {"workspaces": [{"tabs": [{"panes": [
            {"id": "w1:p1", "agent_status": "working", "agent": {"name": "default", "kind": "codex", "status": "working"}},
            {"id": "w1:p2", "agent_status": "blocked", "agent": {"name": "default", "kind": "codex", "status": "blocked"}},
            {"id": "w1:p3", "agent_status": "blocked", "agent": {"name": "helper", "kind": "claude", "status": "blocked"}},
            {"id": "w1:p4", "agent_status": "unknown", "agent": None}]}]}]}
        self.assertEqual(blocked_panes(layout), [{"pane_id": "w1:p2", "name": "codex"},
                                                 {"pane_id": "w1:p3", "name": "helper"}])


class FakeService:
    """A push service: records what it was sent and answers from a script."""

    def __init__(self, answers=None):
        self.sent, self.answers = [], list(answers or [])
        self.lock = threading.Lock()

    def __call__(self, subscription, body, vapid, **options):
        with self.lock:
            self.sent.append({"endpoint": subscription["endpoint"], "body": json.loads(body), **options,
                              "thread": threading.current_thread().name})
            status = self.answers.pop(0) if self.answers else 201
        return {"status": status, "retry_after": 0, "body_bytes": len(body)}


class PushSenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub()
        self.store = push.PushStore(Path(self.temp.name) / "push-subscriptions.json")
        _, _, keys = browser_keys()
        self.store.subscribe("odomo-iphone", {"endpoint": APPLE, "keys": keys})
        self.store.subscribe("odomo-chrome", {"endpoint": FCM, "keys": keys})
        self.service = FakeService()
        self.native = False
        self.now = [1000.0]
        self.sender = push.PushSender(self.hub, self.store, lambda: webpush.Vapid.generate(),
                                      origin=lambda: "https://omodachi-lab.example.ts.net",
                                      host_name=lambda: "omodachi-lab", native_alerts=lambda kind: self.native,
                                      send=self.service, clock=lambda: self.now[0], debounce=30.0,
                                      sleep=lambda seconds: None, log=lambda line: None)
        self.sender.watcher.started_ms = 0
        self.sender.start()

    async def asyncTearDown(self):
        await self.sender.stop()
        self.temp.cleanup()

    def note(self, number, app="Omodachi", active=True):
        return {"id": f"1800000000000-{number}", "app": app, "summary": f"note {number}", "body": "b",
                "glyph": "", "urgency": "normal", "timestamp": 1_800_000_000_000 + number,
                "has_action": False, "active": active}

    async def settle(self):
        for _ in range(3):
            await asyncio.sleep(0)
        await self.sender.drain()

    async def test_first_goes_at_once_the_rest_of_the_window_coalesce(self):
        self.hub.publish("notification.posted", self.note(1))
        await self.settle()
        self.assertEqual(len(self.service.sent), 2)                 # both devices, at once
        self.assertTrue(all(row["thread"] != threading.main_thread().name for row in self.service.sent))
        self.hub.publish("notification.posted", self.note(2))
        self.hub.publish("notification.posted", self.note(3))
        await self.settle()
        self.assertEqual(len(self.service.sent), 2)                 # held for the window
        self.sender.debounce = 0.05
        for window in self.sender._windows.values():
            if window["timer"]:
                window["timer"].cancel()
                window["timer"] = self.sender._loop.call_later(0.05, self.sender._flush,
                                                               next(k for k, v in self.sender._windows.items() if v is window))
        await asyncio.sleep(0.15)
        await self.settle()
        self.assertEqual(len(self.service.sent), 4)
        coalesced = self.service.sent[-1]["body"]["notification"]
        self.assertEqual(coalesced["title"], "Omodachi: 2 notifications")
        self.assertEqual(coalesced["tag"], "note-1800000000000-3")

    async def test_another_source_is_not_held_by_the_first(self):
        self.hub.publish("notification.posted", self.note(1, app="A"))
        self.hub.publish("notification.posted", self.note(2, app="B"))
        await self.settle()
        self.assertEqual(len(self.service.sent), 4)

    async def test_retract_by_tag_only_where_the_platform_allows(self):
        self.hub.update_state({"agent": {"status": "waiting_on_approval"}}, event_type="agent.changed")
        await self.settle()
        self.hub.update_state({"agent": {"status": "working"}}, event_type="agent.changed")
        await self.settle()
        retracts = [row for row in self.service.sent if "omodachi" in row["body"]]
        self.assertEqual([row["endpoint"] for row in retracts], [FCM])  # never to Apple
        self.assertEqual(retracts[0]["body"], {"omodachi": "retract", "tag": "agent"})
        # A toast leaving the desktop never takes the push off the phone.
        self.hub.publish("notification.posted", self.note(1))
        self.hub.publish("notification.updated", self.note(1, active=False))
        await self.settle()
        self.assertEqual(len([row for row in self.service.sent if "omodachi" in row["body"]]), 1)

    async def test_a_herdr_pane_answered_inside_the_delay_says_nothing(self):
        # NOTIFY-1: Herdr waits out its delay before it says anything; a pane
        # answered at the computer meanwhile was answered by someone there.
        self.sender.herdr_delay = 0.05
        self.hub.publish("herdr.layout.changed", {"session": "s", "revision": 1, "blocked": []})
        self.hub.publish("herdr.layout.changed", {"session": "s", "revision": 2,
                                                  "blocked": [{"pane_id": "w1:p1", "name": "a"}]})
        await self.settle()
        self.assertEqual(self.service.sent, [])
        self.assertTrue(self.sender._herdr_timers)
        self.hub.publish("herdr.layout.changed", {"session": "s", "revision": 3, "blocked": []})
        await self.settle()
        self.assertEqual(self.sender._herdr_timers, {})
        await asyncio.sleep(0.1)
        await self.settle()
        self.assertEqual(self.service.sent, [])

    async def test_410_drops_the_subscription_and_429_is_retried_boundedly(self):
        self.service.answers = [410, 429, 429, 201]
        self.hub.publish("notification.posted", self.note(1))
        await self.settle()
        self.assertEqual(len(self.service.sent), 4)              # 1 for the 410, 3 for the 429s
        self.assertEqual([row[0] for row in self.store.targets()], ["odomo-chrome"])
        self.service.answers = [503, 503, 503, 503]
        self.hub.publish("notification.posted", self.note(9, app="Other"))
        await self.settle()
        self.assertEqual(len(self.service.sent), 4 + 1 + push.RETRIES)   # never more than RETRIES again

    async def test_preferences_snooze_native_and_the_host_switch(self):
        self.store.set_prefs("odomo-iphone", 0, {"kinds": {"host_notification": False}})
        self.hub.publish("notification.posted", self.note(1))
        await self.settle()
        self.assertEqual([row["endpoint"] for row in self.service.sent], [FCM])
        self.store.set_prefs("odomo-chrome", 0, {"snooze_until": int(time.time()) + 600})
        self.hub.publish("notification.posted", self.note(2, app="X"))
        await self.settle()
        self.assertEqual(len(self.service.sent), 1)
        self.store.set_prefs("odomo-chrome", 1, {"snooze_until": None})
        self.native = True
        self.hub.publish("notification.posted", self.note(3, app="Y"))
        await self.settle()
        self.assertEqual(len(self.service.sent), 1)
        self.store.set_prefs("odomo-chrome", 2, {"quiet_when_native_connected": False})
        self.store.set_host_enabled("odomo-chrome", False)
        self.hub.publish("notification.posted", self.note(4, app="Z"))
        await self.settle()
        self.assertEqual(len(self.service.sent), 1)

    async def test_every_topic_is_one_apple_accepts(self):
        # Apple: 400 BadWebPushTopic for `note-1790857529061-13` (21 chars, not
        # valid base64url); it took `agent-approval` (14). Every topic is now 16
        # hex characters: base64url-valid, <= 32, stable per tag.
        import base64, re
        for tag in ("note-1790857529061-13", "agent", "herdr-w9-p1", "x" * 32):
            topic = push.topic_for(tag)
            self.assertRegex(topic, r"^[0-9a-f]{16}$")
            base64.urlsafe_b64decode(topic)
            self.assertEqual(topic, push.topic_for(tag))
        self.hub.publish("notification.posted", self.note(13))
        await self.settle()
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{16}", row["topic"]) for row in self.service.sent))

    async def test_a_retraction_never_overtakes_the_push_it_retracts(self):
        order = []
        slow = threading.Event()

        def send(subscription, body, vapid, **options):
            message = json.loads(body)
            if "web_push" in message:
                slow.wait(0.2)      # the push is slow on the wire
            order.append((subscription["endpoint"], "retract" if "omodachi" in message else "push"))
            return {"status": 201}
        self.sender._send = send
        self.hub.update_state({"agent": {"status": "waiting_on_approval"}}, event_type="agent.changed")
        self.hub.update_state({"agent": {"status": "working"}}, event_type="agent.changed")
        await self.settle()
        fcm = [kind for endpoint, kind in order if endpoint == FCM]
        self.assertEqual(fcm, ["push", "retract"])

    async def test_two_agent_changes_back_to_back_are_both_seen(self):
        # Handled after both were published, the state already says `done`;
        # the events themselves still say working, then done.
        self.hub.update_state({"agent": {"status": "working"}}, event_type="agent.changed")
        self.hub.update_state({"agent": {"status": "done"}}, event_type="agent.changed")
        self.hub.update_state({"agent": {"chat": {"pending_approvals": 0}}}, event_type="agent.changed")
        await self.settle()
        self.assertEqual([(row["body"]["notification"]["tag"], row["body"]["notification"]["title"])
                          for row in self.service.sent], [("agent", "The agent is done")] * 2)

    async def test_a_publish_from_another_thread_is_handed_to_the_loop(self):
        thread = threading.Thread(target=lambda: self.hub.publish("notification.posted", self.note(1)))
        thread.start()
        thread.join()
        await asyncio.sleep(0.05)
        await self.settle()
        self.assertEqual(len(self.service.sent), 2)

    async def test_a_push_shown_on_this_computer_is_not_pushed_back(self):
        # Odomo open in a browser on the host itself: its notification lands in
        # the host's notification list, and must not come back as a push.
        self.hub.update_state({"agent": {"status": "waiting_on_approval"}}, event_type="agent.changed")
        await self.settle()
        sent = len(self.service.sent)
        echo = dict(self.note(7, app="Chromium"), summary="The agent needs your approval")
        self.hub.publish("notification.posted", echo)
        await self.settle()
        self.assertEqual(len(self.service.sent), sent)
        self.hub.publish("notification.posted", self.note(8, app="Chromium"))
        await self.settle()
        self.assertEqual(len(self.service.sent), sent + 2)

    async def test_no_public_name_means_no_push(self):
        self.sender._origin = lambda: None
        self.hub.publish("notification.posted", self.note(1))
        await self.settle()
        self.assertEqual(self.service.sent, [])
        self.assertEqual(self.sender.results[-1]["status"], "no_public_name")

    async def test_payload_on_the_wire_is_declarative_with_subject_and_topic(self):
        self.hub.update_state({"agent": {"status": "waiting_on_approval"}}, event_type="agent.changed")
        await self.settle()
        row = self.service.sent[0]
        self.assertEqual(row["body"]["web_push"], 8030)
        self.assertEqual(row["subject"], "https://omodachi-lab.example.ts.net")
        self.assertEqual((row["topic"], row["urgency"]), (push.topic_for("agent"), "high"))
        self.assertEqual(row["body"]["notification"]["navigate"],
                         "https://omodachi-lab.example.ts.net/odomo/?open=agent&ref=approval")


class MirrorTests(unittest.TestCase):
    """NEXT-1 §6.1: a toast that leaves the screen says so to every client."""

    def test_dismissed_and_vanished_rows_publish_notification_updated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "notifications"
            (root / "history").mkdir(parents=True)
            for number in (1, 2):
                (root / f"1800000000000-{number}.json").write_text(json.dumps({
                    "app": "A", "summary": f"s{number}", "body": "", "urgency": 1,
                    "timestamp": 1_800_000_000_000 + number}))
            hub = Hub()
            calls = []
            mirror = NotificationMirror(hub, state_dir=root,
                                        runner=lambda argv, **kw: calls.append(argv) or
                                        type("R", (), {"returncode": 0, "stdout": "ok"})())
            mirror.scan()
            (root / "1800000000000-1.json").rename(root / "history/1800000000000-1.json")
            mirror.scan()
            mirror.act("1800000000000-2", "dismiss")
            updated = [e.payload for e in hub.events_since(0) if e.type == "notification.updated"]
            self.assertEqual([(row["id"], row["active"]) for row in updated],
                             [("1800000000000-1", False), ("1800000000000-2", False)])
            mirror.scan()   # the file is still there: no second update for the same row
            self.assertEqual(len([e for e in hub.events_since(0) if e.type == "notification.updated"]), 2)

    def test_dispatch_hands_events_to_the_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "notifications"
            root.mkdir()
            (root / "1800000000000-1.json").write_text(json.dumps({"app": "A", "summary": "s", "timestamp": 1}))
            hub = Hub()
            handed = []
            mirror = NotificationMirror(hub, state_dir=root)
            mirror.dispatch = lambda function, *args: handed.append((function, args))
            mirror.scan()
            self.assertEqual(hub.events_since(0), [])
            for function, args in handed:
                function(*args)
            self.assertEqual([e.type for e in hub.events_since(0)], ["notification.posted"])


class PushApiTests(_Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        base = Path(self.temp.name)
        self.service.push_store = push.PushStore(base / "push-subscriptions.json")
        self.vapid = webpush.load_or_create(base / "vapid.json")
        self.service.vapid = lambda: self.vapid
        _, _, self.keys = browser_keys()

    async def test_web_device_subscribes_reads_and_changes_prefs(self):
        _, claim = await self.pair_web()
        web = claim["credential"]
        status, key, _ = await self.call("GET", "/v1/push/vapid-public-key", token=web, origin=self.origin)
        self.assertEqual((status, key["vapid_public_key"]), (200, self.vapid.public_b64u))
        status, body, _ = await self.call("POST", "/v1/push/subscriptions", token=web, origin=self.origin,
                                          body={"endpoint": APPLE, "keys": self.keys, "expirationTime": None})
        self.assertEqual(status, 201, body)
        self.assertEqual((body["device_id"], body["endpoint_host"]), ("odomo-a", "web.push.apple.com"))
        self.assertNotIn("endpoint", body)
        status, prefs, _ = await self.call("GET", "/v1/notifications/prefs", token=web, origin=self.origin)
        self.assertEqual((status, prefs["subscribed"], prefs["revision"]), (200, True, 0))
        status, prefs, _ = await self.call("PUT", "/v1/notifications/prefs", token=web, origin=self.origin,
                                           body={"expected_revision": 0, "changes": {"lang": "zh-Hans"}})
        self.assertEqual((status, prefs["lang"], prefs["revision"]), (200, "zh-Hans", 1))
        status, error, _ = await self.call("PUT", "/v1/notifications/prefs", token=web, origin=self.origin,
                                           body={"expected_revision": 0, "changes": {"lang": "en"}})
        self.assertEqual((status, error["error"]["code"]), (409, "stale_revision"))
        status, error, _ = await self.call("POST", "/v1/push/subscriptions", token=web, origin=self.origin,
                                           body={"endpoint": "https://evil.example/x", "keys": self.keys})
        self.assertEqual((status, error["error"]["code"]), (400, "invalid_push_endpoint"))
        status, body, _ = await self.call("DELETE", "/v1/push/subscriptions", token=web, origin=self.origin)
        self.assertEqual((status, body["removed"]), (200, True))

    async def test_push_is_for_web_credentials_only(self):
        for method, path in (("GET", "/v1/push/vapid-public-key"), ("POST", "/v1/push/subscriptions"),
                             ("GET", "/v1/notifications/prefs")):
            status, body, _ = await self.call(method, path, token=self.native,
                                              body={} if method == "POST" else None)
            self.assertEqual((status, body["error"]["code"]), (403, "push_web_only"), path)
        status, body, _ = await self.call("GET", "/v1/notifications/prefs")
        self.assertEqual(status, 401)

    async def test_revoke_and_purge_take_the_subscription(self):
        _, claim = await self.pair_web()
        await self.call("POST", "/v1/push/subscriptions", token=claim["credential"], origin=self.origin,
                        body={"endpoint": FCM, "keys": self.keys})
        listing = await self.service.dispatch_local_async("local.devices.list", {}, local_authorize=lambda: True)
        row = next(row for row in listing["devices"] if row["device_id"] == "odomo-a")
        self.assertEqual(row["push"], {"subscribed": True, "enabled": True})
        result = await self.service.dispatch_local_async("local.devices.push", {"device_id": "odomo-a", "enabled": False},
                                                         local_authorize=lambda: True)
        self.assertEqual(result["push"], {"subscribed": True, "enabled": False})
        with self.assertRaises(Exception):
            await self.service.dispatch_local_async("local.devices.push", {"device_id": "ipad-native", "enabled": False},
                                                    local_authorize=lambda: True)
        self.service.media_pairing = type("Media", (), {
            "revoke_device": lambda self, device, local_authorize: {"certificate_revocation_supported": False},
            "authorized_devices": lambda self: {}, "pending_local": lambda self, local_authorize: {"requests": []}})()
        await self.service.dispatch_local_async("local.devices.revoke", {"device_id": "odomo-a"},
                                                local_authorize=lambda: True)
        self.assertEqual(self.service.push_store.summary(), {})

    async def test_a_revoke_between_the_routes_check_and_the_write_leaves_no_subscription(self):
        """SEC-RENEW-1: the subscription is written only under a credential that still verifies.

        `body()` rechecks the credential, then the write hops to a worker
        thread. A `devices revoke` that finishes in that hop must not be
        followed by a subscription for the browser it revoked.
        """
        _, claim = await self.pair_web()
        self.service.media_pairing = type("Media", (), {
            "revoke_device": lambda self, device, local_authorize: {"certificate_revocation_supported": False}})()
        store = self.service.push_store
        original = store.subscribe

        def subscribe(*args, **kwargs):
            store.subscribe = original
            self.service._revoke_device("odomo-a", lambda: True)
            return original(*args, **kwargs)

        store.subscribe = subscribe
        status, body, _ = await self.call("POST", "/v1/push/subscriptions", token=claim["credential"],
                                          origin=self.origin, body={"endpoint": FCM, "keys": self.keys})
        self.assertEqual(status, 401, "a revoked browser was subscribed")
        self.assertEqual(body["error"]["reason"], "credential_revoked")
        self.assertEqual(store.summary(), {}, "a revoked browser kept a push subscription")

    async def test_roads_say_how_many_web_devices_get_pushes(self):
        """LITE-1d: `odomo.push_devices` on the native app's roads, live."""
        from omodachi_core.host_identity import HostIdentity
        identity = HostIdentity(host_id="0" * 32, host_name="omodachi-lab", addresses=["192.0.2.10"],
                                odomo_push=self.service.odomo_push_devices)
        identity.fingerprint = lambda: "ab" * 32
        self.service.install_host_identity(identity)

        async def odomo():
            status, roads, _ = await self.call("GET", "/v1/host/roads", token=self.native)
            self.assertEqual(status, 200, roads)
            self.assertNotIn("odomo_url", roads)
            return roads["odomo"]

        self.assertEqual(await odomo(), {"url": None, "push_devices": 0})
        _, claim = await self.pair_web()
        self.assertEqual(claim["odomo"], {"url": None, "push_devices": 0})
        self.assertEqual(await odomo(), {"url": None, "push_devices": 0}, "paired is not subscribed")
        await self.call("POST", "/v1/push/subscriptions", token=claim["credential"], origin=self.origin,
                        body={"endpoint": APPLE, "keys": self.keys})
        value = await odomo()
        self.assertEqual(value, {"url": None, "push_devices": 1})
        self.assertNotIn("odomo-a", json.dumps(value))
        await self.service.dispatch_local_async("local.devices.push", {"device_id": "odomo-a", "enabled": False},
                                                local_authorize=lambda: True)
        self.assertEqual((await odomo())["push_devices"], 0, "switched off at the computer")
        await self.service.dispatch_local_async("local.devices.push", {"device_id": "odomo-a", "enabled": True},
                                                local_authorize=lambda: True)
        self.assertEqual((await odomo())["push_devices"], 1)
        # A credential that no longer works is not a phone that gets pushes,
        # even while its row is still in the push file.
        self.auth.revoke_device("odomo-a")
        self.assertEqual((await odomo())["push_devices"], 0)
        self.service.push_store = None
        status, roads, _ = await self.call("GET", "/v1/host/roads", token=self.native)
        self.assertNotIn("odomo", roads, "a host that cannot say says nothing")

    async def test_unavailable_without_a_store(self):
        _, claim = await self.pair_web()
        self.service.push_store = None
        status, body, _ = await self.call("GET", "/v1/notifications/prefs", token=claim["credential"], origin=self.origin)
        self.assertEqual((status, body["error"]["code"]), (503, "push_unavailable"))

    async def test_root_icons_are_odomo_s_and_need_no_credential(self):
        (Path(self.temp.name) / "odomo/apple-touch-icon.png").write_bytes(b"\x89PNGapple")
        (Path(self.temp.name) / "odomo/icon-192.png").write_bytes(b"\x89PNG192")
        from omodachi_core.odomo import OdomoBundle
        self.service.odomo_bundle = OdomoBundle(Path(self.temp.name) / "odomo")
        await self.server.close()
        from omodachi_core.network import NetworkServer
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        for path, body in (("/apple-touch-icon.png", b"\x89PNGapple"), ("/apple-touch-icon-precomposed.png", b"\x89PNGapple"),
                           ("/apple-touch-icon-120x120-precomposed.png", b"\x89PNGapple"), ("/favicon.ico", b"\x89PNG192")):
            status, value, headers = await self.call("GET", path)
            self.assertEqual((status, value, headers["Content-Type"]), (200, body, "image/png"), path)
        status, _, _ = await self.call("GET", "/favicon.ico", host="evil.example")
        self.assertEqual(status, 403)
        status, _, _ = await self.call("GET", "/apple-touch-icon-x.png")
        self.assertEqual(status, 401)

    async def test_the_allow_list_has_the_push_routes_and_nothing_wider(self):
        from omodachi_core import browser_gate
        self.assertTrue(browser_gate.web_allows("GET", "/v1/push/vapid-public-key"))
        self.assertTrue(browser_gate.web_allows("DELETE", "/v1/push/subscriptions"))
        self.assertTrue(browser_gate.web_allows("PUT", "/v1/notifications/prefs"))
        self.assertFalse(browser_gate.web_allows("PUT", "/v1/push/subscriptions"))
        self.assertFalse(browser_gate.web_allows("GET", "/v1/push/subscriptions/x"))


if __name__ == "__main__":
    unittest.main()
