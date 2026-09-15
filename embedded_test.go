package nightfalconpayload

import "testing"

func TestCanonicalExecutableInventory(t *testing.T) {
	if !IsExecutable("cursor/.cursor/hooks/gate-guard.sh") {
		t.Fatal("Cursor gate hook must remain executable")
	}
	if IsExecutable("cursor/.cursor/hooks.json") {
		t.Fatal("Cursor hook configuration must remain non-executable")
	}
}
