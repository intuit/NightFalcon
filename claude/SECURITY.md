# Security Policy

## Reporting a vulnerability

If you discover a security issue **in NightFalcon itself** (the plugin, its
scripts, or its prompts), please report it privately rather than opening a
public issue:

- Use GitHub's **private vulnerability reporting** ("Report a vulnerability"
  under the repository's *Security* tab), **or**
- Open a minimal public issue that says only "security report — please
  provide a private contact" without exploit details.

> Replace this section with your project's real private security contact
> before publishing.

Please do not include exploit details, credentials, or affected third-party
hostnames in any public channel.

## Scope

This policy covers vulnerabilities in the NightFalcon plugin code. It does
**not** cover vulnerabilities that NightFalcon *finds* in a repository you
review — those belong to that repository's owner and their disclosure
process.

## Using NightFalcon responsibly

NightFalcon is a defensive security-analysis tool. Only run it against
repositories you are authorized to review. Proof-of-concept artifacts it
generates are for authorized testing against your own or explicitly
in-scope, non-production targets. Do not use generated PoCs against systems
you do not own or lack written permission to test.
