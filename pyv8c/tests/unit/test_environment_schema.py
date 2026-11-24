"""The environment profile: one table of dotted keys, written and read back."""
import json

import pytest

from pyv8c.foundation import environment as environment_module
from pyv8c.foundation.environment import (
    COLLECTED_KEYS,
    ENVELOPE_KEYS,
    FIELDS,
    PROFILE,
    SCHEMA_VERSION,
    BrowserEnvironment,
    extension_resource,
    profile_path,
)

WINDOWS_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
LINUX_CHROME = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PLUGIN = {"name": "Page Plugin", "filename": "page-plugin",
          "mime_types": [{"type": "application/page", "suffixes": "pg"}]}


def test_every_key_is_declared_once_and_its_default_is_valid():
    names = [field.name for field in FIELDS]
    assert len(names) == len(set(names))
    for field in FIELDS:
        if field.default is not None:
            field.validate(field.default)
    assert set(COLLECTED_KEYS) <= set(names)
    assert not set(ENVELOPE_KEYS) & set(names)


def test_profile_path_follows_the_host():
    assert profile_path("win32") == PROFILE
    darwin = PROFILE.with_name("chrome-environment-darwin.json")
    assert profile_path("darwin") == darwin
    assert darwin.is_file()
    assert profile_path("linux") == PROFILE
    assert BrowserEnvironment().user_agent == BrowserEnvironment.from_file(PROFILE).user_agent


def test_profile_path_falls_back_when_the_host_file_is_missing(monkeypatch, tmp_path):
    missing = tmp_path / "chrome-environment-darwin.json"
    monkeypatch.setattr(environment_module, "_HOST_PROFILES", {"darwin": missing})
    assert profile_path("darwin") == PROFILE


def test_the_defaults_are_the_shipped_profile():
    profile = {name: value for name, value in json.loads(
        PROFILE.read_text(encoding="utf-8")).items() if name not in ENVELOPE_KEYS}
    default = BrowserEnvironment()
    assert default.platform == profile["platform"]
    assert default.hardware_concurrency == profile["hardware_concurrency"]
    assert default.languages == tuple(profile["languages"])
    assert default.viewport.width == profile["viewport.width"]
    assert default.battery.level == profile["battery.level"]
    assert [brand.brand + brand.version for brand in default.client_hints.brands] == [
        entry["brand"] + entry["version"] for entry in profile["client_hints.brands"]]
    assert [plugin.name for plugin in default.plugins] == [
        plugin["name"] for plugin in profile["plugins"]]


def test_the_measured_facts_read_back_as_views():
    """One key per fact, and a profile without them leaves the page its own fallback."""
    environment = BrowserEnvironment({
        "viewport.outer_inset_width": 4,
        "viewport.outer_inset_height": 30,
        "media.constraints": ["width", "height"],
        "speech.voices": [{"name": "Voice", "lang": "en-GB", "local_service": True}],
        "permissions.names": ["camera"],
        "permissions.granted": ["camera"],
        "permissions.spec_names": {"camera": "video_capture"},
        "gpu.wgsl_features": ["subgroup_id"],
    })

    assert (environment.viewport.outer_inset_width,
            environment.viewport.outer_inset_height) == (4, 30)
    assert environment.media.constraints == ("width", "height")
    assert environment.speech.voices == ({"name": "Voice", "lang": "en-GB",
                                          "local_service": True, "default": False},)
    assert environment.permissions.names == ("camera",)
    assert environment.permissions.granted == ("camera",)
    assert dict(environment.permissions.spec_names) == {"camera": "video_capture"}
    assert environment.gpu.wgsl_features == ("subgroup_id",)

    # An empty list is an answer: a profile reporting no voices leaves the page its fallback.
    empty = BrowserEnvironment({"speech.voices": [], "media.constraints": []})
    assert empty.speech.voices == ()
    assert empty.media.constraints == ()


@pytest.mark.parametrize("key, value", (
    ("speech.voices", [{"lang": "en-US"}]),               # a voice needs a name
    ("speech.voices", [{"name": "a"}, {"name": "a"}]),    # and names do not repeat
    ("permissions.spec_names", ["camera"]),               # a table, not a list
    ("viewport.outer_inset_width", True),                 # an inset is a number
))
def test_a_measured_fact_of_the_wrong_shape_is_refused(key, value):
    with pytest.raises((TypeError, ValueError)):
        BrowserEnvironment({key: value})


