use sha2::{Digest, Sha256};
use std::{env, fs};

fn main() {
    let source = "dom/builder.cpp";
    println!("cargo:rerun-if-changed={source}");
    let hash = format!("{:x}", Sha256::digest(fs::read(source).expect("read DOM source")));
    let source_hash = format!("\"{hash}\"");
    let mut compiler = cc::Build::new();
    compiler.cpp(true).std("c++17").file(source);
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
