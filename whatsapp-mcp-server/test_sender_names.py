"""Tests for sender-name resolution via whatsmeow_contacts.

Validates the COALESCE priority chain (full_name > first_name > chats.name >
push_name > JID) and the graceful fallback when whatsmeow.db is absent.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock


# Schema helpers --------------------------------------------------------------

_MESSAGES_SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TIMESTAMP
);
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, content TEXT,
    timestamp TIMESTAMP, is_from_me BOOLEAN, media_type TEXT,
    filename TEXT, url TEXT,
    PRIMARY KEY (id, chat_jid)
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


def _seed_messages_db(path: str, chats=None, messages=None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_MESSAGES_SCHEMA)
    for c in chats or []:
        conn.execute("INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)", c)
    for m in messages or []:
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            m,
        )
    conn.commit()
    conn.close()


def _seed_whatsmeow_db(path: str, contacts=None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_WHATSMEOW_SCHEMA)
    for c in contacts or []:
        conn.execute(
            "INSERT INTO whatsmeow_contacts "
            "(our_jid, their_jid, first_name, full_name, push_name, business_name, redacted_phone) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            c,
        )
    conn.commit()
    conn.close()


class _Harness:
    """Spin up temp DBs and reload whatsapp.py with patched paths."""

    def __init__(self, with_whatsmeow: bool = True):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.messages_db = os.path.join(self.tmpdir.name, "messages.db")
        self.whatsmeow_db = os.path.join(self.tmpdir.name, "whatsapp.db") if with_whatsmeow else os.path.join(self.tmpdir.name, "missing.db")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.tmpdir.cleanup()

    def load_module(self):
        import importlib
        import whatsapp
        importlib.reload(whatsapp)
        whatsapp.MESSAGES_DB_PATH = self.messages_db
        whatsapp.WHATSMEOW_DB_PATH = self.whatsmeow_db
        return whatsapp


# ---------------------------------------------------------------------------


class SenderNamePriorityTests(unittest.TestCase):
    """Table-driven priority assertions against get_sender_name()."""

    JID = "5521998877665@s.whatsapp.net"

    def _run(self, *, full, first, push, chat_name) -> str:
        with _Harness() as h:
            _seed_messages_db(
                h.messages_db,
                chats=[(self.JID, chat_name, None)] if chat_name is not None else [],
            )
            _seed_whatsmeow_db(
                h.whatsmeow_db,
                contacts=[("our", self.JID, first, full, push, "", "")],
            )
            wa = h.load_module()
            return wa.get_sender_name(self.JID)

    def test_full_name_wins(self):
        self.assertEqual(self._run(full="Victor Jager", first="Vic", push="vj", chat_name="chats-row"), "Victor Jager")

    def test_first_name_when_full_missing(self):
        self.assertEqual(self._run(full="", first="Vic", push="vj", chat_name="chats-row"), "Vic")

    def test_chats_name_when_address_book_missing(self):
        self.assertEqual(self._run(full="", first="", push="vj", chat_name="chats-row"), "chats-row")

    def test_push_name_when_no_address_book_and_no_chat(self):
        self.assertEqual(self._run(full="", first="", push="vj", chat_name=None), "vj")

    def test_jid_when_nothing_available(self):
        with _Harness() as h:
            _seed_messages_db(h.messages_db)
            _seed_whatsmeow_db(h.whatsmeow_db)
            wa = h.load_module()
            self.assertEqual(wa.get_sender_name(self.JID), self.JID)


class ListMessagesIntegrationTests(unittest.TestCase):
    def test_sender_name_populated_for_group_member_without_direct_chat(self):
        """The case that motivated this feature: a sender who only appears
        in a group chat (no direct chat, no row in `chats` with their name)
        should still resolve to their address-book name."""
        group_jid = "120363000000000000@g.us"
        sender_jid = "5521998877665@s.whatsapp.net"
        with _Harness() as h:
            _seed_messages_db(
                h.messages_db,
                chats=[(group_jid, "The Group", "2026-04-17 10:00:00")],
                messages=[(
                    "m1", group_jid, sender_jid, "hey everyone",
                    "2026-04-17 10:00:00", 0, None,
                )],
            )
            _seed_whatsmeow_db(
                h.whatsmeow_db,
                contacts=[("our", sender_jid, "", "Victor Jager", "vj", "", "")],
            )
            wa = h.load_module()
            messages = wa.list_messages(chat_jid=group_jid, include_context=False)
            # list_messages returns a formatted string, not Message objects,
            # so assert on the rendered output.
            self.assertIn("From: Victor Jager", messages)

    def test_graceful_without_whatsmeow_db(self):
        """MCP must still work if whatsmeow.db is missing (e.g. dev setup)."""
        chat_jid = "5521000@s.whatsapp.net"
        with _Harness(with_whatsmeow=False) as h:
            _seed_messages_db(
                h.messages_db,
                chats=[(chat_jid, "Fallback Name", "2026-04-17 10:00:00")],
                messages=[("m1", chat_jid, chat_jid, "hi", "2026-04-17 10:00:00", 0, None)],
            )
            wa = h.load_module()
            messages = wa.list_messages(chat_jid=chat_jid, include_context=False)
            # Falls back to chats.name
            self.assertIn("From: Fallback Name", messages)


class ListChatsIntegrationTests(unittest.TestCase):
    def test_last_sender_name_populated(self):
        group_jid = "120363000000000000@g.us"
        sender_jid = "5521998877665@s.whatsapp.net"
        with _Harness() as h:
            _seed_messages_db(
                h.messages_db,
                chats=[(group_jid, "The Group", "2026-04-17 10:00:00")],
                messages=[("m1", group_jid, sender_jid, "latest msg",
                           "2026-04-17 10:00:00", 0, None)],
            )
            _seed_whatsmeow_db(
                h.whatsmeow_db,
                contacts=[("our", sender_jid, "", "Victor Jager", "vj", "", "")],
            )
            wa = h.load_module()
            chats = wa.list_chats(limit=10)
            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].last_sender_name, "Victor Jager")


if __name__ == "__main__":
    unittest.main()
