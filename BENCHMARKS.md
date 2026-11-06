# Isolated v0.1 release baseline

These are historical v0.1 measurements. See [docs/V02.md](docs/V02.md)
for v0.2 architecture and validation, and use the benchmark's `--subscribe` option
to measure a current build with an idle event subscriber.

Measured locally on Linux/Omarchy with Rust `1.99.1`, optimized release build
(thin LTO, stripped), on 2026-11-02. These are synthetic filesystem/IPC measurements,
**not live wallpaper/rendering measurements**. Both runs used generated tiny PNGs,
a temporary HOME/XDG/runtime/state/background, and a fake setter. They ran in
parallel on the same machine; filesystem caches and scheduler load affect results.

| Metric | 100 images | 10,000 images |
| --- | ---: | ---: |
| Release executable size | 1,774,272 bytes (1.69 MiB) | same |
| First IPC status response | 6.87 ms | 6.00 ms |
| IPC + scanned, persisted collection ready | 12.51 ms | 298.51 ms |
| Idle RSS before/after sampling | 3,768 KiB / 3,768 KiB | 12,100 KiB / 12,100 KiB |
| Idle sample duration | 10.0004 s | 10.0004 s |
| CPU ticks gained | 0 user, 0 system | 0 user, 0 system |
| Main-thread context switches gained | 0 voluntary, 0 involuntary | 0 voluntary, 0 involuntary |
| `--version` median, 20 separate processes | 2.78 ms | 1.68 ms |
| Original image hashes unchanged | yes | yes |
| SIGTERM shutdown | exit 0, no forced kill | exit 0, no forced kill |

CPU accounting resolution was 100 ticks/second (10 ms per tick). Zero observed
ticks is **below measurement resolution**, not proof that every possible idle
period consumes literally zero CPU. Context-switch counts are from the main
thread's `/proc/PID/status`; CPU tick totals cover the process. Startup values are
one sample per collection size, not percentile guarantees. No timer expired during
the idle interval, which is shorter than the configured 300 seconds. Resource
usage excludes Omarchy's existing renderer and any real image decoding it performs.

Reproduce from the repository root:

```sh
cargo build --release
python3 scripts/benchmark.py --binary target/release/swiftwall --seconds 10 --images 100
python3 scripts/benchmark.py --binary target/release/swiftwall --seconds 10 --images 10000
```

The script waits for collection readiness and persistence, settles for one second,
and emits JSON. See `scripts/benchmark_README.md` for safety and interpretation.
The daemon remains responsive before scanning finishes; collection memory scales
with path count. Real local collections, long paths, cold caches, larger image
headers, missing folders and storage latency need separate workload measurements.
