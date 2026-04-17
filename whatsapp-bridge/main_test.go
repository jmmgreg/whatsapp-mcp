package main

import "testing"

func TestResolvePort(t *testing.T) {
	tests := []struct {
		name        string
		portFlag    int
		portEnv     string
		defaultPort int
		want        int
	}{
		{"flag wins over env and default", 9000, "9001", 8080, 9000},
		{"env used when flag is zero", 0, "9001", 8080, 9001},
		{"default when flag zero and env empty", 0, "", 8080, 8080},
		{"default when env is not a number", 0, "abc", 8080, 8080},
		{"default when env is zero", 0, "0", 8080, 8080},
		{"default when env is negative", 0, "-1", 8080, 8080},
		{"custom default respected", 0, "", 9999, 9999},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := resolvePort(tc.portFlag, tc.portEnv, tc.defaultPort)
			if got != tc.want {
				t.Errorf("resolvePort(%d, %q, %d) = %d; want %d",
					tc.portFlag, tc.portEnv, tc.defaultPort, got, tc.want)
			}
		})
	}
}

func TestCanonicalizeSender(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
	}{
		{"empty string unchanged", "", ""},
		{"bare phone number gets s.whatsapp.net", "33609553753", "33609553753@s.whatsapp.net"},
		{"already full s.whatsapp.net JID unchanged", "33609553753@s.whatsapp.net", "33609553753@s.whatsapp.net"},
		{"lid JID left alone (caller resolves)", "12345@lid", "12345@lid"},
		{"group JID unchanged", "120363422787427641@g.us", "120363422787427641@g.us"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := canonicalizeSender(tc.input); got != tc.want {
				t.Errorf("canonicalizeSender(%q) = %q; want %q", tc.input, got, tc.want)
			}
		})
	}
}
