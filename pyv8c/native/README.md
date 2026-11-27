# Native components

Cargo builds the `pyv8c-native` crate and compiles the DOM kernel in `dom/` as C++17.
The DOM sources separate module registration, node storage, tree construction,
fragment tokenization and attribute matching. The loaded module exposes a
fingerprint of the C++ sources and headers.
The C++ library links into the Rust PyO3 extension inside the
`pyv8c._native` package alongside Rust audio, Canvas and layout. The extension
uses the CPython 3.12 stable ABI. Python integration lives in `pyv8c._native`.

`src/canvas.rs` provides CPU raster surfaces and PNG, JPEG and WebP encoding
through Skia. PNG encoding releases the GIL around the Skia call.

Rust document layout lives in `src/layout_document.rs` and `src/layout_document/`.
Each document's `LayoutDocument` applies versioned DOM and style batches, routes
Flex/Grid to Taffy, and publishes an immutable geometry snapshot. The Rust
update and solve run with the GIL released.

The Rust component in `src/audio` generates the Chromium 157 FFT wave tables
on first use. The result is cached in memory; the old waveform ZIP is not needed
at runtime and is not included in wheels. It also renders supported constant
x86 oscillators in batches. Batch and single-sample rendering share the same
native phase and FFT interpolation code. Waveforms outside that FFT path,
including custom periodic waves, use their respective waveform implementations.

To build the extension in a source checkout, use CPython 3.12, a Rust
toolchain with 2024 edition support, and a C++17 compiler:

```console
python312 -m pip install "setuptools>=77" "setuptools-rust>=1.10,<2" wheel
python312 setup.py build_rust --release --inplace
```

Normal wheel builds place the binary inside `pyv8c._native`. Cargo.lock pins the Rust
dependencies. The generated values depend on the FFT implementation and target
architecture; compare against the intended Chromium version and platform after
changing those inputs. The audio graph is managed in Python; dynamics
compression runs in Rust with state shared by single-sample and block calls.

## Remaining performance work

The direct constant-oscillator-to-compressor graph is rendered in a native
block. Other graphs retain sample-by-sample graph scheduling while using the
same native compressor state. Batching additional graph types requires preserving
node ordering, automation, phase, and compressor state, and comparing the result
with Chrome's float32 output.