def test_resolved_fills_every_key_and_to_dict_holds_only_what_was_given():
    environment = BrowserEnvironment({"screen.width": 800})
    assert environment.to_dict() == {"screen.width": 800}
    resolved = environment.resolved()
    assert sorted(resolved) == sorted(field.name for field in FIELDS)
    assert resolved["screen.width"] == 800
    assert resolved["screen.height"] == 1080
    assert resolved["app_version"] == resolved["user_agent"].removeprefix("Mozilla/")
    assert resolved["language"] == resolved["languages"][0]


def test_derived_keys_follow_the_user_agent_they_came_from():
    environment = BrowserEnvironment({
        "user_agent": LINUX_CHROME,
        "client_hints.platform_version": "6.5.0",
    })
    assert environment.app_version == LINUX_CHROME.removeprefix("Mozilla/")
    # The brand list is the profile's; only the two real brands' versions follow the user agent.
    reference = BrowserEnvironment().client_hints.brands
    assert [brand.brand for brand in environment.client_hints.brands] == [brand.brand for brand in reference]
    assert [brand.version for brand in environment.client_hints.brands] == [
        "120" if brand.brand in ("Google Chrome", "Chromium") else brand.version for brand in reference]
    assert environment.client_hints.platform == "Linux"
    assert environment.client_hints.architecture == "x86"
    assert environment.client_hints.platform_version == "6.5.0"

    # A later user agent moves everything that followed it, and an explicit
    # member is still the caller's to keep.
    moved = environment.overlay({"user_agent": WINDOWS_CHROME})
    assert moved.client_hints.platform == "Windows"
    assert moved.client_hints.platform_version == "6.5.0"
    assert [brand.version for brand in moved.client_hints.brands] == [
        "152" if brand.brand in ("Google Chrome", "Chromium") else brand.version for brand in reference]


def test_group_views_read_the_values_the_table_holds():
    environment = BrowserEnvironment({
        "viewport.width": 390,
        "screen.avail_height": 600,
        "battery.level": 0.5,
        "connection.effective_type": "3g",
        "storage.temporary.quota": 5000,
        "client_hints.mobile": True,
        "plugins": [PLUGIN],
    })
    assert environment.viewport.width == 390
    assert environment.viewport.device_scale_factor == 1.0
    assert environment.screen.avail_height == 600
    assert environment.battery.level == 0.5
    assert environment.connection.effective_type == "3g"
    assert environment.storage.temporary.quota == 5000
    assert environment.storage.persistent.usage == 0
    assert environment.client_hints.mobile is True
    assert environment.plugins[0].name == "Page Plugin"
    assert environment.plugins[0].mime_types[0].suffixes == "pg"


def test_a_profile_round_trips_through_text_and_file(tmp_path):
    environment = BrowserEnvironment({
        "viewport.width": 390,
        "storage.temporary.quota": 5000,
        "client_hints.mobile": True,
        "plugins": [PLUGIN],
    })
    text = environment.dumps(resolved=True, source={"browser": "chrome-test"})
    document = json.loads(text)
    assert document["schema"] == SCHEMA_VERSION
    assert document["source"] == {"browser": "chrome-test"}
    assert sorted(document) == sorted(["schema", "source", *(f.name for f in FIELDS)])

    assert BrowserEnvironment.loads(text) == environment
    assert BrowserEnvironment.from_dict(environment.to_dict()) == environment
    path = tmp_path / "profile.json"
    environment.to_file(path, resolved=True)
    assert BrowserEnvironment.from_file(path) == environment


def test_a_saved_profile_is_json_a_strict_parser_accepts():
    """``Infinity`` is not JSON: a non-finite number travels as its own name."""
    environment = BrowserEnvironment({
        "battery.discharging_time": float("inf"),
        "connection.downlink_max": float("-inf"),
    })
    text = environment.dumps(resolved=True)
    document = json.loads(text)
    assert json.dumps(document, allow_nan=False), "a bare Infinity would raise here"
    assert document["battery.discharging_time"] == "Infinity"
    assert document["connection.downlink_max"] == "-Infinity"
    assert BrowserEnvironment.loads(text) == environment

    json.dumps(json.loads(PROFILE.read_text(encoding="utf-8")), allow_nan=False)


def test_a_document_of_another_version_is_refused():
    with pytest.raises(ValueError):
        BrowserEnvironment.loads('{"schema": 99}')
    for other in (SCHEMA_VERSION - 1, SCHEMA_VERSION + 1):
        with pytest.raises(ValueError):
            BrowserEnvironment({"schema": other})


