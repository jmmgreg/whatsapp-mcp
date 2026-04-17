import os
import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
# whatsmeow's own database — holds the `whatsmeow_contacts` table the bridge
# never copies into messages.db. We attach it read-only to resolve sender
# names without having to keep two copies of the contact store in sync.
WHATSMEOW_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'whatsapp.db')


# Bridge REST API URL. Honors WHATSAPP_API_BASE_URL (full URL) or WHATSAPP_BRIDGE_PORT
# (port only, assumes localhost). Default: http://localhost:8080/api.
def _resolve_bridge_url() -> str:
    full = os.environ.get("WHATSAPP_API_BASE_URL", "").strip()
    if full:
        return full.rstrip("/")
    port = os.environ.get("WHATSAPP_BRIDGE_PORT", "").strip()
    if port and port.isdigit() and int(port) > 0:
        return f"http://localhost:{port}/api"
    return "http://localhost:8080/api"

WHATSAPP_API_BASE_URL = _resolve_bridge_url()


# -------- Ignore filter (optional) --------
# Loads the same JSON schema as the Go bridge's --ignore-config. Path precedence:
# IGNORE_CONFIG_PATH env var → no filter. Config is re-read on every call so
# edits to the file take effect immediately without restarting the MCP server.


def _load_filter_config() -> dict:
    """Return a dict with keys ignoreArchived, ignoreMuted, ignoredJids.

    Empty dict means "no filtering" — callers treat that as permissive.
    """
    path = os.environ.get("IGNORE_CONFIG_PATH", "").strip()
    if not path:
        return {}
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {
        "ignoreArchived": bool(cfg.get("ignoreArchived")),
        "ignoreMuted": bool(cfg.get("ignoreMuted")),
        "ignoredJids": [j for j in cfg.get("ignoredJids", []) if isinstance(j, str) and j],
    }


def _chat_filter_clauses(cfg: dict, chat_alias: str = "chats") -> Tuple[List[str], List[object]]:
    """Build SQL WHERE fragments that exclude filtered chats.

    Returns (clauses, params) to be AND-joined into the caller's query. The
    alias lets callers with a different table alias (e.g. JOIN aliases) use
    the same helper.
    """
    clauses: List[str] = []
    params: List[object] = []
    if not cfg:
        return clauses, params

    if cfg.get("ignoreArchived"):
        clauses.append(f"({chat_alias}.is_archived = 0 OR {chat_alias}.is_archived IS NULL)")
    if cfg.get("ignoreMuted"):
        clauses.append(f"({chat_alias}.is_muted = 0 OR {chat_alias}.is_muted IS NULL)")
    jids = cfg.get("ignoredJids") or []
    if jids:
        placeholders = ",".join("?" for _ in jids)
        clauses.append(f"{chat_alias}.jid NOT IN ({placeholders})")
        params.extend(jids)
    return clauses, params


def _connect_with_contacts() -> Tuple[sqlite3.Connection, bool]:
    """Open messages.db and try to ATTACH whatsmeow.db as schema `wa`.

    Returns (connection, contacts_available). When contacts are not
    available (whatsmeow.db missing/locked/old schema), callers skip the
    LEFT JOIN and fall back to the pre-existing chats.name / JID path so
    everything still works with only messages.db present.
    """
    conn = sqlite3.connect(MESSAGES_DB_PATH)
    contacts_available = False
    if os.path.exists(WHATSMEOW_DB_PATH):
        try:
            conn.execute("ATTACH DATABASE ? AS wa", (WHATSMEOW_DB_PATH,))
            conn.execute("SELECT 1 FROM wa.whatsmeow_contacts LIMIT 1")
            contacts_available = True
        except sqlite3.Error:
            pass
    return conn, contacts_available


# SQL expression that resolves a sender JID to a display name.
# Priority (per Jean's spec):
#   full_name (address book) > first_name (address book) > chats.name
#   > push_name (self-assigned WhatsApp name) > JID
# NULLIF('', '') → NULL so empty-string columns fall through to the next choice.
_WITH_CONTACTS_NAME_EXPR = """COALESCE(
    NULLIF(wc.full_name,  ''),
    NULLIF(wc.first_name, ''),
    NULLIF({chat_name},   ''),
    NULLIF(wc.push_name,  ''),
    {sender}
)"""

_WITHOUT_CONTACTS_NAME_EXPR = "COALESCE(NULLIF({chat_name}, ''), {sender})"


def _sender_name_expr(contacts_available: bool, sender_col: str, chat_name_col: str) -> str:
    """Render the SQL expression used to derive `sender_name` in list queries.

    Parameters let callers use this with different column aliases — e.g.
    `messages.sender` vs `last_msg.sender` in list_chats.
    """
    tpl = _WITH_CONTACTS_NAME_EXPR if contacts_available else _WITHOUT_CONTACTS_NAME_EXPR
    return tpl.format(sender=sender_col, chat_name=chat_name_col)


@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None
    sender_name: Optional[str] = None  # Resolved per _sender_name_expr priority

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_sender_name: Optional[str] = None  # Resolved per _sender_name_expr priority
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

