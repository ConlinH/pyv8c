"""Build the single pyv8c distribution from the browser and native sources."""
from pathlib import Path
import os
import sys
import sysconfig

from setuptools import find_packages, setup
from setuptools_rust import Binding, RustExtension

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'pyv8'))
from build_support import BuildV8, ListV8Versions, PyV8BuildExt, ext_modules

setup(
    packages=find_packages(where='pyv8c', include=['pyv8c', 'pyv8c.*']),
    package_dir={'': 'pyv8c'},
    package_data={'pyv8c': ['data/*.json'],
                  'pyv8c.idl': ['idl.zip', 'idl-158.0.8127.36.zip'],
                  'pyv8c.pyv8': ['_pyv8.pyi', '__init__.pyi', 'py.typed', 'icudtl.dat']},
    include_package_data=False,
    ext_modules=ext_modules,
    rust_extensions=[RustExtension(
        'pyv8c._native._native', path='pyv8c/native/Cargo.toml',
        binding=Binding.PyO3, py_limited_api=True, args=['--locked'],
        env={**os.environ,
             'PYV8C_PYTHON_INCLUDE': sysconfig.get_path('include'),
             'PYV8C_PYTHON_PLATINCLUDE': sysconfig.get_path('platinclude')},
    )],
    cmdclass={'build_ext': PyV8BuildExt, 'build_v8': BuildV8,
              'list_v8_versions': ListV8Versions},
    options={'bdist_wheel': {'py_limited_api': 'cp312'}},
)
