# Rust document layout

Rust owns document layout, including Taffy Flex/Grid solving. This report retains
the Windows comparisons measured before the old engines were retired. The
four-thread 128-node workload had lower aggregate throughput than one thread in
those comparisons.

## Current implementation

`LayoutDocument` owns the persistent native layout tree, applies versioned Python batches,
measures text through the bundled Skia/HarfBuzz stack, solves layout without the
GIL, and publishes immutable geometry snapshots. `DocumentLayout` supplies stable
DOM keys and computed-style snapshots. Geometry reads and hit testing can use the
native snapshot while DOM objects and Web API bindings remain Python-owned.

The implemented formatting contexts cover block, flex, grid, inline text grouping,
absolute/fixed positioning, basic overflow, replaced controls and transforms. This
covers the scope of the current layout implementation. Browser parity outside the
cases measured here remains unverified.

## Historical tests

On Windows, CPython 3.12 passed the forced-native layout, Shadow DOM, frame, style
and mouse-coordinate regression set: 84 passed. Its full suite completed with
4,783 passed and 14 skipped, with 12 failures in WebSocket cancellation or
certificate cleanup and existing Chrome 158 behavior comparisons. CPython 3.15
passed the dedicated native `LayoutDocument` tests: 5 passed. The same 3.15
full-suite run completed with 4,726 passed and 60 skipped, but 23 tests failed.
Thirteen failures came from missing `setuptools` in that interpreter. Eight were
existing Chrome 158 behavior differences in request headers, rendering frame
clocks, permissions policy, navigation timing and resource timing. One architecture
check caught the experimental metrics field using an ownership-reserved name, which
has been corrected. The remaining failure came from inherited Git configuration
variables that supplied an empty config value.

The forced-native semantic suite passed after the final font-size fix. Seven
geometry-related Chrome 158 repros also passed with the native engine forced on.
This subset does not establish full Chrome parity.

## Historical three-engine comparison

The table reports medians of five runs on CPython 3.15, Windows, 40 measured
operations per worker. Throughput is aggregate operations per second. P95 is the
per-operation end-to-end latency. Peak memory is private process memory while the
workers are active. Each worker owns its Browser and page.

| Engine | Threads | Layout throughput | Layout P95 | Layout peak MiB | Text/nested throughput | Text/nested P95 | Text/nested peak MiB | Replace throughput | Replace P95 | Replace peak MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Python | 1 | 116.8 | 9.32 ms | 79.2 | 145.8 | 7.52 ms | 78.5 | 107.0 | 10.30 ms | 79.7 |
| Python | 2 | 101.3 | 28.25 ms | 99.3 | 129.1 | 19.47 ms | 98.4 | 104.2 | 35.26 ms | 100.2 |
| Python | 4 | 89.6 | 53.81 ms | 125.5 | 116.0 | 39.79 ms | 122.2 | 102.4 | 75.13 ms | 122.1 |
| Taffy subtree | 1 | 340.5 | 3.73 ms | 78.4 | 134.9 | 8.16 ms | 79.2 | 152.0 | 7.66 ms | 80.8 |
| Taffy subtree | 2 | 271.7 | 10.16 ms | 99.7 | 121.9 | 21.21 ms | 99.8 | 145.4 | 26.34 ms | 100.6 |
| Taffy subtree | 4 | 190.7 | 25.83 ms | 125.4 | 108.5 | 43.04 ms | 121.9 | 147.4 | 58.32 ms | 123.8 |
| Rust document | 1 | 323.1 | 3.93 ms | 79.2 | 553.3 | 2.48 ms | 169.7 | 136.9 | 7.71 ms | 170.7 |
| Rust document | 2 | 252.0 | 10.08 ms | 101.1 | 436.4 | 6.13 ms | 209.6 | 140.5 | 20.02 ms | 198.5 |
| Rust document | 4 | 193.8 | 26.16 ms | 128.4 | 353.1 | 14.89 ms | 304.2 | 135.3 | 56.12 ms | 229.2 |

