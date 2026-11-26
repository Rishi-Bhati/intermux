package main

import "testing"

func TestCLISubcommands(t *testing.T) {
	if code := run([]string{"version"}); code != 0 {
		t.Fatalf("version returned %d", code)
	}
	if code := run([]string{}); code != 2 {
		t.Fatalf("empty args returned %d, want 2", code)
	}
	if code := run([]string{"unknown-cmd"}); code != 2 {
		t.Fatalf("unknown cmd returned %d, want 2", code)
	}
}
