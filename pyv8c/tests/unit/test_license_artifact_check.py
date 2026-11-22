"""The license check must examine the actual distribution, including file bytes."""

import importlib.util
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("check_license", ROOT / "tools/check_license.py")
check_license = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_license)


def test_wheel_license_check_detects_missing_and_modified_files(tmp_path):
    artifact = tmp_path / "example.whl"
    with zipfile.ZipFile(artifact, "w") as wheel:
        for name in check_license.LICENSE_FILES:
            if name == "LICENSE":
                continue
            data = (ROOT / name).read_bytes()
            if name == "third_party_licenses/ICU-LICENSE.txt":
                data = b"incomplete license"
            wheel.writestr("example-1.dist-info/licenses/" + name, data)

    errors = []
    check_license.check_artifact(artifact, errors)
    assert any("missing LICENSE" in error for error in errors)
    assert any("ICU-LICENSE.txt differs" in error for error in errors)


def test_wheel_license_check_accepts_complete_files(tmp_path):
    artifact = tmp_path / "example.whl"
    with zipfile.ZipFile(artifact, "w") as wheel:
        for name in check_license.LICENSE_FILES:
            wheel.writestr("example-1.dist-info/licenses/" + name, (ROOT / name).read_bytes())

    errors = []
    check_license.check_artifact(artifact, errors)
    assert errors == []
