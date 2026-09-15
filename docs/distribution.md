# NightFalcon distribution channels

NightFalcon uses one canonical Python CLI and embeds same Claude Code, Codex,
and Cursor payloads in every supported package. Registry packages are not yet
published. Name-availability checks do not reserve names; only registry claim
or first accepted publication does.

## User commands after publication

| Channel | Public coordinate | Install command |
|---|---|---|
| PyPI | `nightfalcon` | `pip install nightfalcon` |
| Homebrew Core | `nightfalcon` | `brew install nightfalcon` |
| Homebrew tap | `intuit/tap/nightfalcon` | `brew install intuit/tap/nightfalcon` |
| npm | `nightfalcon` | `npm install -g nightfalcon` |
| crates.io | `nightfalcon` | `cargo install nightfalcon` |
| Go | `github.com/intuit/nightfalcon/cmd/nightfalcon` | `go install github.com/intuit/nightfalcon/cmd/nightfalcon@latest` |
| RubyGems | `nightfalcon` | `gem install nightfalcon` |
| NuGet | `nightfalcon` | `dotnet tool install --global nightfalcon` |
| Packagist | `intuit/nightfalcon` | `composer global require intuit/nightfalcon` |
| Maven Central | `com.intuit:nightfalcon` | Use Maven or Gradle dependency coordinate |
| Debian/Ubuntu APT | package `nightfalcon` | `apt install nightfalcon` after repository setup |

Plain `brew install nightfalcon` requires formula acceptance into Homebrew
Core. Until then, Intuit tap command is supported route. Plain `apt install`
requires an APT repository signed and configured by distributor. GitHub and
GitLab project names distribute source but do not reserve registry names.

All launchers require Python 3.11 or later. Java package requires Java 17 or
later; NuGet global tool requires .NET 8; npm launcher requires Node.js 18 or
later. Packages do not download executable code at runtime.

## Repository artifacts

- `pyproject.toml` builds PyPI wheel and source archive.
- `package.json` builds npm package.
- `Cargo.toml` builds crates.io package.
- `go.mod` and `cmd/nightfalcon` support Go installation.
- `nightfalcon.gemspec` builds RubyGem.
- `composer.json` defines Packagist package.
- `pom.xml` builds Maven artifact.
- `packaging/nuget/NightFalcon.csproj` builds .NET global tool.
- `packaging/homebrew` renders checksum-bound Homebrew formula.
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
