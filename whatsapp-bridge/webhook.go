package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"
)

// WebhookConfig holds settings for the outbound live-event webhook.
//
// A nil *WebhookConfig means webhooks are disabled — all methods on a nil
// receiver are safe no-ops. This lets callers hold a single field that's
// either nil or configured, without branching at every call site.
type WebhookConfig struct {
	URL     string
	Timeout time.Duration
	Headers map[string]string

	client *http.Client
}

// WebhookPayload is the JSON body posted to the webhook URL on every live
// (non-history-sync) message the bridge observes.
type WebhookPayload struct {
	ChatJID   string    `json:"chat_jid"`
	Sender    string    `json:"sender"`
	Text      string    `json:"text"`
	Timestamp time.Time `json:"timestamp"`
	Direction string    `json:"direction"`
	IsGroup   bool      `json:"is_group"`
	MediaType string    `json:"media_type,omitempty"`
}

// newWebhookConfig builds a WebhookConfig from an explicit URL plus env-var
// overrides, or returns nil if no URL was supplied. When the URL is empty the
// caller should treat the webhook as disabled (no-op).
//
// Inputs:
//   - urlArg:       URL from a CLI flag (takes precedence over env if set)
//   - urlEnv:       value of the WEBHOOK_URL env var
//   - timeoutEnv:   value of WEBHOOK_TIMEOUT_SECONDS (integer seconds; defaults to 3)
//   - headersEnv:   value of WEBHOOK_HEADERS (JSON object of string→string)
//
// Extracted as a pure function (no global state, no os.Getenv calls) so it
// can be unit-tested exhaustively.
func newWebhookConfig(urlArg, urlEnv, timeoutEnv, headersEnv string) (*WebhookConfig, error) {
	url := urlArg
	if url == "" {
		url = urlEnv
	}
	if url == "" {
		return nil, nil
	}

	timeout := 3 * time.Second
	if timeoutEnv != "" {
		n, err := strconv.Atoi(timeoutEnv)
		if err != nil || n <= 0 {
			return nil, fmt.Errorf("WEBHOOK_TIMEOUT_SECONDS must be a positive integer, got %q", timeoutEnv)
		}
		timeout = time.Duration(n) * time.Second
	}

	var headers map[string]string
	if headersEnv != "" {
		if err := json.Unmarshal([]byte(headersEnv), &headers); err != nil {
			return nil, fmt.Errorf("WEBHOOK_HEADERS must be a JSON object of string→string: %w", err)
		}
	}

	return &WebhookConfig{
		URL:     url,
		Timeout: timeout,
		Headers: headers,
		client:  &http.Client{Timeout: timeout},
	}, nil
}

// Fire posts the payload to the configured webhook asynchronously. Returns
// immediately; network errors are logged to stderr but never block or retry.
// No-op if cfg is nil (webhook disabled).
func (cfg *WebhookConfig) Fire(payload WebhookPayload) {
	if cfg == nil {
		return
	}
	go cfg.fireSync(payload)
}

// fireSync performs the HTTP POST synchronously. Exposed for testing so that
// tests can observe completion without polling.
func (cfg *WebhookConfig) fireSync(payload WebhookPayload) {
	body, err := json.Marshal(payload)
	if err != nil {
		fmt.Printf("webhook: failed to marshal payload: %v\n", err)
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), cfg.Timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, cfg.URL, bytes.NewReader(body))
	if err != nil {
		fmt.Printf("webhook: failed to build request: %v\n", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range cfg.Headers {
		req.Header.Set(k, v)
	}

	resp, err := cfg.client.Do(req)
	if err != nil {
		fmt.Printf("webhook: POST %s failed: %v\n", cfg.URL, err)
		return
	}
	resp.Body.Close()
	if resp.StatusCode >= 400 {
		fmt.Printf("webhook: POST %s returned %d\n", cfg.URL, resp.StatusCode)
	}
}
