# Public-distribution hardening review

This is an unreleased hardening change on the v0.2 development branch. It does
not establish release readiness, marketplace acceptance or compatibility with
untested Omarchy versions. Historical supervised desktop acceptance is described
in [VALIDATION.md](VALIDATION.md) and [V02.md](V02.md); it was not repeated here.

## Changes

- Supervised deployment now requires matching explicit loaded-unit environment
  values before probing/stopping/installing, rechecks before each start, and
  checks the running MainPID's environment before health IPC. Environment files,
  pass/unset directives, PAM injection and ambiguous escaped output fail closed.
  No automatic override is installed. See the exact supported contract and trust
  limits in [V02.md](V02.md#installation-boundaries). The private rehearsal uses
  the same explicit unit contract and retains its execve isolation guard.
- An asynchronous state-write failure queues one best-effort write containing
  the newly imposed suspension even without a dirty successor. Failure of that
  follow-up does not create another retry. Independently requested saves can
  still coalesce; successful persistence does not clear suspension. Tests cover
  the no-dirty failure, successful durable follow-up/restart and repeated failure
  stopping after exactly two attempts. Persistent I/O failure or a crash before
  durable completion can leave the old state on disk, so a restart cannot recover
  a suspension that was never saved.
- Command-socket status replies refresh the countdown only while the subscribed
  snapshot epoch, instance and sequence remain unchanged since dispatch. A newer
  snapshot or restarted daemon wins over a late status reply.
- Missing runtime environment no longer produces `/control.sock`. Invalid
  absolute-path overrides, root fallbacks, control characters, dot segments and
  oversized Unix socket paths produce actionable errors without opening a socket
  or scheduling retries. An invalid explicit runtime override does not select
  the XDG fallback.
- QML declarations use block scope where needed and the panel's content ID no
  longer shadows an inherited property. The validator still rejects warnings
  and nonzero lint exits; no warning suppression was added.
- Preview tests separate literal rendering from filesystem/socket constraints
  and prioritize production-runtime refusal diagnostics before length checks.
  Production socket length validation remains enabled. The long-TMPDIR tests do
  not relocate their fixtures to `/tmp`. The picker Close timing check begins
  after the fake portal's Open request, excludes interpreter startup, and retains
  a 1.5-second completion bound around the 500ms Close guard plus 200ms timeout.

## Follow-up environment-evidence hardening, 2026-11-06

Service-property query stdout/stderr are now omitted from coordinator events and
the rehearsal runner's separate trace, including nonzero exits and partial output
on timeout/error. Full responses remain available in memory for the existing
environment validation. Validation errors use fixed messages and discard raw
exception chains and untrusted variable names. Existing evidence directories and
logs are tightened to `0700`/`0600` without removing prior entries; symlinked
directory components, symlinked/multiply linked logs and nonregular logs are refused.

Canary regressions exercise the real service/coordinator runner, the nested
rehearsal runner with mocked subprocesses, failed/malformed queries, timeout/error
payloads, the upgrade-failure event, permissive preexisting paths and umask, and
unsafe log paths. An additional persistence test covers a dirty save intervening
before the first failure, successful suspension persistence, later coalesced
saves, and durable restart state. No production daemon behavior changed in this
follow-up; its Rust change adds coverage.

| Follow-up check | Observed result |
| --- | --- |
| `python3 -B -m unittest test_deployment test_service_control.EnvironmentContractTests test_service_control.ServiceControlTests -q` from `scripts/` | 118 passed in 74.067s; deployment/private filesystem and fake-manager service tests |
| `cargo test --locked --bin swiftwall daemon::tests::persistence -- --test-threads=2` | 4 passed, including dirty-save recovery and later coalescing |
| `cargo test --locked --bin swiftwall without_dirty -- --test-threads=2` | 2 passed; durable no-dirty recovery and bounded repeated failure |
| `cargo fmt --check` | Passed |
| `cargo clippy --locked --all-targets -- -D warnings` | Passed |
| `git diff --check` | Passed |

No live service/desktop operation, real-manager rehearsal, commit or push was
performed. The parent's outside-sandbox results below remain the full socket-suite
verification record; sandbox restrictions are not application failures.

## Verification outside the coding sandbox

The parent reviewer reran checks without the sandbox restriction. These results
supersede the sandbox failures below; the older record is retained to explain why
those runs could not exercise Unix sockets.

| Check | Observed result |
| --- | --- |
| `cargo +1.89.0 check --locked --all-targets` | Passed; documented minimum Rust version verified against locked dependencies |
| `cargo fmt --check` | Passed |
| `cargo clippy --locked --all-targets -- -D warnings` | Passed |
| `cargo build --release --locked` | Passed |
| `cargo test --locked` | Passed: 77 unit, 4 config CLI, 14 daemon and 11 v0.2 integration tests |
| `python3 -m unittest discover -s scripts -p 'test_*.py' -q` | 354 tests passed in 152.989s after evidence-log and coalescing regression additions |
| `python3 scripts/validate_plugin.py` | Passed; 0 warnings/errors, 41 informational diagnostics |
| `python3 scripts/test_widget.py --binary target/release/swiftwall` | Passed after deferred socket teardown: SettingsDraft 14 passed; runtime steps 0–33 reached `SWIFTWALL_RUNTIME_PASS` through daemon stop/restart and malformed/legacy peers. |

An initial Rust rerun failed once with `Text file busy` while executing a freshly
created fake setter; the complete rerun passed. This is recorded as a test-fixture
flake, not silently omitted. The initial widget crash was isolated to Quickshell
0.3.2 reentrancy: disconnecting in its error callback clears an internal pointer
before upstream error cleanup finishes. SwiftWall now detaches/marks sockets
retired immediately and defers disconnect/destruction with `Qt.callLater`.
Regression tests cover both callback stacks, stale events and already-destroyed
objects. The corrected harness passes; this does not establish native compositor
focus/parenting or real portal-dialog correctness. It still reports test-host
colour warnings and a private Quickshell IPC path limitation, neither hidden nor
counted as live acceptance. No real wallpaper, installed service or desktop
configuration was changed.

Gitleaks 8.30.1 scanned both existing commits (all refs) with no leaks found; this
is a bounded automated scan, not a guarantee of absence of private data. The plugin
ID was absent from the current marketplace registry when checked.

The owner selected MIT licensing; the root `LICENSE` now records those terms.
The CI checkout action is pinned to the verified v7 commit. GitHub CI and the
marketplace submission remain pending; no public release is claimed.

## Earlier sandbox verification, 2026-11-06

Tools: Rust/Cargo 1.99.1, Python 3.14.9, Qt lint/Test 6.12.0. All engines/setters
used by tests are private fixtures or mocks. No live acceptance, real setter,
systemd service operation, installation into the live home, desktop configuration
change, commit, push or publication was performed.

| Check | Observed result |
| --- | --- |
| `cargo fmt --check` | Exit 0 |
| `cargo clippy --all-targets -- -D warnings` | Exit 0 |
| `cargo build --release --locked` | Exit 0 |
| `cargo test -- --test-threads=2` | Exit 101; unit target: 75 passed, 1 failed; Cargo stopped before integration targets |
| `cargo test --tests --no-fail-fast` | Exit 101; 80 passed, 25 failed across four targets; details below |
| `python3 -B -m unittest discover -s scripts -p 'test_*.py' -v` | Exit 1; 343 tests in 98.250s: 305 passed, 24 failures, 14 errors, 0 skipped; all failures/errors require denied private sockets/D-Bus |
| Focused deployment/environment/QML regressions | Exit 0; 35 passed |
| Long-TMPDIR preview suite | Exit 0; 18 passed, including both previously reported regressions; fixtures remained under the supplied scratch TMPDIR |
| `python3 -B scripts/validate_plugin.py` | Exit 0; installed manifest validator plus all 6 payload QML files; 0 warnings/errors, 41 informational missing-property diagnostics |
| New service QML logic test | Exit 0; all 5 environment cases pass using unchanged SharedService source with explicit Quickshell/Io stubs and fake sockets |
| SettingsDraft QtTest, direct offscreen runner | Exit 0; 14 passed, 0 failed/skipped (includes init/cleanup cases) |
| `python3 scripts/test_widget.py --binary target/release/swiftwall` | Exit 1; private D-Bus cannot bind a Unix socket, so runtime scenarios did not run |
| `git diff --check` | Exit 0 |

Rust target results: unit tests **75 passed / 1 failed**, config CLI **4 / 0**,
daemon integration **0 / 14**, v0.2 integration **1 / 10**. The failing runtime
unit and every failed integration daemon report `Operation not permitted (os
error 1)` at private socket/startup setup. The sandbox forbids AF_UNIX binding.
The two new persistence tests pass within the unit target. No tests were changed
to skip this restriction or to replace socket integration with an asserted pass.

Python failure breakdown: **22** picker integration failures and **1** picker QML
process failure at private D-Bus startup, **1** release-daemon integration failure
at daemon startup, and **14** preview-proxy errors at socket construction/bind.
Each is an `Operation not permitted` setup failure. All deployment mock/private
filesystem tests and the newly added logic regressions passed in discovery.
The picker suite's ordinary traceback only shows its missing `unix:` address
assertion; a separate reproduction captured the private bus's bind-denial stderr
in `hardening-picker-blocker.log`.

The direct QML logic test exercises runtime precedence, invalid path diagnostics,
no request/socket creation on refusal, dispatch-time authority capture, newer
sequence ordering, instance restart, valid countdown refresh, suspension and
disconnect clearing. Its stubs are explicit: it does not establish actual
Quickshell socket transport, host panel behaviour or visual correctness. Those
remain blocked in the separate runtime harness. The modified picker timing
assertion also remains unverified at runtime because its private D-Bus setup is
denied before the chooser test begins.

Raw local logs remain ignored under `artifacts/hardening-*.log`, including
`hardening-rust-all-targets.log`, `hardening-python-final.log`,
`hardening-targeted-regressions.log`, `hardening-preview-long-tmp.log`,
`hardening-qml-validation.log`, `hardening-service-qml-logic.log`,
`hardening-settings-qml.log`, `hardening-picker-blocker.log` and
`hardening-widget-runtime.log`. These are local
evidence, not distributable plugin payloads. Graph discovery was attempted; after
initial indexing, subsequent queries/coverage checks were rejected by automatic
approval review because approvals are disabled. Source files were inspected
directly instead; no graph completeness claim is made.

## CI and remaining distribution checklist

No existing CI workflow was found. [Offline checks](../.github/workflows/offline.yml)
adds a hosted Linux job for format, Clippy, all Rust tests, locked release build
and Python discovery. It uses read-only repository permissions, no persisted
checkout credentials, no desktop session, and no systemd activation or real setter.
It adds no application dependencies and installs no Omarchy/Quickshell packages.
The checkout action is pinned to its v7 commit. Hosted execution remains pending.
Optional Qt/Quickshell/Gio checks can skip when the runner lacks those tools;
hosted CI is not native-widget acceptance. A separate Rust 1.89.0 job checks the
documented minimum against locked dependencies; the equivalent check passed locally.

- [x] Rerun all Rust/Python/socket and picker timing tests outside the coding
  sandbox: full suites passed; retain the recorded fake-setter flake.
- [ ] Run the real-import offscreen widget harness and independently reviewed
  native widget/chooser acceptance for these changes. No fresh live acceptance
  was authorized in this hardening task.
- [ ] Review the stricter unit environment contract and perform an explicitly
  authorized isolated systemd rehearsal; mocks do not establish real-manager
  compatibility or close concurrent-edit races.
- [ ] Observe the new CI workflow on the proposed release revision and inspect
  optional skips. Validate the existing MSRV claim separately if retaining it.
- [x] Owner selected MIT; root `LICENSE` added. Review any third-party assets
  separately before adding marketplace previews.
- [ ] Review final metadata, version, distribution URLs, compatibility evidence,
  privacy boundaries and marketplace requirements before any publication. No
  new minimum Omarchy version, legal terms or installation URLs are asserted.
