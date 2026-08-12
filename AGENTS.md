# Public ReplayLab agent rules

These instructions are binding for the entire repository. Read
`RELEASE_POLICY.md` before changing code, version claims, tags, or release
assets.

## Repository identity

- This is the public stable distribution channel, not the canonical development
  source and not a research repository.
- At adoption of this policy, public stable is `0.5.1`. Private development may
  have higher versions; they are not public releases.
- Never advance the public version merely because a newer private `VERSION`,
  commit, build, or ZIP exists.
- Never import private Git history, Atlas evidence/provider internals,
  reverse-engineering material, replay corpora, dumps, private diagnostics, or
  local paths.

## Publication authority

- Creating or replacing a tag, GitHub Release, executable, ZIP, manifest, or
  public source snapshot requires explicit owner approval naming the exact
  version to promote.
- Routine development, documentation work, or permission to push a branch is
  not release approval.
- No new public package may be published while licensing and bundled
  third-party asset redistribution rights remain unresolved.
- Do not delete or rewrite existing public history as an improvised licensing,
  privacy, or cleanup fix. Such a migration requires explicit approval, backup,
  and a written plan.

## Git and release invariants

- Inspect status, upstream divergence, current releases, and tags before making
  changes.
- Use an `agent/*` branch and pull request for routine work. Stage explicit files
  and preserve unrelated changes.
- Do not force-push `main`, move stable tags, replace published assets, or delete
  releases without explicit owner approval.
- A promoted stable release must satisfy every gate in `RELEASE_POLICY.md` and
  must include immutable `vX.Y.Z`, GitHub Release, verified portable ZIP,
  SHA-256 checksum, release notes, and provenance manifest.
- The uploaded ZIP must be byte-for-byte identical to the artifact that passed
  packaged self-test and clean-install smoke testing.
- Documentation and security maintenance must not claim unreleased private
  functionality or alter the declared stable version.
