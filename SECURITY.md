# Security policy

## Supported versions

Security fixes target latest release and current default branch.

## Reporting a vulnerability

Do not open a public issue for an undisclosed vulnerability. Use repository
**Security → Report a vulnerability** flow to create a private GitHub Security
Advisory. Include affected client distribution, version or commit,
reproduction steps, impact, and suggested mitigation.

Maintainers will acknowledge a complete report as soon as practical, validate
scope, coordinate a fix and disclosure timeline, and credit reporters who want
attribution. Never include secrets, private source, customer data, or harmful
public proof-of-concept material.

NightFalcon generates security analysis and proof-of-concept artifacts. Run it
only against targets you own or have explicit authorization to assess. Review
generated artifacts before sharing them.

## Package and release compromise

Report suspected package takeover, malicious release, signing-key exposure,
checksum mismatch, provenance failure, dependency substitution, or compromised
publisher identity through same private advisory channel. Include registry,
package version, observed checksum, install source, and available provenance.

Do not continue using suspected artifact. Preserve package, lockfile, command
output, and download metadata without executing it again. Maintainers will
compare artifact against tag-bound source, release manifest, SHA-256 checksum,
SBOM, and CI provenance; revoke affected release or publisher credentials;
then publish remediation and rotation guidance.