The layout case misses the concurrency gate: Rust's two- and four-thread medians
are below its single-thread median. The text/nested case also loses throughput at
two and four threads, and its peak memory grows sharply. Replace-block throughput
changes little across worker counts.

The layout profile shows that a one-thread worker spends about 7 microseconds
applying its batch, 19 microseconds in the native solve, 50 microseconds building
the immutable geometry snapshot and 63 microseconds in Python capture. At four
threads those averages rise to about 9, 25, 65 and 72 microseconds. The native
solve runs outside the GIL and accounts for a small fraction of end-to-end time.
These samples leave the cost of contention in other stages unresolved.

## Geometry snapshot reuse

The native snapshots now share immutable DOM ancestry, offset parents, paint
order and the set of transformed nodes. Changes to topology or relevant styles
rebuild that metadata. Each solve still publishes fresh box values and resolves
transform origins against those values. A Python profile of 80 layout batches
found 20,480 calls to the geometry API and 160 layout updates. A clean read now
returns the existing snapshot before fetching the rule set and entering capture.

The before and after builds ran in alternating order on Windows with CPython
3.15.0, five rounds and 40 operations per worker. The following results use the
same three benchmark fixtures. Throughput is in batches/s and P95 is in ms.
Snapshot time is the median of the final solve samples reported by each worker,
in microseconds. The private-memory peak for the 128-node workload was about
80, 101 and 128 MiB on both builds.

| Case | Threads | Throughput before → after | P95 before → after | Snapshot before → after |
| --- | ---: | ---: | ---: | ---: |
| 128-node layout | 1 | 306.4 → 333.2 | 4.18 → 3.91 | 51.7 → 5.4 |
| 128-node layout | 2 | 254.2 → 279.3 | 10.05 → 9.86 | 61.3 → 5.4 |
| 128-node layout | 4 | 196.8 → 194.6 | 25.40 → 25.19 | 63.2 → 5.8 |
| Text/nested | 1 | 527.0 → 584.3 | 2.62 → 2.37 | 43.0 → 5.7 |
| Text/nested | 2 | 426.0 → 448.1 | 6.32 → 5.61 | 48.6 → 5.6 |
| Text/nested | 4 | 327.3 → 335.7 | 16.16 → 16.35 | 49.0 → 8.1 |
| Replace block | 1 | 126.2 → 132.8 | 8.85 → 8.34 | 56.1 → 52.2 |
| Replace block | 2 | 132.4 → 127.7 | 21.27 → 21.67 | 58.1 → 51.1 |
| Replace block | 4 | 127.4 → 130.0 | 54.70 → 53.57 | 65.0 → 50.5 |

The 128-node snapshot takes about 90% less time and still publishes 130 boxes
per solve. Native solving takes 22–28 microseconds for the sampled updates and
Python capture takes 68–75 microseconds. Whole-subtree replacement rebuilds the
metadata each time and takes about 5.2 ms in Python capture. The two-thread
128-node case improved in all five before/after pairs. The new two- and
four-thread totals exceeded the new single-thread total in zero of five rounds,
leaving the default-switch gate unmet.

The forced-Rust layout suite passed 90 tests each on CPython 3.12 and 3.15.
The CPython 3.15 run with normal backend selection passed 96 tests, including the
Taffy subtree suite. The seven geometry repros passed both their fixed
expectations and their Chrome comparisons. The snapshot tests cover paint order and
offset-parent changes, transform origins after resize, retained snapshots, and
clean reads that skip stylesheet rule lookup.

Local raw data is available under
`build/rust-layout/snapshot-ab/results-5x40.json`. The intermediate comparison
with only native metadata reuse is in `geometry-only-5x40.json` in the same
directory.

## Geometry callback contention

A separate Windows diagnostic build timed `PyEval_RestoreThread` at the
V8-to-Python callback boundary. Each worker ran 120 measured batches on CPython
3.15.0. The 128-node fixture makes 256 geometry reads and 256 width reads per
batch. The full workload also makes DOM query, collection and mutation calls.
The table shows mean wall time summed and divided by completed batches. The GIL
column measures elapsed time inside the restore call, including any scheduling
delay. Style capture and Taffy solve are the sums of both updates in each batch.

