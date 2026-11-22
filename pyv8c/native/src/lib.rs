use pyo3::{ffi, prelude::*};

mod audio;

unsafe extern "C" {
    fn pyv8c_create_dom_module() -> *mut ffi::PyObject;
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = module.py();
    let audio_module = PyModule::new(py, "pyv8c._native.audio")?;
    audio::register(&audio_module)?;
    module.add("audio", audio_module)?;

    // The C++ initializer returns a new reference or sets a Python exception.
    let dom_module = unsafe { Bound::from_owned_ptr_or_err(py, pyv8c_create_dom_module()) }?;
    module.add("dom", dom_module)?;
    Ok(())
}
