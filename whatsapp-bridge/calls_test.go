package main

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
)

func TestCallResultToString(t *testing.T) {
	tests := []struct {
		in   waSyncAction.CallLogRecord_CallResult
		want string
	}{
		{waSyncAction.CallLogRecord_CONNECTED, "connected"},
		{waSyncAction.CallLogRecord_MISSED, "missed"},
		{waSyncAction.CallLogRecord_REJECTED, "rejected"},
		{waSyncAction.CallLogRecord_CANCELLED, "cancelled"},
		{waSyncAction.CallLogRecord_FAILED, "failed"},
		{waSyncAction.CallLogRecord_ONGOING, "ongoing"},
		{waSyncAction.CallLogRecord_CallResult(9999), "unknown"},
	}
	for _, tc := range tests {
		if got := callResultToString(tc.in); got != tc.want {
			t.Errorf("callResultToString(%v) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// TestStoreCallUpsert verifies the INSERT OR REPLACE behavior we rely on for
// deduping history-sync rows against live-event rows for the same CallID.
func TestStoreCallUpsert(t *testing.T) {
	dir := t.TempDir()
	// Temporarily cd into dir so NewMessageStore's relative "store/" path works.
	prevWD, _ := os.Getwd()
	defer os.Chdir(prevWD)
	if err := os.Chdir(dir); err != nil {
		t.Fatalf("chdir: %v", err)
	}

	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore: %v", err)
	}
	defer store.Close()

	callID := "abc-123"
	ts := time.Date(2026, 4, 17, 12, 0, 0, 0, time.UTC)

	// 1. Live ring: stub row with result "ringing", zero duration.
	if err := store.StoreCall(callID, "33609964472@s.whatsapp.net", "33609964472@s.whatsapp.net",
		ts, 0, true, false, false, "ringing"); err != nil {
		t.Fatalf("StoreCall ringing: %v", err)
	}

	// 2. Later, history sync delivers the authoritative record (connected, 42s duration).
	if err := store.StoreCall(callID, "33609964472@s.whatsapp.net", "33609964472@s.whatsapp.net",
		ts, 42, true, false, false, "connected"); err != nil {
		t.Fatalf("StoreCall connected: %v", err)
	}

	// Assert: exactly one row, with the later values (upsert, not duplicate).
	dbPath := filepath.Join(dir, "store", "messages.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer db.Close()

	var count int
	if err := db.QueryRow("SELECT COUNT(*) FROM calls WHERE id = ?", callID).Scan(&count); err != nil {
		t.Fatalf("count: %v", err)
	}
	if count != 1 {
		t.Errorf("expected exactly 1 row after upsert, got %d", count)
	}

	var result string
	var duration int64
	if err := db.QueryRow("SELECT result, duration_sec FROM calls WHERE id = ?", callID).Scan(&result, &duration); err != nil {
		t.Fatalf("select: %v", err)
	}
	if result != "connected" || duration != 42 {
		t.Errorf("upsert not effective: result=%q duration=%d; want connected/42", result, duration)
	}
}

// TestStoreCallEmptyIDIsNoop — protects against accidentally writing rows with
// empty primary keys, which would collapse every "no-id" call into one row.
func TestStoreCallEmptyIDIsNoop(t *testing.T) {
	dir := t.TempDir()
	prevWD, _ := os.Getwd()
	defer os.Chdir(prevWD)
	_ = os.Chdir(dir)

	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore: %v", err)
	}
	defer store.Close()

	if err := store.StoreCall("", "x", "x", time.Now(), 0, true, false, false, "ringing"); err != nil {
		t.Fatalf("StoreCall with empty id returned error: %v", err)
	}

	db, _ := sql.Open("sqlite3", filepath.Join(dir, "store", "messages.db"))
	defer db.Close()
	var count int
	_ = db.QueryRow("SELECT COUNT(*) FROM calls").Scan(&count)
	if count != 0 {
		t.Errorf("expected 0 rows after empty-id store, got %d", count)
	}
}