| Threads | Full batch ms | V8 callback GIL restore ms | Python style capture and batch preparation ms | Native solve ms |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 2.527 | 0.055 | 0.116 | 0.040 |
| 2 | 6.760 | 3.095 | 0.138 | 0.047 |
| 4 | 18.455 | 13.362 | 0.148 | 0.054 |

The full workload had 775 callback acquisitions per batch at every thread
count. With four threads, 256 cached `getBoundingClientRect()` calls alone took
12.60 ms, including 9.25 ms restoring the GIL. Reading the `width` of 256
pre-created DOMRects took 0.95 ms, including 0.75 ms restoring the GIL. Reading
the same values from plain JavaScript objects took 0.12 ms and made zero callback
acquisitions. These controls identify the frequent geometry callback as the
main source of contention. The restore timer covers V8 callback entry. The Rust
solve timer covers the detached Taffy call, with PyO3 reattachment outside it.

A single-thread Python profile also placed snapshot validation at the top of the
geometry handler's cumulative time. Each read previously walked the document's
children to find the root and allocated an animation iterator on pages with no
tracked animations. Rust Layout now uses the root from the published snapshot
while the DOM layout version matches. Viewport and iframe visibility checks
remain live, and animation checks return early for an empty collection.
The Element module also resolves the DOMRect type at import time. Each call still
returns a fresh DOMRect through the existing V8 binding.

In a second diagnostic run, four-thread callback GIL restore time fell from
13.36 to 12.23 ms per full batch. The cached geometry-only control fell from
12.60 to 11.21 ms per batch, including a decline in GIL restore time from 9.25
to 8.22 ms. The full batch still makes 775 callback acquisitions. Style
preparation and solve remained near 0.15 and 0.06 ms per four-thread batch.

The ordinary binaries ran the same benchmark in five alternating before/after
pairs per thread count, with 40 operations per worker on Windows CPython 3.15.0.
The table shows the median of the runs.

| Case | Threads | Throughput before → after, batches/s | P95 before → after, ms |
| --- | ---: | ---: | ---: |
| 128-node layout | 1 | 385.8 → 420.8 | 3.34 → 3.08 |
| 128-node layout | 2 | 295.0 → 330.4 | 8.86 → 8.14 |
| 128-node layout | 4 | 217.1 → 229.4 | 23.25 → 20.87 |
| Text/nested | 1 | 646.1 → 695.0 | 2.09 → 2.06 |
| Text/nested | 2 | 475.1 → 519.6 | 5.46 → 5.08 |
| Text/nested | 4 | 389.9 → 421.0 | 13.93 → 13.20 |
| Replace block | 1 | 141.0 → 139.0 | 7.52 → 7.93 |
| Replace block | 2 | 144.4 → 140.8 | 18.77 → 19.94 |
| Replace block | 4 | 136.8 → 135.0 | 55.57 → 51.80 |

Four-thread 128-node throughput improved in 5/5 before/after pairs. Its median
was 54.5% of the new single-thread throughput, and neither multi-thread
configuration exceeded single-thread throughput in any of the five rounds.
Whole-block replacement still spends about 5 ms per update in Python capture.
Its throughput fell 1–3% and the two-thread P95 rose about 6%.

The forced-Rust suite passed 107 tests on each of CPython 3.12 and 3.15. The
normal backend selection passed 160 targeted layout and architecture checks on
3.15. Seven geometry repros passed both fixed expectations and Chrome
comparisons. The native layout regression checks that replacing the document
root invalidates the cached root and preserves old DOMRect values.

Diagnostic scripts and samples are under `build/rust-layout/bridge-ab/`.
`profile-before-*.json` and `profile-after-*.json` use the diagnostic binary.
`results-5x40.json` contains the paired runs using the ordinary binary.

## Single-engine verification

