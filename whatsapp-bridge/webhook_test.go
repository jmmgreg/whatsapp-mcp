package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func TestNewWebhookConfig(t *testing.T) {
	tests := []struct {
		name                                string
		urlArg, urlEnv, timeoutEnv, hdrsEnv string
		wantNil                             bool
		wantURL                             string
		wantTimeout                         time.Duration
		wantHeaders                         map[string]string
		wantErr                             bool
	}{
		{
			name:    "no config returns nil (webhooks disabled)",
			wantNil: true,
		},
		{
			name:        "env URL only, default timeout",
			urlEnv:      "https://example.test/hook",
			wantURL:     "https://example.test/hook",
			wantTimeout: 3 * time.Second,
		},
		{
			name:        "flag URL wins over env URL",
			urlArg:      "https://from-flag.test/hook",
			urlEnv:      "https://from-env.test/hook",
			wantURL:     "https://from-flag.test/hook",
			wantTimeout: 3 * time.Second,
		},
		{
			name:        "custom timeout",
			urlEnv:      "https://example.test/hook",
			timeoutEnv:  "10",
			wantURL:     "https://example.test/hook",
			wantTimeout: 10 * time.Second,
		},
		{
			name:       "invalid timeout (non-numeric)",
			urlEnv:     "https://example.test/hook",
			timeoutEnv: "abc",
			wantErr:    true,
		},
		{
			name:       "invalid timeout (zero)",
			urlEnv:     "https://example.test/hook",
			timeoutEnv: "0",
			wantErr:    true,
		},
		{
			name:        "headers parsed from JSON",
			urlEnv:      "https://example.test/hook",
			hdrsEnv:     `{"Authorization": "Bearer xyz", "X-Source": "wa"}`,
			wantURL:     "https://example.test/hook",
			wantTimeout: 3 * time.Second,
			wantHeaders: map[string]string{"Authorization": "Bearer xyz", "X-Source": "wa"},
		},
		{
			name:    "invalid headers JSON",
			urlEnv:  "https://example.test/hook",
			hdrsEnv: "not-json",
			wantErr: true,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cfg, err := newWebhookConfig(tc.urlArg, tc.urlEnv, tc.timeoutEnv, tc.hdrsEnv)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got nil (cfg=%+v)", cfg)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if tc.wantNil {
				if cfg != nil {
					t.Fatalf("expected nil cfg, got %+v", cfg)
				}
				return
			}
			if cfg.URL != tc.wantURL {
				t.Errorf("URL: got %q, want %q", cfg.URL, tc.wantURL)
			}
			if cfg.Timeout != tc.wantTimeout {
				t.Errorf("Timeout: got %v, want %v", cfg.Timeout, tc.wantTimeout)
			}
			if tc.wantHeaders != nil {
				if len(cfg.Headers) != len(tc.wantHeaders) {
					t.Errorf("Headers: got %v, want %v", cfg.Headers, tc.wantHeaders)
				}
				for k, v := range tc.wantHeaders {
					if cfg.Headers[k] != v {
						t.Errorf("Headers[%q]: got %q, want %q", k, cfg.Headers[k], v)
					}
				}
			}
		})
	}
}

func TestFireNilIsNoOp(t *testing.T) {
	// Must not panic.
	var cfg *WebhookConfig
	cfg.Fire(WebhookPayload{ChatJID: "x"})
}

func TestFireSyncDeliversPayload(t *testing.T) {
	var (
		mu          sync.Mutex
		gotBody     []byte
		gotCT       string
		gotAuth     string
		gotMethod   string
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		gotMethod = r.Method
		gotCT = r.Header.Get("Content-Type")
		gotAuth = r.Header.Get("Authorization")
		gotBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	cfg, err := newWebhookConfig("", srv.URL, "", `{"Authorization": "Bearer test-token"}`)
	if err != nil {
		t.Fatalf("newWebhookConfig: %v", err)
	}
	if cfg == nil {
		t.Fatal("expected non-nil cfg")
	}

	ts := time.Date(2026, 4, 17, 12, 34, 56, 0, time.UTC)
	payload := WebhookPayload{
		ChatJID:   "5521966683939@s.whatsapp.net",
		Sender:    "5521966683939@s.whatsapp.net",
		Text:      "hello",
		Timestamp: ts,
		Direction: "incoming",
		IsGroup:   false,
		MediaType: "",
	}
	cfg.fireSync(payload)

	mu.Lock()
	defer mu.Unlock()
	if gotMethod != http.MethodPost {
		t.Errorf("method: got %q, want POST", gotMethod)
	}
	if gotCT != "application/json" {
		t.Errorf("Content-Type: got %q, want application/json", gotCT)
	}
	if gotAuth != "Bearer test-token" {
		t.Errorf("Authorization: got %q, want %q", gotAuth, "Bearer test-token")
	}
	var decoded WebhookPayload
	if err := json.Unmarshal(gotBody, &decoded); err != nil {
		t.Fatalf("decode body: %v (body=%s)", err, gotBody)
	}
	if decoded.ChatJID != payload.ChatJID ||
		decoded.Sender != payload.Sender ||
		decoded.Text != payload.Text ||
		decoded.Direction != payload.Direction ||
		!decoded.Timestamp.Equal(payload.Timestamp) {
		t.Errorf("payload round-trip mismatch: got %+v, want %+v", decoded, payload)
	}
	// Non-media payload must omit media_type in JSON
	var raw map[string]any
	_ = json.Unmarshal(gotBody, &raw)
	if _, present := raw["media_type"]; present {
		t.Errorf("media_type should be omitted when empty; body=%s", gotBody)
	}
}

func TestFireSyncTimeout(t *testing.T) {
	// Server that never responds — fireSync should return within the timeout,
	// not block forever.
	block := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-block
	}))
	defer func() {
		close(block)
		srv.Close()
	}()

	cfg, err := newWebhookConfig("", srv.URL, "1", "")
	if err != nil {
		t.Fatalf("newWebhookConfig: %v", err)
	}
	start := time.Now()
	cfg.fireSync(WebhookPayload{ChatJID: "x"})
	elapsed := time.Since(start)
	if elapsed > 3*time.Second {
		t.Errorf("fireSync exceeded expected timeout: elapsed=%v", elapsed)
	}
}
