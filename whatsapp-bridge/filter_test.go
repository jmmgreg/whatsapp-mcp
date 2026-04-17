package main

import (
	"os"
	"path/filepath"
	"sort"
	"sync"
	"testing"
)

func writeFilterFile(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "ignore.json")
	if err := os.WriteFile(path, []byte(body), 0644); err != nil {
		t.Fatalf("write filter file: %v", err)
	}
	return path
}

func TestNewFilterConfig_NoPath(t *testing.T) {
	cfg, err := newFilterConfig("")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg != nil {
		t.Fatalf("expected nil cfg for empty path, got %+v", cfg)
	}
}

func TestNewFilterConfig_MissingFile(t *testing.T) {
	if _, err := newFilterConfig(filepath.Join(t.TempDir(), "nope.json")); err == nil {
		t.Fatal("expected error for missing file")
	}
}

func TestNewFilterConfig_MalformedJSON(t *testing.T) {
	path := writeFilterFile(t, "{not-json")
	if _, err := newFilterConfig(path); err == nil {
		t.Fatal("expected parse error")
	}
}

func TestFilter_MatchesJID(t *testing.T) {
	path := writeFilterFile(t, `{"ignoredJids": ["g@g.us", "u@s.whatsapp.net", ""]}`)
	cfg, err := newFilterConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.Matches("g@g.us") {
		t.Error("should match explicit JID")
	}
	if !cfg.Matches("u@s.whatsapp.net") {
		t.Error("should match user JID")
	}
	if cfg.Matches("other@g.us") {
		t.Error("should not match non-listed JID")
	}
	// Empty-string entries must not poison the set.
	if cfg.Matches("") {
		t.Error("empty string must never match")
	}
}

func TestFilter_MatchesChatMeta(t *testing.T) {
	path := writeFilterFile(t, `{"ignoreArchived": true, "ignoreMuted": false, "ignoredJids": ["x@g.us"]}`)
	cfg, err := newFilterConfig(path)
	if err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name              string
		jid               string
		archived, muted   bool
		want              bool
	}{
		{"explicitly ignored JID drops regardless of state", "x@g.us", false, false, true},
		{"archived chat drops when ignoreArchived=true", "a@g.us", true, false, true},
		{"muted chat kept when ignoreMuted=false", "b@g.us", false, true, false},
		{"clean chat kept", "c@g.us", false, false, false},
		{"archived+muted chat drops (archive rule fires)", "d@g.us", true, true, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := cfg.MatchesChatMeta(tc.jid, tc.archived, tc.muted); got != tc.want {
				t.Errorf("MatchesChatMeta(%q, %v, %v) = %v; want %v", tc.jid, tc.archived, tc.muted, got, tc.want)
			}
		})
	}
}

func TestFilter_NilReceiverIsPermissive(t *testing.T) {
	var cfg *FilterConfig
	if cfg.Matches("anything@g.us") {
		t.Error("nil filter must not match anything")
	}
	if cfg.MatchesChatMeta("x", true, true) {
		t.Error("nil filter must not match via meta")
	}
	if cfg.shouldCheckMeta() {
		t.Error("nil filter must not ask for meta lookup")
	}
	snap := cfg.Snapshot()
	if snap.IgnoreArchived || snap.IgnoreMuted || len(snap.IgnoredJIDs) != 0 {
		t.Errorf("nil filter snapshot should be empty, got %+v", snap)
	}
}

func TestFilter_Reload(t *testing.T) {
	path := writeFilterFile(t, `{"ignoredJids": ["old@g.us"]}`)
	cfg, err := newFilterConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.Matches("old@g.us") || cfg.Matches("new@g.us") {
		t.Fatal("initial state wrong")
	}

	if err := os.WriteFile(path, []byte(`{"ignoredJids": ["new@g.us"]}`), 0644); err != nil {
		t.Fatal(err)
	}
	if err := cfg.Reload(); err != nil {
		t.Fatal(err)
	}
	if cfg.Matches("old@g.us") {
		t.Error("old JID should no longer match after reload")
	}
	if !cfg.Matches("new@g.us") {
		t.Error("new JID should match after reload")
	}
}

func TestFilter_ReloadIsRaceSafe(t *testing.T) {
	path := writeFilterFile(t, `{"ignoredJids": ["a@g.us"]}`)
	cfg, err := newFilterConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	stop := make(chan struct{})
	// Reader goroutine: hammers Matches concurrently with reloads.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				_ = cfg.Matches("a@g.us")
			}
		}
	}()
	for i := 0; i < 50; i++ {
		if err := cfg.Reload(); err != nil {
			t.Fatalf("reload iteration %d: %v", i, err)
		}
	}
	close(stop)
	wg.Wait()
}

func TestFilter_Snapshot(t *testing.T) {
	path := writeFilterFile(t, `{"ignoreArchived": true, "ignoreMuted": true, "ignoredJids": ["b@g.us", "a@g.us"]}`)
	cfg, err := newFilterConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	snap := cfg.Snapshot()
	if !snap.IgnoreArchived || !snap.IgnoreMuted {
		t.Error("flags not reflected in snapshot")
	}
	// JID order in the snapshot is not guaranteed; sort for comparison.
	got := append([]string(nil), snap.IgnoredJIDs...)
	sort.Strings(got)
	want := []string{"a@g.us", "b@g.us"}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("IgnoredJIDs: got %v, want %v", got, want)
	}
}