The default browser path now uses Rust document layout. The switch exposed two
differences in native line composition, which were corrected using the existing
tests for CSS `overflow-wrap: break-word` and a leading `<br>`.

The Windows regression set includes the V8 bridge, unit and browser suites,
and selected integration suites for layout consumers, input and animation.
CPython 3.12 passed 2,846 tests with one skip, and CPython 3.15 passed 2,847.
Seven geometry repros passed both their fixed expectations and Chrome
comparisons. The rebuilt extension and the benchmark run without backend
selection.

## Geometry reads after the layout review

A cached geometry read now uses the validated snapshot's node map directly.
When a fresh solve is needed, a detached element still returns empty geometry
without starting the solve. The V8 bindings and Rust solver use the same builds
on both sides of the comparison.

Windows CPython 3.15.0 ran five alternating before/after pairs with 40 batches
per worker in each run. The table shows medians of throughput in batches/s,
end-to-end P95 in ms, and peak process private memory in MiB.

| Case | Threads | Throughput before → after | P95 before → after | Peak before → after |
| --- | ---: | ---: | ---: | ---: |
| 128-node layout | 1 | 438.9 → 497.7 | 2.91 → 2.39 | 80.2 → 80.3 |
| 128-node layout | 2 | 349.4 → 352.0 | 7.35 → 6.85 | 102.7 → 102.8 |
| 128-node layout | 4 | 250.8 → 263.3 | 20.11 → 17.88 | 129.0 → 129.0 |
| Text/nested | 1 | 720.5 → 777.3 | 1.60 → 1.47 | 169.3 → 169.8 |
| Text/nested | 2 | 539.7 → 589.5 | 4.95 → 4.48 | 227.5 → 227.6 |
| Text/nested | 4 | 434.2 → 463.7 | 11.71 → 11.31 | 316.9 → 304.7 |
| Replace block | 1 | 141.0 → 141.2 | 8.03 → 7.29 | 171.4 → 171.1 |
| Replace block | 2 | 146.9 → 145.4 | 18.77 → 16.21 | 199.7 → 199.4 |
| Replace block | 4 | 137.3 → 137.3 | 52.84 → 52.40 | 230.3 → 228.8 |

All five four-thread pairs improved on the 128-node and text/nested fixtures.
The four-thread 128-node median reached 53% of the new single-thread median.
The number of callbacks entering Python is the same for the retained change.
An earlier V8 numeric-property prototype removed Python calls for fresh
rectangle property reads. On the 128-node fixture it changed the four-thread
throughput from 250.3 to 251.7 batches/s and the two-thread throughput from
335.2 to 321.5 batches/s. That prototype was removed.

Raw samples are under `build/rust-layout/rect-access/` in
`snapshot-read-results.json` and `results.json`.

## Cascade cost during subtree replacement

On Windows CPython 3.15, five alternating before/after pairs ran 40 batches per
worker with the same Rust and V8 binaries. The table gives medians. The cascade
now fetches the root font size on demand for rem units and checks declared
properties for length conversion. Shorthand expansion and inherited animated
styles retain the full property scan.

| Workload | Threads | Throughput before → after (batches/s) | P95 before → after (ms) |
| --- | ---: | ---: | ---: |
| Replace block | 1 | 136.8 → 158.1 | 8.52 → 7.36 |
| Replace block | 2 | 144.4 → 163.7 | 20.05 → 14.66 |
| Replace block | 4 | 136.5 → 154.3 | 54.28 → 46.77 |
| 128-node layout | 1 | 494.1 → 498.3 | 2.61 → 2.80 |
| 128-node layout | 2 | 374.1 → 365.4 | 6.66 → 6.51 |
| 128-node layout | 4 | 261.9 → 259.3 | 18.49 → 18.51 |
| Text/nested | 1 | 767.9 → 792.4 | 1.50 → 1.46 |
| Text/nested | 2 | 591.2 → 583.9 | 4.45 → 4.46 |
| Text/nested | 4 | 460.1 → 464.1 | 11.14 → 11.19 |

