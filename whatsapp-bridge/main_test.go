package main

import "testing"

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
