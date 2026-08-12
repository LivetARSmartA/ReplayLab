# ReplayLab 0.6.0 public promotion candidate

Status: source candidate only. Public stable remains 0.5.1 until the complete
promotion gate passes and `v0.6.0` is deliberately released.

## Identified input

- Canonical private source commit: `25a6ca1223ba7bf733c3a4a8bdd9846a593e471a`.
- Atlas/provider input: omitted. No Atlas binary, evidence or implementation is
  part of this public candidate.

## Bounded source allowlist

- top-level launcher files: `run_gui.py`, `START_REPLAY_LAB.cmd` and
  `requirements.txt`;
- presentation/orchestration modules: `w3g_parser/*.py`;
- versioned public runtime profiles: `w3g_parser/profiles/*.json`;
- seven first-party native adapters: `native/replaylab_camera_host.exe`,
  `native/replaylab_runtime_host.exe`, `native/replaylab_signature_probe.exe`,
  `native/replaylab_telemetry_host.exe`,
  `native/replaylab_replay_transport_host.exe`,
  `native/replaylab_replay_inspect.exe` and
  `native/replaylab_deep_analysis_core.exe`.

The export does not include private history, replay files, maps, logs, dumps,
local paths, build caches or the Atlas Deep Analysis capture provider.

## Completed candidate checks

- all seven native deliverables passed their unit/self-tests and Windows import
  audit in the canonical source checkout;
- 15 architecture, protocol, corpus and presentation tests passed;
- source and packaged GUI self-tests passed with a real replay;
- a cleanly extracted private candidate repeated GUI/replay tests with exit
  code `0`;
- this bounded public source candidate passes compileall and the offscreen GUI
  replay self-test.

## Gates intentionally deferred

- confirm or replace every bundled third-party visual asset before building a
  public package;
- build the public ZIP from a clean checkout without the Atlas provider;
- generate a release manifest containing channel, canonical source SHA,
  toolchain/dependency versions and hashes for every payload file;
- repeat packaged and clean-extraction tests on that exact ZIP;
- review the final diff and release notes, then merge, tag and publish the same
  tested bytes.

## Known limitation for release notes

iCCup Launcher can perform legitimate preparation before Warcraft starts, such
as checking or downloading a map. ReplayLab 0.6.0 may currently mistake that
busy phase for a failed launch. The follow-up must preserve one launch session
across preparing/updating states and cancel only on an explicit failure,
diagnostic timeout or user request.

## Rollback

Until promotion completes, `v0.5.1` remains the authoritative public stable
release. If a 0.6.0 promotion gate fails tomorrow, do not create or move the
tag and do not upload a partial package.