Replace-block throughput improved in all five pairs at each thread count.
The median final-update Python capture sample at one thread fell from 4.99 to
4.19 ms. In a separate count over 40 replacements, property eligibility checks
fell from 143,280 to 10,240 and root font-size lookups from 10,640 to zero.
This fixture specifies widths in pixels.

A separate five-pair single-thread run of 400 batches per sample repeated the
128-node case because the short run showed a P95 increase. Median throughput
went from 483.3 to 504.3 batches/s and median P95 from 2.55 to 2.47 ms.
Local samples are in `build/style-cascade-095116973/results.json` and
`layout-1-long.json` in the same directory.

## Reading viewport dimensions together

A warm single-thread Python probe on the 128-node page measured 1.23 µs per snapshot lookup,
0.15 µs per native coordinate lookup and 0.19 µs to construct a Python DOMRect.
A separate JS control making 128 method calls took 0.067 ms returning a number,
0.110 ms returning the same rectangle and 0.336 ms returning fresh rectangles.
The fresh-object measurement includes Python construction and V8 wrapping.

Layout now reads both viewport dimensions together. An embedded document uses one
parent layout lookup for the pair. The nested fixture reads a warmed leaf rectangle
16 times per batch through two same-origin iframe documents. The launch lock covers
V8 initialization only. The fixture measures 20 batches per worker, and the existing
three fixtures measure 40. The figures are medians of five alternating before/after
pairs on Windows CPython 3.15 using the production extensions.

| Workload | Threads | Throughput before → after (batches/s) | P95 before → after (ms) |
| --- | ---: | ---: | ---: |
| 128-node layout | 1 | 483.0 → 482.2 | 2.55 → 2.43 |
| 128-node layout | 2 | 354.6 → 370.9 | 6.95 → 6.46 |
| 128-node layout | 4 | 253.1 → 260.3 | 18.70 → 18.15 |
| Text/nested | 1 | 762.4 → 779.6 | 1.54 → 1.53 |
| Text/nested | 2 | 563.5 → 577.6 | 4.85 → 4.73 |
| Text/nested | 4 | 447.5 → 453.4 | 11.35 → 11.28 |
| Replace block | 1 | 151.0 → 153.8 | 7.23 → 7.11 |
| Replace block | 2 | 160.9 → 161.0 | 17.72 → 16.05 |
| Replace block | 4 | 144.8 → 145.1 | 48.97 → 49.08 |
| Two nested frames | 1 | 201.0 → 288.2 | 5.48 → 3.84 |
| Two nested frames | 2 | 187.0 → 260.2 | 16.10 → 10.06 |
| Two nested frames | 4 | 174.1 → 240.9 | 43.03 → 30.60 |

In a separate four-thread diagnostic run of 120 batches per worker, the full
128-node workload entered Python 775 times per batch on both versions. Time
inside GIL restoration (including scheduler delay) averaged 11.92 ms before
and 11.78 ms after, out of 16.54 and 16.42 ms of batch wall time respectively.

Local raw samples are in `build/geometry-hot-100039533/results.json` and
`frames.json` in the same directory.

## Cached embedding styles during geometry reads

An embedding visibility check now reads connected ancestors through the owning
document's versioned computed-style cache. Detached frames retain direct style
calculation. On the warm two-frame fixture, 16 rectangle reads triggered 288
cascade calls including recursive calls before the change and zero afterwards.
On the ordinary element path, an absolute local import resolves the layout
function in about 0.17 µs versus 0.30 µs for the relative import in a single-thread probe.

Windows CPython 3.15, five alternating pairs per thread count. Each worker ran
40 batches in the standard cases and 20 batches of 16 reads in the two-frame
case. The table reports median total throughput and median end-to-end P95.