def test_unknown_keys_and_bad_values_name_the_key():
    with pytest.raises(TypeError, match="unknown environment key: screen.widht"):
        BrowserEnvironment({"screen.widht": 800})
    for key, value, error in (
        ("screen.width", "wide", TypeError),
        ("viewport.device_scale_factor", True, TypeError),
        ("connection.effective_type", "5g", ValueError),
        ("storage.temporary.usage", 10 ** 12, ValueError),
        ("client_hints.brands", [{"brand": "X"}], TypeError),
        ("plugins", [{"name": "a"}, {"name": "a"}], ValueError),
        ("plugins", [{"name": "a", "mime_types": [{"type": "a"}, {"type": "a"}]}], ValueError),
        ("timezone", None, TypeError),
    ):
        with pytest.raises(error, match=key.split(".")[-1]):
            BrowserEnvironment({key: value})


def test_an_environment_is_immutable():
    environment = BrowserEnvironment()
    with pytest.raises(AttributeError):
        environment.user_agent = "other"
    with pytest.raises(AttributeError):
        environment.screen = None
    with pytest.raises(AttributeError):
        environment.nope


# -- the extensions a page can read out of ------------------------------------

X_PATH = "cneomjecgakdfoeehmmmoiklncdiodmh"
TRANSLATE = "aapbdbdomjkkjkaonfhkkikfgjllcleb"


def installed(**overrides):
    """One enabled extension: a literal file, a glob, and the origins it names."""
    declaration = {
        X_PATH: [{"resources": ["/icons/icon_64.png", "/_locales/*/messages.json"],
                  "matches": ["http://*/*", "https://*/*"]}],
    }
    declaration.update(overrides)
    return BrowserEnvironment({"extensions": declaration})


def test_an_extension_declaration_is_kept_in_chromes_own_shape():
    environment = installed(**{TRANSLATE: ["popup_css_compiled.css"]})
    assert environment.extensions[TRANSLATE] == (
        {"resources": ("popup_css_compiled.css",), "matches": ("<all_urls>",)},
    )


def test_an_extension_id_and_its_resources_are_checked():
    with pytest.raises(ValueError, match="not an extension id"):
        BrowserEnvironment({"extensions": {"not-an-id": ["a.css"]}})
    with pytest.raises(TypeError, match="must list the resources"):
        BrowserEnvironment({"extensions": {TRANSLATE: []}})
    with pytest.raises(TypeError, match="at least one resource pattern"):
        BrowserEnvironment({"extensions": {TRANSLATE: [{"matches": ["<all_urls>"]}]}})
    with pytest.raises(TypeError, match="must map extension ids"):
        BrowserEnvironment({"extensions": ["a.css"]})


def test_a_hit_needs_the_installed_id_the_declared_path_and_a_named_source():
    environment = installed()
    source = "https://www.radissonhotels.com/zh-cn/"
    assert extension_resource(environment, f"chrome-extension://{X_PATH}/icons/icon_64.png",
                              source_url=source) == "icons/icon_64.png"
    assert extension_resource(environment, f"chrome-extension://{X_PATH}/_locales/am/messages.json",
                              source_url=source) == "_locales/am/messages.json"
    # A path the extension does not expose, an id it does not have, and another scheme.
    assert extension_resource(environment, f"chrome-extension://{X_PATH}/content.css",
                              source_url=source) is None
    assert extension_resource(environment, f"chrome-extension://{TRANSLATE}/icons/icon_64.png",
                              source_url=source) is None
    assert extension_resource(environment, "https://example.test/icons/icon_64.png",
                              source_url=source) is None
    assert extension_resource(None, f"chrome-extension://{X_PATH}/icons/icon_64.png") is None


def test_extension_paths_are_normalized_before_matching():
    environment = installed()
    root = f"chrome-extension://{X_PATH}"
    assert extension_resource(environment, root + "/icons/../icons/icon_64.png") == "icons/icon_64.png"
    assert extension_resource(environment, root + "/_locales/am/%2e%2e/%2e%2e/messages.json") is None
    assert extension_resource(environment, root + "/icons/%00icon_64.png") is None
    assert extension_resource(environment, root + "/icons/%5cicon_64.png") is None


def test_a_page_the_declaration_does_not_name_cannot_read_it():
    environment = installed(**{TRANSLATE: [{"resources": ["popup_css_compiled.css"],
                                           "matches": ["https://elsewhere.test/*"]}]})
    target = f"chrome-extension://{TRANSLATE}/popup_css_compiled.css"
    assert extension_resource(environment, target, source_url="https://elsewhere.test/app") ==\
        "popup_css_compiled.css"
    assert extension_resource(environment, target, source_url="https://example.test/") is None
    # Nothing named the source: a load with no page behind it still resolves.
    assert extension_resource(environment, target) == "popup_css_compiled.css"
