package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sync"
)

// FilterConfig holds the "which chats to silently drop" settings, loaded from
// an optional JSON file. A nil *FilterConfig means no filtering — every
// method on a nil receiver is a safe no-op / permissive default, so callers
// can hold a single field that's either nil or configured without branching
// at every call site.
type FilterConfig struct {
	mu             sync.RWMutex
	path           string
	ignoreArchived bool
	ignoreMuted    bool
	ignoredJIDs    map[string]struct{}
}

// filterFileSchema mirrors the on-disk JSON format.
type filterFileSchema struct {
	IgnoreArchived bool     `json:"ignoreArchived"`
	IgnoreMuted    bool     `json:"ignoreMuted"`
	IgnoredJIDs    []string `json:"ignoredJids"`
}

// newFilterConfig loads the file at path. Returns nil (no filter) when path
// is empty. If the file is missing or malformed, returns an error — callers
// treat "configured but broken" differently from "not configured at all".
func newFilterConfig(path string) (*FilterConfig, error) {
	if path == "" {
		return nil, nil
	}
	f := &FilterConfig{path: path}
	if err := f.Reload(); err != nil {
		return nil, err
	}
	return f, nil
}

// Reload re-reads the configured file and atomically replaces the in-memory
// state. Safe to call from multiple goroutines.
func (f *FilterConfig) Reload() error {
	if f == nil {
		return fmt.Errorf("nil FilterConfig")
	}
	data, err := os.ReadFile(f.path)
	if err != nil {
		return fmt.Errorf("read filter config %s: %w", f.path, err)
	}
	var schema filterFileSchema
	if err := json.Unmarshal(data, &schema); err != nil {
		return fmt.Errorf("parse filter config %s: %w", f.path, err)
	}

	ignored := make(map[string]struct{}, len(schema.IgnoredJIDs))
	for _, jid := range schema.IgnoredJIDs {
		if jid != "" {
			ignored[jid] = struct{}{}
		}
	}

	f.mu.Lock()
	f.ignoreArchived = schema.IgnoreArchived
	f.ignoreMuted = schema.IgnoreMuted
	f.ignoredJIDs = ignored
	f.mu.Unlock()
	return nil
}

// Matches returns true if the given chat JID is in the explicit ignore list.
// Does NOT consult archive/mute state — use MatchesChatMeta for that.
// Safe on a nil receiver (returns false).
func (f *FilterConfig) Matches(jid string) bool {
	if f == nil {
		return false
	}
	f.mu.RLock()
	defer f.mu.RUnlock()
	_, ok := f.ignoredJIDs[jid]
	return ok
}

// MatchesChatMeta returns true if a chat with the given archive/mute state
// should be dropped. Combines the ignoredJids list with ignoreArchived /
// ignoreMuted flags. Safe on a nil receiver.
func (f *FilterConfig) MatchesChatMeta(jid string, archived, muted bool) bool {
	if f == nil {
		return false
	}
	f.mu.RLock()
	defer f.mu.RUnlock()
	if _, ok := f.ignoredJIDs[jid]; ok {
		return true
	}
	if f.ignoreArchived && archived {
		return true
	}
	if f.ignoreMuted && muted {
		return true
	}
	return false
}

// shouldCheckMeta returns true if the filter cares about archive/mute state,
// signaling to callers that they need to look up chat metadata before
// storing a message. Safe on a nil receiver.
func (f *FilterConfig) shouldCheckMeta() bool {
	if f == nil {
		return false
	}
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.ignoreArchived || f.ignoreMuted
}

// Snapshot returns a point-in-time copy of the filter state — used by the
// MCP server (via an HTTP endpoint) and by consumers that want to apply the
// same rules to their own queries (e.g. SQL WHERE clauses).
type FilterSnapshot struct {
	IgnoreArchived bool     `json:"ignoreArchived"`
	IgnoreMuted    bool     `json:"ignoreMuted"`
	IgnoredJIDs    []string `json:"ignoredJids"`
}

func (f *FilterConfig) Snapshot() FilterSnapshot {
	if f == nil {
		return FilterSnapshot{}
	}
	f.mu.RLock()
	defer f.mu.RUnlock()
	jids := make([]string, 0, len(f.ignoredJIDs))
	for jid := range f.ignoredJIDs {
		jids = append(jids, jid)
	}
	return FilterSnapshot{
		IgnoreArchived: f.ignoreArchived,
		IgnoreMuted:    f.ignoreMuted,
		IgnoredJIDs:    jids,
	}
}