| Workload | Threads | Throughput before → after (batches/s) | P95 before → after (ms) |
| --- | ---: | ---: | ---: |
| 128-node layout | 1 | 498.9 → 534.4 | 2.38 → 2.22 |
| 128-node layout | 2 | 359.4 → 387.9 | 6.77 → 6.15 |
| 128-node layout | 4 | 255.5 → 280.6 | 18.48 → 16.97 |
| Text/nested | 1 | 793.1 → 824.8 | 1.45 → 1.46 |
| Text/nested | 2 | 591.3 → 620.7 | 4.51 → 4.32 |
| Text/nested | 4 | 454.9 → 475.5 | 11.09 → 10.30 |
| Replace block | 1 | 150.0 → 152.7 | 7.30 → 7.24 |
| Replace block | 2 | 162.9 → 164.7 | 17.65 → 15.47 |
| Replace block | 4 | 151.3 → 151.7 | 46.69 → 47.54 |
| Two nested frames | 1 | 282.4 → 2399.9 | 3.79 → 0.47 |
| Two nested frames | 2 | 262.5 → 1722.3 | 9.86 → 1.85 |
| Two nested frames | 4 | 243.9 → 1528.4 | 28.85 → 4.12 |

Local samples are in `build/geometry-check-101343834/results.json` and
`frames.json` in the same directory.

## Document and viewport lookups on warm geometry reads

Reading a rectangle from a published snapshot still resolves the node's owner
document, checks embedding visibility and reads the live viewport. The common
path now reads the service attributes directly where available. Nodes with
partial services retain the realm viewport or default dimensions. Ownership
and frame visibility are evaluated on each read.

On a warmed Windows CPython 3.15 page, the minimum of three 50,000-call Python
timings fell from 0.182 to 0.140 µs for `ownerDocument`, from 1.043 to 0.859 µs
for `layout_snapshot(document, element=node)`, and from 2.135 to 1.864 µs for
the Python rectangle getter. The latter two timings include nested calls.

Five paired runs alternated the two source versions using the same native
binary and 40 batches per worker. Values below are the medians across runs.

| Workload | Threads | Batches/s before → after | P95 ms before → after |
| --- | ---: | ---: | ---: |
| 128-node layout | 1 | 529.0 → 545.2 | 2.476 → 2.124 |
| 128-node layout | 2 | 381.4 → 383.6 | 6.265 → 6.525 |
| 128-node layout | 4 | 279.8 → 286.2 | 17.144 → 16.623 |
| Text/nested | 1 | 824.1 → 833.2 | 1.462 → 1.397 |
| Text/nested | 2 | 614.8 → 624.3 | 4.275 → 4.213 |
| Text/nested | 4 | 482.7 → 491.1 | 10.304 → 10.239 |
| Replace block | 1 | 148.4 → 152.2 | 7.469 → 7.356 |
| Replace block | 2 | 161.4 → 163.0 | 18.169 → 16.251 |
| Replace block | 4 | 153.2 → 154.2 | 48.429 → 43.941 |

The 128-node four-thread throughput increased in all five pairs and reached
52.5% of single-thread throughput. The two-thread P95 increase in the short
run prompted six additional alternating pairs at 200 batches per worker.
Throughput rose from 371.9 to 388.7 batches/s and P95 moved from 6.358 to
6.398 ms. These measurements show a smaller cost per geometry read and no
consistent two-thread latency improvement.

Local samples are in `build/geometry-read-cost-110243845/results.json` and
`layout-two-long.json` in the same directory.

## Reproduction

Build the extensions from the repository root with
`python setup.py build_ext --inplace build_rust --release --inplace`. With the project package available on
`PYTHONPATH`, run the current document layout benchmark:

```powershell
python -m benchmarks.layout_document 1 --iterations 40
python -m benchmarks.layout_document 2 --iterations 40
python -m benchmarks.layout_document 4 --iterations 40
```

Use the same interpreter, source tree, fonts and machine for comparisons. Repeat
each configuration five times. The historical local samples are kept under
`build/rust-layout/final-rust-*.jsonl` and `build/rust-layout/bench-*.jsonl` and
are not tracked.

Historical samples with `native_profile` came from instrumented builds. The
current benchmark measures end-to-end latency, throughput, CPU and process
memory. Layout solve returns the geometry snapshot directly.
