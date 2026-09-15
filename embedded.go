package nightfalconpayload

import "embed"
import "strings"

// Files contains canonical src/nightfalcon plus Claude, Codex, and Cursor payloads.
//go:embed all:src/nightfalcon all:claude all:codex all:cursor
var Files embed.FS

//go:embed packaging/runtime-executables.txt
var executableInventory string

var executablePaths = func() map[string]struct{} {
	paths := make(map[string]struct{})
	for _, path := range strings.Fields(executableInventory) {
		paths[path] = struct{}{}
	}
	return paths
}()

// IsExecutable reports whether path has canonical executable payload mode.
func IsExecutable(path string) bool {
	_, ok := executablePaths[path]
	return ok
}