def get_sender_name(sender_jid: str) -> str:
    """Resolve a sender JID to a human-readable display name.

    Priority:
      1. whatsmeow_contacts.full_name  (from the phone's address book)
      2. whatsmeow_contacts.first_name (also address book)
      3. chats.name                    (only populated for direct chats &
                                        group names — so this helps when the
                                        JID is a direct-chat counterparty)
      4. whatsmeow_contacts.push_name  (the name the user set on their own WA)
      5. sender_jid                    (last-resort fallback)

    Each COALESCE step ignores empty strings, so a row where the higher-
    priority column is set to '' falls through correctly.
    """
    conn = None
    try:
        conn, contacts_available = _connect_with_contacts()
        cursor = conn.cursor()

        if contacts_available:
            cursor.execute(
                """
                SELECT COALESCE(
                    NULLIF(wc.full_name,  ''),
                    NULLIF(wc.first_name, ''),
                    NULLIF(c.name,        ''),
                    NULLIF(wc.push_name,  ''),
                    ?
                )
                FROM (SELECT ? AS jid) AS q
                LEFT JOIN wa.whatsmeow_contacts wc ON wc.their_jid = q.jid
                LEFT JOIN chats c                 ON c.jid         = q.jid
                LIMIT 1
                """,
                (sender_jid, sender_jid),
            )
        else:
            # Backwards-compatible path: whatsmeow.db unavailable, fall back
            # to the old chats-only lookup so the MCP still returns something
            # sensible in a messages-only deployment.
            cursor.execute(
                "SELECT name FROM chats WHERE jid = ? LIMIT 1",
                (sender_jid,),
            )

        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
        return sender_jid
    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if conn is not None:
            conn.close()

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    
    try:
        if message.is_from_me:
            sender_name = "Me"
        elif message.sender_name:
            # list_messages already resolved this via the whatsmeow JOIN —
            # don't re-query per message.
            sender_name = message.sender_name
        else:
            sender_name = get_sender_name(message.sender)
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn, contacts_available = _connect_with_contacts()
        cursor = conn.cursor()

        # Build base query. sender_name is derived via a COALESCE chain over
        # the whatsmeow contact store + a *separate* chats lookup keyed by the
        # sender JID — we can't reuse the outer `chats` join because that's
        # keyed by the chat JID (which is the group for group messages, not
        # the person). sender_chat.name is non-null for direct-chat people.
        sender_name_sql = _sender_name_expr(contacts_available, "messages.sender", "sender_chat.name")
        query_parts = [
            f"SELECT messages.timestamp, messages.sender, chats.name, messages.content, "
            f"messages.is_from_me, chats.jid, messages.id, messages.media_type, "
            f"{sender_name_sql} AS sender_name FROM messages",
            "JOIN chats ON messages.chat_jid = chats.jid",
            "LEFT JOIN chats sender_chat ON sender_chat.jid = messages.sender",
        ]
        if contacts_available:
            query_parts.append("LEFT JOIN wa.whatsmeow_contacts wc ON wc.their_jid = messages.sender")
        where_clauses = []
        params = []

        # Apply ignore filter (no-op if IGNORE_CONFIG_PATH is unset or empty).
        _filter_clauses, _filter_params = _chat_filter_clauses(_load_filter_config(), "chats")
        where_clauses.extend(_filter_clauses)
        params.extend(_filter_params)

        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7],
                sender_name=msg[8]
            )
            result.append(message)
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn, contacts_available = _connect_with_contacts()
        cursor = conn.cursor()

        # Derive last_sender_name via the same priority chain list_messages
        # uses. When the last-message JOIN isn't present (include_last_message
        # False), there's no sender to resolve — last_sender_name stays NULL.
        # sender_chat is a *separate* alias into chats keyed by the sender
        # JID (not the group JID) so direct-chat contacts still fall back
        # correctly when no whatsmeow row exists.
        last_sender_name_sql = (
            _sender_name_expr(contacts_available, "messages.sender", "sender_chat.name")
            if include_last_message else "NULL"
        )
        query_parts = [f"""
            SELECT
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me,
                {last_sender_name_sql} as last_sender_name
            FROM chats
        """]

        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid
                AND chats.last_message_time = messages.timestamp
            """)
            query_parts.append(
                "LEFT JOIN chats sender_chat ON sender_chat.jid = messages.sender"
            )
            if contacts_available:
                query_parts.append(
                    "LEFT JOIN wa.whatsmeow_contacts wc ON wc.their_jid = messages.sender"
                )
            
        where_clauses = []
        params = []

        # Apply ignore filter (no-op if IGNORE_CONFIG_PATH is unset or empty).
        _filter_clauses, _filter_params = _chat_filter_clauses(_load_filter_config(), "chats")
        where_clauses.extend(_filter_clauses)
        params.extend(_filter_params)

        if query:
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5],
                last_sender_name=chat_data[6] if len(chat_data) > 6 else None
            )
            result.append(chat)

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Split query into characters to support partial matching
        search_pattern = '%' +query + '%'
        
        cursor.execute("""
            SELECT DISTINCT 
                jid,
                name
            FROM chats
            WHERE 
                (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                AND jid NOT LIKE '%@g.us'
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern))
        
        contacts = cursor.fetchall()
        
        result = []
        for contact_data in contacts:
            contact = Contact(
                phone_number=contact_data[0].split('@')[0],
                name=contact_data[1],
                jid=contact_data[0]
            )
            result.append(contact)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn, contacts_available = _connect_with_contacts()
        cursor = conn.cursor()

        # sender_chat keyed on the sender JID (not c.jid, which is the group)
        # so fallback to chats.name works for direct-chat counterparties.
        last_sender_name_sql = _sender_name_expr(contacts_available, "m.sender", "sender_chat.name")
        join_wc = "LEFT JOIN wa.whatsmeow_contacts wc ON wc.their_jid = m.sender" if contacts_available else ""

        cursor.execute(f"""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me,
                {last_sender_name_sql} as last_sender_name
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            LEFT JOIN chats sender_chat ON sender_chat.jid = m.sender
            {join_wc}
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (jid, jid, limit, page * limit))

        chats = cursor.fetchall()

        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5],
                last_sender_name=chat_data[6]
            )
            result.append(chat)


        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (jid, jid))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        query += " WHERE c.jid = ?"
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{sender_phone_number}%",))
        
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None
