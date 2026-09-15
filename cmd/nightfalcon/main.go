package main

import (
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	payload "github.com/intuit/nightfalcon"
)

func findPython() string {
	candidates := []string{os.Getenv("NIGHTFALCON_PYTHON"), "python3", "python"}
	for _, candidate := range candidates {
		if candidate == "" { continue }
		command := exec.Command(candidate, "-c", "import sys; raise SystemExit(sys.version_info < (3, 11))")
		if command.Run() == nil { return candidate }
	}
	return ""
}

func main() {
	python := findPython()
	if python == "" { fmt.Fprintln(os.Stderr, "nightfalcon: Python 3.11 or later is required"); os.Exit(1) }
	root, err := os.MkdirTemp("", "nightfalcon-")
	if err != nil { fmt.Fprintln(os.Stderr, err); os.Exit(1) }
	defer os.RemoveAll(root)
	err = fs.WalkDir(payload.Files, ".", func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil { return walkErr }
		if path == "." { return nil }
		target := filepath.Join(root, filepath.FromSlash(path))
		if entry.IsDir() { return os.MkdirAll(target, 0o700) }
		content, readErr := payload.Files.ReadFile(path)
		if readErr != nil { return readErr }
		if makeErr := os.MkdirAll(filepath.Dir(target), 0o700); makeErr != nil { return makeErr }
		mode := fs.FileMode(0o600)
		if payload.IsExecutable(path) { mode = 0o700 }
		return os.WriteFile(target, content, mode)
	})
	if err != nil { fmt.Fprintln(os.Stderr, err); os.Exit(1) }
	command := exec.Command(python, append([]string{"-m", "nightfalcon"}, os.Args[1:]...)...)
	command.Dir = root
	pythonPath := filepath.Join(root, "src")
	if existing := os.Getenv("PYTHONPATH"); existing != "" { pythonPath += string(os.PathListSeparator) + existing }
	command.Env = append(os.Environ(), "PYTHONPATH="+pythonPath)
	command.Stdin, command.Stdout, command.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := command.Run(); err != nil {
		if exit, ok := err.(*exec.ExitError); ok { os.Exit(exit.ExitCode()) }
		fmt.Fprintln(os.Stderr, strings.TrimSpace(err.Error())); os.Exit(1)
	}
}
