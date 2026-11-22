import pytest

from tools.fetch_chromium import ChromiumFetchError, read_default_chromium_version


def test_default_chromium_version_comes_from_tracked_file(tmp_path):
    (tmp_path / "CHROMIUM_VERSION").write_text("154.0.8030.0\n", encoding="ascii")

    assert read_default_chromium_version(tmp_path) == "154.0.8030.0"


def test_missing_or_invalid_default_chromium_version_is_rejected(tmp_path):
    with pytest.raises(ChromiumFetchError, match="missing default"):
        read_default_chromium_version(tmp_path)

    (tmp_path / "CHROMIUM_VERSION").write_text("not-a-version\n", encoding="ascii")
    with pytest.raises(ChromiumFetchError, match="invalid Chromium version"):
        read_default_chromium_version(tmp_path)
