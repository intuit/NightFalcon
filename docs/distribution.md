# NightFalcon distribution channels

NightFalcon uses one canonical Python CLI and embeds same Claude Code, Codex,
and Cursor payloads in every supported package. Registry packages are not yet
published. Name-availability checks do not reserve names; only registry claim
or first accepted publication does.

## User commands after publication

| Channel | Public coordinate | Install command |
|---|---|---|
| npm | `nightfalcon` | `npm install -g nightfalcon` |
| npx | `nightfalcon` | `npx nightfalcon <command>` |
| Go | `github.com/intuit/nightfalcon/cmd/nightfalcon` | `go install github.com/intuit/nightfalcon/cmd/nightfalcon@latest` |
| Homebrew | `intuit/nightfalcon/nightfalcon` | `brew tap intuit/nightfalcon https://github.com/intuit/NightFalcon` then `brew install intuit/nightfalcon/nightfalcon` |
| Debian/Ubuntu APT | release asset `nightfalcon_<version>-1_all.deb` | `sudo apt install ./nightfalcon_<version>-1_all.deb` |

This repository is its own Homebrew tap: `Formula/nightfalcon.rb` installs the
checksum-bound `.pyz` attached to the matching GitHub release. The Debian
package is published as a GitHub release asset rather than through a signed
APT repository, so download it and verify its SHA-256 before installation.
Debian packages require Debian 12 or Ubuntu 24.04 or later for Python 3.11.

All launchers require Python 3.11 or later. npm launcher requires Node.js 18 or
later; Go installation requires Go 1.22 or later. Packages do not download
executable code at runtime.

## Repository artifacts

- `pyproject.toml` defines canonical Python CLI used by universal executable.
- `package.json` builds npm package, also used by `npx`.
- `go.mod` and `cmd/nightfalcon` support Go installation.
- `packaging/homebrew` renders checksum-bound `Formula/nightfalcon.rb`; re-render
  and commit it whenever the release `.pyz` changes.
- `packaging/homebrew/core/nightfalcon.rb.template` is source-build formula
  reserved for a future `Homebrew/homebrew-core` submission. Core requires a
  notable repository (75 stars, or 225 when self-submitted) before acceptance.
- `packaging/debian` builds Debian package around offline universal executable.

GitHub Actions automation is currently removed. Build and verify artifacts
locally using the scripts in `scripts/release/`. Pushing commits or tags does
not run tests, build packages, or publish releases automatically.

## Installation integrity

Prefer signed release tags and compare downloaded artifact against published
SHA-256 checksum. Retain SBOM and release manifest with deployment evidence.
Run `nightfalcon doctor` after installation. Installer records strict receipts,
rejects symlink escapes, verifies embedded payload hashes, and only removes
files owned by matching receipt during uninstall.

Maintainer release procedure lives in
[`docs/maintainers/releasing.md`](maintainers/releasing.md).
