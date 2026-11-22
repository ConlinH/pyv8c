# The three IDL locations

[简体中文](../../pyv8c/idl-layout.md) | **English**

- `pyv8c.engine.idl` reads the IDL baseline documents, applies the profile, and provides queries.
- `pyv8c.idl.spec` defines the interface and member declarations used by the Python implementation.
- `pyv8c/pyv8c/idl/idl.zip` is the Chromium IDL data shipped with the package, not a Python implementation.

Document and query views are reused through a bounded cache, and the generated interface classes are held by each Browser's Bindings;
load timing belongs to a single `Idl.load` call and is not shared between Browsers. Interface implementation coverage can be viewed with
`python tools/idl_audit.py --coverage`; adding `--json report.json` saves the full list.

The baseline shipped by default in the release package is the `pyv8c/pyv8c/idl/idl.zip` corresponding to `CHROMIUM_VERSION`. `CHROMIUM_VERSION` records the baseline source for the IDL extractor; no version consistency check runs at packaging time. `Idl.load(path=...)` can load an IDL document of any version, and profiles match by content digest rather than a version string.
