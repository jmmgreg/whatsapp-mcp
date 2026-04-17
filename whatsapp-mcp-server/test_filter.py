"""Unit tests for the ignore-filter SQL helpers in whatsapp.py.

Covers both the config loader (_load_filter_config) and the SQL clause
builder (_chat_filter_clauses). End-to-end SQL behavior is validated
against a temporary SQLite DB shaped like the one the bridge creates.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from whatsapp import _chat_filter_clauses, _load_filter_config


def _make_sample_db(db_path: str) -> None:
    """Seed a DB with the same columns the bridge's filter-branch creates."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TIMESTAMP,
            is_archived BOOLEAN DEFAULT 0,
            is_muted BOOLEAN DEFAULT 0
        );
        CREATE TABLE messages (
            id TEXT,
            chat_jid TEXT,
            sender TEXT,
            content TEXT,
            timestamp TIMESTAMP,
            is_from_me BOOLEAN,
            PRIMARY KEY (id, chat_jid)
        );
        """
    )
    cur.executemany(
        "INSERT INTO chats (jid, name, is_archived, is_muted) VALUES (?, ?, ?, ?)",
        [
            ("a@g.us", "Active",   0, 0),
            ("b@g.us", "Archived", 1, 0),
            ("c@g.us", "Muted",    0, 1),
            ("d@g.us", "Banned",   0, 0),
        ],
    )
    cur.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, is_from_me) VALUES (?, ?, ?, ?, ?)",
        [
            ("m1", "a@g.us", "u1", "hi from active",   0),
            ("m2", "b@g.us", "u2", "hi from archived", 0),
            ("m3", "c@g.us", "u3", "hi from muted",    0),
            ("m4", "d@g.us", "u4", "hi from banned",   0),
        ],
    )
    conn.commit()
    conn.close()


class LoadFilterConfigTests(unittest.TestCase):
    def test_no_env_returns_empty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IGNORE_CONFIG_PATH", None)
            self.assertEqual(_load_filter_config(), {})

    def test_missing_file_is_permissive(self):
        with mock.patch.dict(os.environ, {"IGNORE_CONFIG_PATH": "/nope/nada.json"}):
            self.assertEqual(_load_filter_config(), {})

    def test_malformed_json_is_permissive(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not-json")
            path = f.name
        try:
            with mock.patch.dict(os.environ, {"IGNORE_CONFIG_PATH": path}):
                self.assertEqual(_load_filter_config(), {})
        finally:
            os.unlink(path)

    def test_valid_config(self):
        payload = {"ignoreArchived": True, "ignoreMuted": False, "ignoredJids": ["x@g.us", "", "y@g.us"]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            path = f.name
        try:
            with mock.patch.dict(os.environ, {"IGNORE_CONFIG_PATH": path}):
                cfg = _load_filter_config()
                self.assertTrue(cfg["ignoreArchived"])
                self.assertFalse(cfg["ignoreMuted"])
                self.assertEqual(cfg["ignoredJids"], ["x@g.us", "y@g.us"])  # empty string stripped
        finally:
            os.unlink(path)


class ChatFilterClausesTests(unittest.TestCase):
    def test_empty_config(self):
        clauses, params = _chat_filter_clauses({})
        self.assertEqual(clauses, [])
        self.assertEqual(params, [])

    def test_all_flags(self):
        clauses, params = _chat_filter_clauses(
            {"ignoreArchived": True, "ignoreMuted": True, "ignoredJids": ["x@g.us", "y@g.us"]}
        )
        self.assertEqual(len(clauses), 3)
        self.assertIn("is_archived", clauses[0])
        self.assertIn("is_muted", clauses[1])
        self.assertIn("NOT IN", clauses[2])
        self.assertEqual(params, ["x@g.us", "y@g.us"])

    def test_jids_only(self):
        clauses, params = _chat_filter_clauses({"ignoredJids": ["x@g.us"]})
        self.assertEqual(len(clauses), 1)
        self.assertIn("NOT IN", clauses[0])
        self.assertEqual(params, ["x@g.us"])

    def test_sql_end_to_end(self):
        """Apply the clauses to a real SQLite query and verify row filtering."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "messages.db")
            _make_sample_db(db_path)
            cfg = {"ignoreArchived": True, "ignoreMuted": True, "ignoredJids": ["d@g.us"]}
            clauses, params = _chat_filter_clauses(cfg, "chats")

            sql = "SELECT chats.jid FROM messages JOIN chats ON messages.chat_jid = chats.jid"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY chats.jid"

            conn = sqlite3.connect(db_path)
            try:
                rows = [r[0] for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()

            # b (archived), c (muted), d (explicitly ignored) all dropped; only a survives.
            self.assertEqual(rows, ["a@g.us"])


if __name__ == "__main__":
    unittest.main()
