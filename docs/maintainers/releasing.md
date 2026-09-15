# Releasing NightFalcon

This runbook creates reviewable artifacts. It does not authorize registry
publication or ownership changes.

## Release gate

1. Use clean default-branch checkout with no untracked build output.
2. Update `VERSION` and every registry manifest to identical semantic version.
3. Regenerate migration and source-integrity manifests.
4. Run root, Claude/Codex/Cursor, distribution, and native-package test suites locally.
5. Build release twice from clean checkouts and compare checksums.
6. Review SBOM, licenses, release manifest, and payload inventory.
7. Create signed `v<version>` tag from reviewed commit.

On Debian/Ubuntu build host, canonical clean-checkout command is:

```bash
python scripts/release/build-debian-package.py --output-dir dist
```

Builder stages tracked repository files into temporary Debian source tree,
copies `packaging/debian` to required top-level `debian/`, runs unsigned binary
package build, and returns `.deb`, `.buildinfo`, and `.changes` files to `dist`.

GitHub Actions automation is currently removed. Build artifacts locally with
the scripts in `scripts/release/`, then verify SHA-256 and
`release-manifest.json`. Execute `nightfalcon version` and `nightfalcon doctor`
from the universal executable and Python wheel in a clean environment.

Release and registry publication require a separately authorized manual
process. Pushing a tag does not build or publish artifacts.

## Registry ownership

Use organization-controlled publisher accounts with phishing-resistant MFA,
least-privilege tokens, multiple maintainers, and recovery contacts. Use an approved manual publishing process for PyPI. Apply an equivalent
protected release process to
npm, crates.io, RubyGems, NuGet, Maven Central, Packagist, Homebrew tap, and APT
repository. Availability checks alone provide no ownership.

Never reuse personal access token across registries. Never place credentials
in repository, workflow variables readable by pull requests, package archives,
or command history. Record package digest and registry response for every
publication.

## Post-publication verification

For each channel, install exact version in clean container or VM, run
`nightfalcon version`, `nightfalcon doctor`, install all detected clients, and
compare installed payload hashes with release manifest. Confirm public package
metadata points only to `https://github.com/intuit/nightfalcon`.

If digest, provenance, metadata, or behavior differs, stop remaining publishes,
follow package-compromise process in [`SECURITY.md`](../../SECURITY.md), and do
not overwrite or delete evidence.
