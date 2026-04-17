#!/usr/bin/env python3
"""One-time migration for existing whatsapp-mcp databases.

Does two things to make the sender/chat JID columns consistent with the
new storage logic in the Go bridge:

  1. Resolves all `@lid` JIDs in `messages.db` using whatsmeow's `lid_map`
     in `whatsapp.db`. Chats with a LID JID that already has a phone-based
     counterpart are merged (messages moved, duplicates pruned).

  2. Normalizes every `sender` value to full JID form. Historically the
     bridge stored senders as a bare user portion (e.g. "33609553753")
     from handleMessage, but as a full JID (e.g. "5521966683939@s.whatsapp.net")
     from handleHistorySync's Participant path. That split the same person's
     messages across two keys. This pass rewrites every bare-user sender
     to `<user>@s.whatsapp.net`.

Run from the repo root after upgrading:
    python3 migrate-lids.py
"""

import sqlite3
import os
import sys

# Resolve store directory: honor $WA_STORE_DIR, else use <repo>/whatsapp-bridge/store
STORE_DIR = os.environ.get("WA_STORE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "whatsapp-bridge", "store"
)
WHATSMEOW_DB = os.path.join(STORE_DIR, "whatsapp.db")
MESSAGES_DB = os.path.join(STORE_DIR, "messages.db")


def load_lid_map(whatsmeow_db_path):
    conn = sqlite3.connect(whatsmeow_db_path)
    cur = conn.cursor()
    cur.execute("SELECT lid, pn FROM whatsmeow_lid_map")
    lid_map = dict(cur.fetchall())
    conn.close()
    print(f"Loaded {len(lid_map)} LID→phone mappings from whatsmeow")
    return lid_map


def migrate_lids(cur, lid_map):
    """Rewrite @lid JIDs to their phone-based counterparts in chats + messages."""
    # Build full-JID and bare-user maps
    jid_map = {}
    user_map = {}
    for lid_jid, pn_jid in lid_map.items():
        jid_map[lid_jid] = pn_jid
        lid_user = lid_jid.split("@")[0] if "@" in lid_jid else lid_jid
        pn_user = pn_jid.split("@")[0] if "@" in pn_jid else pn_jid
        user_map[lid_user] = pn_user

    # --- chats.jid ---
    cur.execute("SELECT jid FROM chats WHERE jid LIKE '%@lid'")
    lid_chats = cur.fetchall()
    print(f"\nChats with @lid JID: {len(lid_chats)}")
    chats_updated = 0
    for (chat_jid,) in lid_chats:
        bare_lid = chat_jid.replace("@lid", "") if chat_jid.endswith("@lid") else chat_jid
        new_jid = None
        if bare_lid in user_map:
            new_jid = f"{user_map[bare_lid]}@s.whatsapp.net"
        elif chat_jid in jid_map:
            new_jid = jid_map[chat_jid]
        if not new_jid:
            print(f"  NO MAPPING: {chat_jid}")
            continue

        cur.execute("SELECT COUNT(*) FROM chats WHERE jid = ?", (new_jid,))
        if cur.fetchone()[0] > 0:
            # Target already exists — merge. Drop messages that exist in both
            # (same id), then move the rest, then delete the now-empty LID chat.
            cur.execute(
                """DELETE FROM messages WHERE chat_jid = ? AND id IN (
                    SELECT m1.id FROM messages m1
                    INNER JOIN messages m2 ON m1.id = m2.id
                    WHERE m1.chat_jid = ? AND m2.chat_jid = ?
                )""",
                (chat_jid, chat_jid, new_jid),
            )
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
    print(f"Chats updated: {chats_updated}/{len(lid_chats)}")

    # --- messages.sender — full @lid JIDs ---
    cur.execute("SELECT DISTINCT sender FROM messages WHERE sender LIKE '%@lid'")
    lid_senders_full = cur.fetchall()
    senders_updated = 0
    for (sender,) in lid_senders_full:
        new_sender = jid_map.get(sender)
        if not new_sender:
            user_part = sender.split("@")[0] if "@" in sender else sender
            if user_part in user_map:
                new_sender = f"{user_map[user_part]}@s.whatsapp.net"
        if new_sender:
            cur.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new_sender, sender))
            senders_updated += 1
    print(f"\nFull @lid senders updated: {senders_updated}/{len(lid_senders_full)}")

    # --- messages.sender — bare LID users (no @ suffix at all) ---
    cur.execute("SELECT DISTINCT sender FROM messages WHERE sender != '' AND sender NOT LIKE '%@%'")
    bare_senders = [row[0] for row in cur.fetchall()]
    bare_lid_updated = 0
    for sender in bare_senders:
        if sender in user_map:
            new_sender = f"{user_map[sender]}@s.whatsapp.net"
            cur.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new_sender, sender))
            bare_lid_updated += 1
    print(f"Bare LID users resolved: {bare_lid_updated}")

    return chats_updated, senders_updated + bare_lid_updated


def normalize_senders(cur):
    """Append `@s.whatsapp.net` to any sender that is still a bare user portion.

    After LID migration, the remaining bare-user senders are phone numbers
    (legacy handleMessage path). We rewrite them to full JIDs so every row
    has the same format as handleHistorySync's Participant path.
    """
    cur.execute("SELECT DISTINCT sender FROM messages WHERE sender != '' AND sender NOT LIKE '%@%'")
    bare = [row[0] for row in cur.fetchall()]
    if not bare:
        print("\nNo bare-user senders to normalize.")
        return 0
    print(f"\nNormalizing {len(bare)} distinct bare-user sender(s) to full JID form")
    rows_updated = 0
    for sender in bare:
        new_sender = f"{sender}@s.whatsapp.net"
        cur.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new_sender, sender))
        rows_updated += cur.rowcount
    print(f"Sender rows normalized: {rows_updated}")
    return rows_updated


def main():
    if not os.path.exists(WHATSMEOW_DB):
        print(f"ERROR: whatsmeow database not found at {WHATSMEOW_DB}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(MESSAGES_DB):
        print(f"ERROR: messages database not found at {MESSAGES_DB}", file=sys.stderr)
        sys.exit(1)

    lid_map = load_lid_map(WHATSMEOW_DB)
    conn = sqlite3.connect(MESSAGES_DB)
    cur = conn.cursor()

    chats_updated, senders_updated = migrate_lids(cur, lid_map)
    rows_normalized = normalize_senders(cur)

    conn.commit()
    conn.close()
    print(
        f"\nMigration complete. Total: {chats_updated} chats updated, "
        f"{senders_updated} @lid senders resolved, "
        f"{rows_normalized} sender rows normalized to full JID form."
    )


if __name__ == "__main__":
    main()
