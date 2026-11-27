use sha2::{Digest, Sha256};
use std::{env, ffi::OsStr, fs};

fn main() {
    println!("cargo:rerun-if-changed=dom");
    let mut sources: Vec<_> = fs::read_dir("dom")
        .expect("list DOM sources")
        .map(|entry| entry.expect("read DOM source entry").path())
        .filter(|path| {
            matches!(
                path.extension().and_then(|ext| ext.to_str()),
                Some("cpp" | "h")
            )
        })
        .collect();
    sources.sort();
    let mut hasher = Sha256::new();
    let mut compiler = cc::Build::new();
    compiler.cpp(true).std("c++17");
    for path in sources {
        let name = path
            .file_name()
            .and_then(|name| name.to_str())
            .expect("UTF-8 DOM filename");
        hasher.update(name.as_bytes());
        hasher.update([0]);
        hasher.update(Sha256::digest(fs::read(&path).expect("read DOM source")));
        if path.extension() == Some(OsStr::new("cpp")) {
            compiler.file(path);
        }
    }
    let source_hash = format!("\"{:x}\"", hasher.finalize());
    for key in ["PYV8C_PYTHON_INCLUDE", "PYV8C_PYTHON_PLATINCLUDE"] {
        println!("cargo:rerun-if-env-changed={key}");
        compiler.include(env::var_os(key).unwrap_or_else(|| panic!("{key} is set by setup.py")));
    }
    compiler
        .define("Py_LIMITED_API", "0x030C0000")
        .define("DOM_SOURCE_SHA256", source_hash.as_str())
        .opt_level(3)
        .compile("pyv8c_dom");
}
