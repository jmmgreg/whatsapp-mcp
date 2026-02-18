#!/usr/bin/env python3
"""One-time migration: resolve all @lid JIDs in messages.db using whatsmeow's lid_map from whatsapp.db."""

import sqlite3
import os

STORE_DIR = os.path.join(os.path.dirname(__file__), "whatsapp-bridge", "store")
WHATSMEOW_DB = os.path.join(STORE_DIR, "whatsapp.db")
MESSAGES_DB = os.path.join(STORE_DIR, "messages.db")


def load_lid_map(whatsmeow_db_path):
    """Load the LID→phone mapping from whatsmeow's database."""
    conn = sqlite3.connect(whatsmeow_db_path)
    cur = conn.cursor()
    cur.execute("SELECT lid, pn FROM whatsmeow_lid_map")
    lid_map = {}
    for lid, pn in cur.fetchall():
        lid_map[lid] = pn
    conn.close()
    print(f"Loaded {len(lid_map)} LID→phone mappings from whatsmeow")
    return lid_map


def migrate_messages_db(messages_db_path, lid_map):
    """Resolve @lid JIDs in chats and messages tables."""
    conn = sqlite3.connect(messages_db_path)
    cur = conn.cursor()

    # Build a full JID mapping: "12345@lid" → "33612345678@s.whatsapp.net"
    jid_map = {}
    user_map = {}  # bare user mapping: "12345" → "33612345678"
    for lid_jid, pn_jid in lid_map.items():
        jid_map[lid_jid] = pn_jid
        # Also build bare user mapping (strip server part)
        lid_user = lid_jid.split("@")[0] if "@" in lid_jid else lid_jid
        pn_user = pn_jid.split("@")[0] if "@" in pn_jid else pn_jid
        user_map[lid_user] = pn_user

    # --- Migrate chats table (jid column) ---
    cur.execute("SELECT jid FROM chats WHERE jid LIKE '%@lid'")
    lid_chats = cur.fetchall()
    print(f"\nChats with @lid JID: {len(lid_chats)}")

    chats_updated = 0
    for (chat_jid,) in lid_chats:
        # lid_map keys are bare numbers, chat JIDs have @lid suffix — strip for lookup
        bare_lid = chat_jid.replace("@lid", "") if chat_jid.endswith("@lid") else chat_jid
        if bare_lid in user_map:
            phone_user = user_map[bare_lid]
            new_jid = f"{phone_user}@s.whatsapp.net"
            # Check if the target JID already exists (avoid duplicates)
            cur.execute("SELECT COUNT(*) FROM chats WHERE jid = ?", (new_jid,))
            if cur.fetchone()[0] > 0:
                # Target exists — merge: delete duplicate messages first, then move remaining
                cur.execute("""DELETE FROM messages WHERE chat_jid = ? AND id IN (
                    SELECT m1.id FROM messages m1
                    INNER JOIN messages m2 ON m1.id = m2.id
                    WHERE m1.chat_jid = ? AND m2.chat_jid = ?
                )""", (chat_jid, chat_jid, new_jid))
                dupes = cur.rowcount
                cur.execute("UPDATE messages SET chat_jid = ? WHERE chat_jid = ?", (new_jid, chat_jid))
                moved = cur.rowcount
                cur.execute("DELETE FROM chats WHERE jid = ?", (chat_jid,))
                print(f"  MERGED: {chat_jid} → {new_jid} ({dupes} dupes removed, {moved} moved)")
            else:
                cur.execute("UPDATE chats SET jid = ? WHERE jid = ?", (new_jid, chat_jid))
                cur.execute("UPDATE messages SET chat_jid = ? WHERE chat_jid = ?", (new_jid, chat_jid))
                print(f"  RENAMED: {chat_jid} → {new_jid}")
            chats_updated += 1
        elif chat_jid in jid_map:
            # Try full JID match
            new_jid = jid_map[chat_jid]
            cur.execute("SELECT COUNT(*) FROM chats WHERE jid = ?", (new_jid,))
            if cur.fetchone()[0] > 0:
                cur.execute("""DELETE FROM messages WHERE chat_jid = ? AND id IN (
                    SELECT m1.id FROM messages m1
                    INNER JOIN messages m2 ON m1.id = m2.id
                    WHERE m1.chat_jid = ? AND m2.chat_jid = ?
                )""", (chat_jid, chat_jid, new_jid))
                dupes = cur.rowcount
                cur.execute("UPDATE messages SET chat_jid = ? WHERE chat_jid = ?", (new_jid, chat_jid))
                moved = cur.rowcount
                cur.execute("DELETE FROM chats WHERE jid = ?", (chat_jid,))
                print(f"  MERGED: {chat_jid} → {new_jid} ({dupes} dupes removed, {moved} moved)")
            else:
                cur.execute("UPDATE chats SET jid = ? WHERE jid = ?", (new_jid, chat_jid))
                cur.execute("UPDATE messages SET chat_jid = ? WHERE chat_jid = ?", (new_jid, chat_jid))
                print(f"  RENAMED: {chat_jid} → {new_jid}")
            chats_updated += 1
        else:
            print(f"  NO MAPPING: {chat_jid}")

    print(f"Chats updated: {chats_updated}/{len(lid_chats)}")

    # --- Migrate messages table (sender column) ---
    # Senders can be bare LID users (just the number part) or full JIDs
    cur.execute("SELECT DISTINCT sender FROM messages WHERE sender LIKE '%@lid'")
    lid_senders_full = cur.fetchall()
    print(f"\nDistinct senders with full @lid JID: {len(lid_senders_full)}")

    senders_updated = 0
    for (sender,) in lid_senders_full:
        if sender in jid_map:
            new_sender = jid_map[sender]
            cur.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new_sender, sender))
            senders_updated += 1
        else:
            # Try bare user lookup
            user_part = sender.split("@")[0] if "@" in sender else sender
            if user_part in user_map:
                new_sender = user_map[user_part]
                cur.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new_sender, sender))
                senders_updated += 1

    print(f"Full @lid senders updated: {senders_updated}/{len(lid_senders_full)}")

    # Also check bare LID users (no @lid suffix but matches LID user IDs)
    # These are stored as just the numeric part
    cur.execute("SELECT DISTINCT sender FROM messages")
    all_senders = [row[0] for row in cur.fetchall()]
    bare_updated = 0
    for sender in all_senders:
        if "@" not in sender and sender in user_map:
            new_sender = user_map[sender]
            cur.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new_sender, sender))
            bare_updated += 1

    print(f"Bare LID user senders updated: {bare_updated}")

    conn.commit()
    conn.close()
    print(f"\nMigration complete. Total: {chats_updated} chats, {senders_updated + bare_updated} sender mappings updated.")


if __name__ == "__main__":
    if not os.path.exists(WHATSMEOW_DB):
        print(f"ERROR: whatsmeow database not found at {WHATSMEOW_DB}")
        exit(1)
    if not os.path.exists(MESSAGES_DB):
        print(f"ERROR: messages database not found at {MESSAGES_DB}")
        exit(1)

    lid_map = load_lid_map(WHATSMEOW_DB)
    migrate_messages_db(MESSAGES_DB, lid_map)
