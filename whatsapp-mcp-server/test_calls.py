"""Tests for list_calls and its caller-name resolution."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime


_MESSAGES_SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TIMESTAMP
);
CREATE TABLE calls (
    id TEXT PRIMARY KEY,
    chat_jid TEXT,
    from_jid TEXT,
    timestamp TIMESTAMP,
    duration_sec INTEGER DEFAULT 0,
    is_incoming BOOLEAN,
    is_video BOOLEAN,
    is_group BOOLEAN,
    result TEXT
);
"""

_WHATSMEOW_SCHEMA = """
CREATE TABLE whatsmeow_contacts (
    our_jid TEXT, their_jid TEXT,
    first_name TEXT, full_name TEXT, push_name TEXT,
    business_name TEXT, redacted_phone TEXT,
    PRIMARY KEY (our_jid, their_jid)
);
"""


def _seed(msg_path, wa_path, chats=None, calls=None, contacts=None):
    c1 = sqlite3.connect(msg_path)
    c1.executescript(_MESSAGES_SCHEMA)
    for row in chats or []:
        c1.execute("INSERT INTO chats (jid, name) VALUES (?, ?)", row)
    for row in calls or []:
        c1.execute(
            "INSERT INTO calls (id, chat_jid, from_jid, timestamp, duration_sec, "
            "is_incoming, is_video, is_group, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    c1.commit(); c1.close()

    if wa_path:
        c2 = sqlite3.connect(wa_path)
        c2.executescript(_WHATSMEOW_SCHEMA)
        for row in contacts or []:
            c2.execute(
                "INSERT INTO whatsmeow_contacts (our_jid, their_jid, first_name, full_name, push_name, business_name, redacted_phone) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        c2.commit(); c2.close()


class _Harness:
    def __init__(self, with_wa=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.msg = os.path.join(self.tmp.name, "messages.db")
        self.wa = os.path.join(self.tmp.name, "whatsapp.db") if with_wa else os.path.join(self.tmp.name, "missing.db")

    def __enter__(self): return self
    def __exit__(self, *a): self.tmp.cleanup()

    def load(self):
        import importlib, whatsapp
        importlib.reload(whatsapp)
        whatsapp.MESSAGES_DB_PATH = self.msg
        whatsapp.WHATSMEOW_DB_PATH = self.wa
        return whatsapp


class ListCallsTests(unittest.TestCase):
    def test_resolves_caller_name_from_contacts(self):
        jid = "33609964472@s.whatsapp.net"
        ts = "2026-04-17 10:00:00"
        with _Harness() as h:
            _seed(h.msg, h.wa,
                  calls=[("call-1", jid, jid, ts, 42, 1, 0, 0, "connected")],
                  contacts=[("our", jid, "", "Maman", "", "", "")])
            wa = h.load()
            calls = wa.list_calls(limit=10)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].from_name, "Maman")
            self.assertEqual(calls[0].duration_sec, 42)
            self.assertEqual(calls[0].result, "connected")

    def test_missed_only_filter(self):
        jid = "33609964472@s.whatsapp.net"
        with _Harness() as h:
            _seed(h.msg, h.wa, calls=[
                ("c-conn", jid, jid, "2026-04-17 10:00:00", 30, 1, 0, 0, "connected"),
                ("c-miss", jid, jid, "2026-04-17 11:00:00", 0, 1, 0, 0, "missed"),
            ])
            wa = h.load()
            self.assertEqual(len(wa.list_calls(missed_only=True)), 1)
            self.assertEqual(len(wa.list_calls(missed_only=False)), 2)

    def test_after_before_filters(self):
        jid = "33609964472@s.whatsapp.net"
        with _Harness() as h:
            _seed(h.msg, h.wa, calls=[
                ("c1", jid, jid, "2026-04-15 10:00:00", 0, 1, 0, 0, "missed"),
                ("c2", jid, jid, "2026-04-17 10:00:00", 0, 1, 0, 0, "missed"),
                ("c3", jid, jid, "2026-04-19 10:00:00", 0, 1, 0, 0, "missed"),
            ])
            wa = h.load()
            result = wa.list_calls(after="2026-04-16", before="2026-04-18")
            self.assertEqual([c.id for c in result], ["c2"])

    def test_chat_jid_filter(self):
        group = "120363@g.us"
        direct = "33@s.whatsapp.net"
        with _Harness() as h:
            _seed(h.msg, h.wa, calls=[
                ("c-g", group, direct, "2026-04-17 10:00:00", 0, 1, 1, 1, "missed"),
                ("c-d", direct, direct, "2026-04-17 11:00:00", 0, 1, 0, 0, "missed"),
            ])
            wa = h.load()
            result = wa.list_calls(chat_jid=group)
            self.assertEqual([c.id for c in result], ["c-g"])

    def test_works_without_whatsmeow_db(self):
        """Fallback path: no whatsmeow.db — should still list calls, resolving
        names from chats.name only."""
        jid = "33609964472@s.whatsapp.net"
        with _Harness(with_wa=False) as h:
            _seed(h.msg, None,
                  chats=[(jid, "Maman (chats.name)")],
                  calls=[("call-1", jid, jid, "2026-04-17 10:00:00", 10, 1, 0, 0, "connected")])
            wa = h.load()
            calls = wa.list_calls(limit=10)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].from_name, "Maman (chats.name)")

    def test_format_rendering(self):
        jid = "33609964472@s.whatsapp.net"
        with _Harness() as h:
            _seed(h.msg, h.wa,
                  calls=[
                      ("c-miss", jid, jid, "2026-04-17 10:30:00", 0, 1, 0, 0, "missed"),
                      ("c-conn", jid, jid, "2026-04-17 11:00:00", 42, 1, 1, 0, "connected"),
                  ],
                  contacts=[("our", jid, "", "Maman", "", "", "")])
            wa = h.load()
            out = wa.format_calls_list(wa.list_calls(limit=10))
            # Most recent first
            self.assertIn("← Maman (video, connected 42s)", out)
            self.assertIn("← Maman (voice, missed)", out)
            # connected line is first (DESC by timestamp)
            self.assertLess(out.index("connected"), out.index("missed"))


if __name__ == "__main__":
    unittest.main()
