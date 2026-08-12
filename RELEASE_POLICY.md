# Public release policy

Status: binding policy for `LivetARSmartA/ReplayLab`, adopted 2026-08-12.

## Channel contract

This repository is the public stable distribution channel. It does not track
private development automatically. A private version becomes public stable only
after an explicit owner decision naming that exact version and successful
completion of the promotion gate below.

At adoption of this policy:

- public stable: `0.5.1`;
- private development: `0.5.7`;
- versions `0.5.2` through `0.5.7` are intentionally not public releases.

The latest non-prerelease GitHub Release is authoritative for the public stable
version. Branch content, private version files, local ZIPs, and development
commit numbers are not promotion signals.

## Promotion gate

Every public stable release requires:

1. Explicit owner approval of the exact version.
2. A clean and identified canonical source commit plus the Atlas/provider commit
   used by the build, when applicable.
3. A bounded publication allowlist and a reviewed diff against the previous
   stable release.
4. Applicable unit, integration, and native tests.
5. Packaged `ReplayLab.exe --self-test` and a clean-directory installation smoke
   test on supported Windows.
6. Secret, private-path, forbidden-artifact, and private-public-boundary scans.
7. Recorded provenance and redistribution permission for every bundled
   third-party asset, or omission of that asset from the public package.
8. A manifest with version, channel, source SHA, Atlas/provider SHA, build time,
   toolchain/dependency versions, and hashes for all payload files.
9. An immutable `vX.Y.Z` tag, GitHub Release, portable ZIP, SHA-256 checksum,
   release notes, known limitations, and rollback instructions.
10. Confirmation that uploaded assets are byte-for-byte the tested artifacts,
    not a post-acceptance rebuild.

If any gate is unknown or fails, the version remains private development. A
partial upload must be removed or marked as a draft before users are directed to
it; the previous stable release remains authoritative.

## Prohibited publication content

Public commits and artifacts must not contain:

- private source history or unrestricted source-tree copies;
- Atlas evidence, provider internals, reverse-engineering notes, dumps, replay
  corpora, maps, game binaries, or decompiler output;
- credentials, cookies, personal diagnostics, private logs, or local absolute
  paths;
- third-party assets without documented redistribution permission;
- unverified helper binaries or manifests that cannot be tied to the exact ZIP.

## Maintenance changes

Security instructions, checksum corrections, and documentation fixes may be
reviewed independently, but they must not advance the stable version or claim
private functionality. Routine work uses a topic branch and pull request. Stable
tags and published assets are immutable unless the owner authorizes a documented
security incident response.
