# SwiftWall release benchmark

`benchmark.py` is a Linux-only, Python 3 standard-library benchmark. It does not
build SwiftWall, install anything, access the network, or contact your running
daemon. Run it from the repository root **after** building the release binary:

```sh
cargo build --release
python3 scripts/benchmark.py
python3 scripts/benchmark.py --binary target/release/swiftwall --seconds 30 --images 1000
python3 scripts/benchmark.py --seconds 10 --images 1000 --subscribe
```

Options:

- `--binary`: executable to measure, default `target/release/swiftwall`. Relative
  paths resolve from the calling directory. Use a trusted release binary.
- `--seconds`: idle sampling duration, default `10`, inclusive range `1..120`
  (fractional seconds allowed).
- `--images`: positive number of generated images, default `100`.
- `--subscribe`: keep a native v0.2 event subscription open during the idle sample;
  requires a v0.2 test binary. No heartbeat or status polling is sent during sampling.

A missing or non-executable binary is rejected before creating the fixture or
launching anything. The script never builds or automatically runs benchmarks as
part of import, compilation, or test discovery. Its filename does not match
`test*.py`; portable Python test discovery does not execute benchmarks. The QML
runner is also explicit and does not launch a Qt session merely when imported.

## Isolation and cleanup

Each invocation creates a private fixture under `/tmp/swbench-*`, ignoring any
inherited `TMPDIR`. The child environment is constructed from scratch: `HOME`,
all supplied XDG paths, all SwiftWall configuration/state/runtime/background/
command overrides, and `TMPDIR` point into this fixture. Desktop session and
inherited SwiftWall overrides are not passed through. All child working
directories are temporary as well.

The fixture contains valid, generated 1×1 RGB PNGs, a background symlink, and a
configuration with `interval_seconds = 300`, `selection = "shuffle"`, and a
folder containing only those images. An executable fake Omarchy command validates
its arguments and image location and can only replace that fixture's background
symlink. No real Omarchy or wallpaper command is called. This is path/environment
isolation for the trusted SwiftWall binary, not an OS sandbox for arbitrary code.

The daemon receives SIGTERM in a `finally` block, including on measurement errors
or Ctrl-C. Shutdown waits up to 15 seconds, then kills and reaps the daemon if
necessary. Forced or unsuccessful shutdown fails the benchmark. Generated image
SHA-256 hashes and directory membership are compared before/after the daemon
run; changes fail the benchmark. Temporary files are removed on exit. As with
normal Python cleanup, SIGKILL or machine failure cannot guarantee cleanup.

## Measurements and JSON output

Successful runs print one JSON object to stdout; errors and daemon diagnostics go
to stderr with a nonzero exit status.

- **Binary disk size:** logical `binary_size_bytes` and allocated disk bytes
  (`st_blocks * 512`).
- **CLI latency:** median wall-clock latency of 20 separate `--version` launches,
  performed before daemon startup. Includes Python process-launch/wait overhead;
  it is not daemon startup or IPC latency. These runs also warm the binary cache.
- **Startup:** one fresh-fixture daemon sample, timed from immediately before
  `Popen`. Reports first successful direct Unix-socket `status\n` response and
  the time until status reports the full collection, no scan or pending
  persistence, and a running schedule, **and** `state.json` contains precisely
  all generated collection paths. Startup polls every 5 ms (individual socket
  operations have a 250 ms timeout), so these are observed readiness times, not
  exact internal event timestamps. Readiness has a separate 30-second timeout.
- **Idle:** after readiness, settle for one second, take `/proc/PID/stat` and
  `/proc/PID/status` snapshots, sleep for `--seconds`, then sample again. No IPC,
  CLI launches, image writes, or periodic polling occur during this sample.
  The 300-second rotation interval exceeds startup plus settling and the maximum
  sample, so no automatic wallpaper deadline is intentionally exercised.
- **RSS:** `VmRSS` in KiB at the beginning/end of the idle window, not peak RSS.
- **CPU:** process `utime`/`stime` tick deltas, `SC_CLK_TCK`, CPU seconds, and
  percent relative to one core (`100 * cpu_seconds / actual_elapsed_seconds`).
  Process CPU includes its threads but excludes child processes. Tick granularity
  means a zero delta does not prove zero work; short samples are noisy.
- **Context switches:** voluntary/involuntary deltas from `/proc/PID/status`,
  or JSON `null` if unavailable. Linux exposes these for the main thread here,
  not a sum over all worker threads.
- **Integrity/shutdown:** unchanged-image assertion and daemon exit information.

Elapsed idle time includes snapshot overhead and scheduler delays; the requested
sleep is not an exact sampling deadline. Results depend on kernel, CPU load,
filesystem, cache state, and build options. Repeat invocations for comparisons;
startup intentionally has only one sample per invocation. Synthetic tiny PNGs
measure collection bookkeeping and idle behavior, not image decode/rendering or
real desktop wallpaper application.

Syntax validation without execution or bytecode/cache files:

```sh
python3 -c 'from pathlib import Path; p = Path("scripts/benchmark.py"); compile(p.read_bytes(), str(p), "exec")'
```
