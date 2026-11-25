"""Download, build, and validate the V8 library used by PyV8."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


V8_GIT_URL = "https://chromium.googlesource.com/v8/v8.git"
DEPOT_TOOLS_GIT_URL = (
    "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
)
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
ICU_DATETIME_PATCH = Path(__file__).resolve().parent / "patches" / "icu-thread-datetime.patch"


class V8BuildError(RuntimeError):
    """Raised when the selected V8 checkout or build is unusable."""


def file_sha256(path: str | Path) -> str:
    """Return a streaming SHA-256 digest for a build artifact."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise V8BuildError(f"invalid boolean value: {value!r}")


def _version_tuple(version: str) -> tuple[int, int, int, int]:
    if not VERSION_PATTERN.fullmatch(version):
        raise V8BuildError(
            f"invalid V8 version {version!r}; expected a tag such as 12.5.99 or 13.1.201.22"
        )
    parts = [int(part) for part in version.split(".")]
    if len(parts) == 3:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


def read_default_v8_version(project_root: str | Path) -> str:
    version_file = Path(project_root) / "V8_VERSION"
    if not version_file.is_file():
        raise V8BuildError(f"missing default V8 version file: {version_file}")
    version = version_file.read_text(encoding="ascii").strip()
    _version_tuple(version)
    return version


def read_selected_v8_version(project_root: str | Path) -> str:
    root = Path(project_root)
    local_version_file = root / ".pyv8-version"
    if local_version_file.is_file():
        version = local_version_file.read_text(encoding="ascii").strip()
        _version_tuple(version)
        return version
    return read_default_v8_version(root)


def save_selected_v8_version(project_root: str | Path, version: str) -> Path:
    _version_tuple(version)
    version_file = Path(project_root) / ".pyv8-version"
    version_file.write_text(version + "\n", encoding="ascii")
    return version_file


def parse_v8_version_refs(output: str) -> list[str]:
    versions = set()
    prefix = "refs/tags/"
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith(prefix):
            continue
        version = fields[1][len(prefix):]
        if VERSION_PATTERN.fullmatch(version):
            versions.add(version)
    return sorted(versions, key=_version_tuple, reverse=True)


def _git_proxy() -> str | None:
    for key in ("https.proxy", "http.proxy"):
        try:
            result = subprocess.run(
                ["git", "config", "--get", key],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def _append_git_config(
    env: dict[str, str], entries: Sequence[tuple[str, str]]
) -> None:
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError as exc:
        raise V8BuildError("GIT_CONFIG_COUNT must be an integer") from exc
    for offset, (key, value) in enumerate(entries):
        index = count + offset
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + len(entries))


def _network_environment(
    *, proxy: str | None = None, no_proxy: bool | None = None
) -> dict[str, str]:
    env = dict(os.environ)
    disable_proxy = (
        no_proxy
        if no_proxy is not None
        else _read_bool(env.get("PYV8_NO_PROXY"), False)
    )
    if disable_proxy:
        proxy_keys = {
            "ALL_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "PYV8_PROXY",
        }
        for key in tuple(env):
            if key.upper() in proxy_keys:
                env.pop(key, None)
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
        _append_git_config(
            env,
            [("http.proxy", ""), ("https.proxy", "")],
        )
        return env

    explicit_proxy = proxy or env.get("PYV8_PROXY")
    if explicit_proxy:
        env["HTTP_PROXY"] = explicit_proxy
        env["HTTPS_PROXY"] = explicit_proxy
        env["http_proxy"] = explicit_proxy
        env["https_proxy"] = explicit_proxy
        _append_git_config(
            env,
            [("http.proxy", explicit_proxy), ("https.proxy", explicit_proxy)],
        )
    elif not any(
        env.get(key)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    ):
        git_proxy = _git_proxy()
        if git_proxy:
            # CIPD and other depot_tools downloads do not read Git configuration.
            env["HTTP_PROXY"] = git_proxy
            env["HTTPS_PROXY"] = git_proxy
            env["http_proxy"] = git_proxy
            env["https_proxy"] = git_proxy
    return env


def fetch_v8_versions(
    *, proxy: str | None = None, no_proxy: bool | None = None
) -> list[str]:
    command = ["git", "ls-remote", "--tags", "--refs", V8_GIT_URL]
    env = _network_environment(proxy=proxy, no_proxy=no_proxy)
    for attempt in range(3):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            break
        if attempt < 2:
            time.sleep(2 ** attempt)
    else:
        detail = result.stderr.strip() or f"git exited with {result.returncode}"
        raise V8BuildError(f"failed to query V8 release tags: {detail}")
    versions = parse_v8_version_refs(result.stdout)
    if not versions:
        raise V8BuildError("the V8 repository returned no numeric release tags")
    return versions


def recommended_v8_versions(
    versions: Sequence[str], default_version: str, limit: int = 12
) -> list[str]:
    if limit < 1:
        raise V8BuildError("version list limit must be at least 1")
    _version_tuple(default_version)

    release_lines: dict[tuple[int, int], list[str]] = {}
    for version in versions:
        parts = _version_tuple(version)
        release_lines.setdefault(parts[:2], []).append(version)

    latest_per_line = []
    for line_versions in release_lines.values():
        patched = [version for version in line_versions if len(version.split(".")) == 4]
        candidates = patched or line_versions
        latest_per_line.append(max(candidates, key=_version_tuple))
    latest_per_line.sort(key=_version_tuple, reverse=True)

    result = [default_version]
    for version in latest_per_line:
        if version not in result:
            result.append(version)
        if len(result) >= limit:
            break
    return result


def print_v8_version_choices(
    versions: Sequence[str],
    default_version: str,
    limit: int = 12,
    output: Callable[[str], None] = print,
) -> list[str]:
    choices = recommended_v8_versions(versions, default_version, limit)
    output("Available V8 release choices:")
    for index, version in enumerate(choices, 1):
        status = " [default, tested]" if version == default_version else " [unverified]"
        output(f"  {index:2}. {version}{status}")
    output("Other numeric tags can be entered directly during interactive selection.")
    return choices


def select_v8_version(
    versions: Sequence[str],
    default_version: str,
    limit: int = 12,
    input_func: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> str:
    choices = print_v8_version_choices(versions, default_version, limit, output)
    available = set(versions)
    while True:
        answer = input_func(
            f"Select V8 version [1-{len(choices)}] or enter an exact tag (default 1): "
        ).strip()
        if not answer:
            return choices[0]
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(choices):
                return choices[index - 1]
        elif VERSION_PATTERN.fullmatch(answer) and answer in available:
            return answer
        output("Invalid selection or unknown V8 tag; please try again.")


def read_v8_header_version(v8_home: Path) -> str | None:
    version_header = v8_home / "include" / "v8-version.h"
    if not version_header.is_file():
        return None

    values: dict[str, int] = {}
    pattern = re.compile(
        r"^#define\s+(V8_MAJOR_VERSION|V8_MINOR_VERSION|V8_BUILD_NUMBER|V8_PATCH_LEVEL)\s+(\d+)"
    )
    for line in version_header.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            values[match.group(1)] = int(match.group(2))

    required = (
        "V8_MAJOR_VERSION",
        "V8_MINOR_VERSION",
        "V8_BUILD_NUMBER",
        "V8_PATCH_LEVEL",
    )
    if any(name not in values for name in required):
        raise V8BuildError(f"cannot parse V8 version header: {version_header}")

    parts = [values[name] for name in required]
    if parts[-1] == 0:
        parts.pop()
    return ".".join(str(part) for part in parts)


def _target_cpu(machine: str) -> str:
    normalized = machine.strip().lower()
    if normalized in {"amd64", "x86_64", "x64"}:
        return "x64"
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    raise V8BuildError(f"unsupported V8 target architecture: {machine}")


def _platform_folder() -> str:
    if os.name == "nt":
        return "win"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "mac"
    raise V8BuildError(f"unsupported platform: {sys.platform}")


def cppgc_microtask_queue_enabled(output_dir: str | Path) -> bool:
    """Whether the V8 library in *output_dir* was built with cppgc microtask queues.

    ``v8::MicrotaskQueue``'s class layout is conditional on the
    ``V8_CPPGC_MICROTASK_QUEUE`` macro, which V8 passes on its own compile
    command line and ships in no public header.  An embedder built without it
    disagrees with the library about that class and faults on every virtual call
    into a queue -- ``EnqueueMicrotask`` and ``PerformCheckpoint`` alike.  The
    flag is therefore read back from the generated build files, so the extension
    always matches the library it links, whichever way the gn flag
    ``v8_cppgc_microtask_queue`` defaults in the selected V8 version.
    """
    override_value = os.environ.get("PYV8_CPPGC_MICROTASK_QUEUE")
    override = _read_bool(override_value) if override_value is not None else None
    marker = re.compile(r"(?:-D|/D)V8_CPPGC_MICROTASK_QUEUE(?:=[^\s]+)?(?:\s|$)")
    obj_dir = Path(output_dir) / "obj"
    found = None
    for name in ("v8_base_without_compiler.ninja", "v8_base.ninja"):
        path = obj_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text.replace("$\r\n", "").replace("$\n", "")
        definitions = [line for line in text.splitlines() if line.lstrip().startswith("defines =")]
        if not definitions or not any(re.search(r"(?:-D|/D)V8_", line) for line in definitions):
            continue
        value = any(marker.search(line) for line in definitions)
        if found is not None and found != value:
            raise V8BuildError("conflicting V8 microtask queue ABI in build metadata")
        found = value
    if found is None:
        if override is None:
            raise V8BuildError(
                f"cannot determine V8 microtask queue ABI from {obj_dir}; "
                "set PYV8_CPPGC_MICROTASK_QUEUE=1 or 0 to match the V8 build"
            )
        return override
    if override is not None and override != found:
        raise V8BuildError("PYV8_CPPGC_MICROTASK_QUEUE conflicts with V8 build metadata")
    return found


@dataclass
class V8BuildConfig:
    project_root: Path
    version: str
    v8_home: Path
    output_dir: Path
    depot_tools_home: Path
    build_root: Path
    jobs: int
    download_and_build: bool
    force_build: bool
    target_cpu: str
    extra_gn_args: str
    icu_data_override: Path | None
    bundled_v8_home: Path
    network_proxy: str | None
    disable_proxy: bool | None

    @classmethod
    def from_environment(
        cls,
        project_root: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        *,
        version: str | None = None,
        v8_home: str | Path | None = None,
        output_dir: str | Path | None = None,
        jobs: int | str | None = None,
        download_and_build: bool | None = None,
        force_build: bool | None = None,
        proxy: str | None = None,
        no_proxy: bool | None = None,
    ) -> "V8BuildConfig":
        root = Path(project_root or Path(__file__).resolve().parent).resolve()
        env = dict(os.environ if environ is None else environ)

        selected_version = read_selected_v8_version(root)
        requested_version = version or env.get("PYV8_V8_VERSION") or selected_version
        _version_tuple(requested_version)

        bundled_home = (root / "v8").resolve()
        build_root = Path(env.get("PYV8_BUILD_ROOT", root / ".pyv8-build")).resolve()
        depot_home = Path(
            env.get("PYV8_DEPOT_TOOLS", build_root / "depot_tools")
        ).resolve()

        should_build = (
            download_and_build
            if download_and_build is not None
            else _read_bool(env.get("PYV8_BUILD_V8"), False)
        )
        should_force = (
            force_build
            if force_build is not None
            else _read_bool(env.get("PYV8_FORCE_V8_BUILD"), False)
        )

        if proxy is not None and no_proxy:
            raise V8BuildError("proxy and no_proxy cannot be enabled together")
        if no_proxy:
            selected_proxy = None
            selected_disable_proxy = True
        elif proxy is not None:
            selected_proxy = proxy
            selected_disable_proxy = False
        else:
            selected_disable_proxy = (
                _read_bool(env.get("PYV8_NO_PROXY"), False)
                if "PYV8_NO_PROXY" in env
                else None
            )
            selected_proxy = (
                None if selected_disable_proxy else env.get("PYV8_PROXY")
            )
            if selected_proxy and selected_disable_proxy is None:
                selected_disable_proxy = False

        explicit_home = v8_home or env.get("PYV8_V8_HOME")
        bundled_version = read_v8_header_version(bundled_home)
        version_is_bundled = (
            bundled_version is not None
            and _version_tuple(bundled_version) == _version_tuple(requested_version)
        )
        if explicit_home:
            selected_home = Path(explicit_home).resolve()
        elif should_build or not version_is_bundled:
            selected_home = (
                build_root / "versions" / requested_version / "v8"
            ).resolve()
        else:
            selected_home = bundled_home

        selected_cpu = env.get("PYV8_TARGET_CPU") or _target_cpu(platform.machine())
        if selected_cpu not in {"x64", "arm64"}:
            raise V8BuildError(f"unsupported V8 target CPU: {selected_cpu}")

        explicit_output = output_dir or env.get("PYV8_V8_OUT")
        if explicit_output:
            selected_output = Path(explicit_output).resolve()
        elif selected_home == bundled_home:
            selected_output = (
                selected_home
                / "out"
                / _platform_folder()
                / f"{selected_cpu}.release"
            )
        else:
            selected_output = selected_home / "out" / f"pyv8-{selected_cpu}.release"

        selected_jobs = int(jobs or env.get("PYV8_V8_JOBS") or os.cpu_count() or 1)
        if selected_jobs < 1:
            raise V8BuildError("PYV8_V8_JOBS must be at least 1")

        icu_override = env.get("PYV8_ICU_DATA")
        return cls(
            project_root=root,
            version=requested_version,
            v8_home=selected_home,
            output_dir=selected_output,
            depot_tools_home=depot_home,
            build_root=build_root,
            jobs=selected_jobs,
            download_and_build=should_build,
            force_build=should_force,
            target_cpu=selected_cpu,
            extra_gn_args=env.get("PYV8_GN_ARGS", "").strip(),
            icu_data_override=Path(icu_override).resolve() if icu_override else None,
            bundled_v8_home=bundled_home,
            network_proxy=selected_proxy,
            disable_proxy=selected_disable_proxy,
        )

    @property
    def include_dir(self) -> Path:
        return self.v8_home / "include"

    @property
    def library_dir(self) -> Path:
        return self.output_dir / "obj"

    @property
    def library_file(self) -> Path:
        name = "v8_monolith.lib" if os.name == "nt" else "libv8_monolith.a"
        return self.library_dir / name

    @property
    def stamp_file(self) -> Path:
        return self.output_dir / "pyv8-build.json"

    @property
    def gn_args(self) -> str:
        values = {
            "is_component_build": "false",
            "is_debug": "false",
            "symbol_level": "0",
            "target_cpu": f'"{self.target_cpu}"',
            "treat_warnings_as_errors": "false",
            "use_custom_libcxx": "false",
            "v8_enable_i18n_support": "true",
            "v8_enable_pointer_compression": "false",
            "v8_enable_sandbox": "false",
            #: Chrome 154's V8 ships Temporal; the 15.2 tag implements it behind
            #: this build switch, so the library must be built with it on.
            "v8_enable_temporal_support": "true",
            "v8_monolithic": "true",
            "v8_use_external_startup_data": "false",
        }
        if sys.platform.startswith("linux"):
            # The static V8 archive is linked into the Python shared extension.
            values["v8_monolithic_for_shared_library"] = "true"
            # Chromium's bundled sysroot may ship a C++ library older than V8
            # requires; use the host toolchain consistently with the extension.
            values["use_sysroot"] = "false"
        result = " ".join(f"{key}={value}" for key, value in values.items())
        if self.extra_gn_args:
            result += f" {self.extra_gn_args}"
        return result

    def icu_data_file(self) -> Path | None:
        candidates = []
        if self.icu_data_override is not None:
            candidates.append(self.icu_data_override)
        candidates.extend(
            [
                self.output_dir / "icudtl.dat",
                self.v8_home / "icudtl.dat",
            ]
        )
        if self.v8_home == self.bundled_v8_home:
            candidates.append(self.project_root.parent / "pyv8c" / "pyv8c" / "pyv8" / "icudtl.dat")
        return next((path for path in candidates if path.is_file()), None)

    def validate(self) -> None:
        header = self.include_dir / "v8.h"
        if not header.is_file():
            raise V8BuildError(
                f"V8 headers were not found in {self.include_dir}; "
                "set PYV8_BUILD_V8=1 to download and build the selected version"
            )

        actual_version = read_v8_header_version(self.v8_home)
        if actual_version is None or _version_tuple(actual_version) != _version_tuple(
            self.version
        ):
            raise V8BuildError(
                f"selected V8 version is {self.version}, but {self.v8_home} contains "
                f"{actual_version or 'an unknown version'}"
            )

        if not self.library_file.is_file():
            raise V8BuildError(
                f"V8 monolith library was not found: {self.library_file}; "
                "set PYV8_BUILD_V8=1 to build it"
            )

    def _stamp_data(self) -> dict[str, object]:
        return {
            "version": self.version,
            "target_cpu": self.target_cpu,
            "gn_args": self.gn_args,
            "icu_datetime_patch_sha256": file_sha256(ICU_DATETIME_PATCH),
        }

    def _can_reuse_build(self) -> bool:
        if self.force_build or not self.stamp_file.is_file():
            return False
        try:
            stamp = json.loads(self.stamp_file.read_text(encoding="utf-8"))
            if stamp != self._stamp_data():
                return False
            self.validate()
            return self.icu_data_file() is not None
        except (OSError, ValueError, V8BuildError):
            return False

    def prepare(self) -> None:
        if not self.download_and_build:
            self.validate()
            return
        if self._can_reuse_build():
            print(f"Reusing V8 {self.version} from {self.v8_home}")
            return

        self._ensure_depot_tools()
        self._bootstrap_depot_tools()
        self._checkout_v8()
        self._build_v8()
        self.validate()
        if self.icu_data_file() is None:
            raise V8BuildError(
                f"V8 {self.version} was built without icudtl.dat in {self.output_dir}"
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stamp_file.write_text(
            json.dumps(self._stamp_data(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _command_environment(self) -> dict[str, str]:
        env = _network_environment(
            proxy=self.network_proxy,
            no_proxy=self.disable_proxy,
        )
        path_entries = [str(self.depot_tools_home)]
        if os.name == "nt":
            git_cmd_dir = self._windows_git_cmd_dir()
            if git_cmd_dir is not None:
                # depot_tools bootstrap does not recognize Scoop's shim directory;
                # expose the underlying Git for Windows installation as well.
                path_entries.append(str(git_cmd_dir))
        path_entries.append(env.get("PATH", ""))
        env["PATH"] = os.pathsep.join(path_entries)
        if os.name == "nt":
            env.setdefault("DEPOT_TOOLS_WIN_TOOLCHAIN", "0")
            self._configure_visual_studio_environment(env)
            self._configure_windows_sdk_environment(env)
            # The checkout was cloned explicitly; avoid an extra Git update before
            # every depot_tools command. CIPD is bootstrapped separately below.
            env.setdefault("DEPOT_TOOLS_UPDATE", "0")
        return env

    @staticmethod
    def _configure_visual_studio_environment(env: dict[str, str]) -> None:
        supported_years = ("2026", "2022", "2019", "2017")
        if any(env.get(f"vs{year}_install") for year in supported_years):
            return

        explicit_home = env.get("PYV8_VS_HOME")
        if explicit_home:
            year = env.get("PYV8_VS_VERSION", "2022")
            if year not in supported_years:
                raise V8BuildError(
                    f"unsupported PYV8_VS_VERSION {year!r}; expected one of {supported_years}"
                )
            installation = Path(explicit_home).resolve()
            if not installation.is_dir():
                raise V8BuildError(f"PYV8_VS_HOME does not exist: {installation}")
            env[f"vs{year}_install"] = str(installation)
            env.setdefault("GYP_MSVS_OVERRIDE_PATH", str(installation))
            return

        vswhere = shutil.which("vswhere", path=env.get("PATH"))
        if not vswhere:
            program_files_x86 = env.get("ProgramFiles(x86)") or env.get(
                "PROGRAMFILES(X86)"
            )
            if program_files_x86:
                candidate = (
                    Path(program_files_x86)
                    / "Microsoft Visual Studio"
                    / "Installer"
                    / "vswhere.exe"
                )
                if candidate.is_file():
                    vswhere = str(candidate)
        if not vswhere:
            return

        result = subprocess.run(
            [
                vswhere,
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-format",
                "json",
                "-utf8",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return
        try:
            installations = json.loads(result.stdout.lstrip("\ufeff"))
        except (TypeError, ValueError):
            return
        if not installations:
            return

        installation = installations[0]
        major_version = str(installation.get("installationVersion", "")).split(".", 1)[0]
        year = {"18": "2026", "17": "2022", "16": "2019", "15": "2017"}.get(
            major_version
        )
        installation_path_value = installation.get("installationPath")
        installation_path = Path(installation_path_value) if installation_path_value else None
        if year and installation_path is not None and installation_path.is_dir():
            env[f"vs{year}_install"] = str(installation_path)
            env.setdefault("GYP_MSVS_OVERRIDE_PATH", str(installation_path))

    @staticmethod
    def _configure_windows_sdk_environment(env: dict[str, str]) -> None:
        existing_sdk = env.get("WINDOWSSDKDIR") or env.get("WindowsSdkDir")
        if existing_sdk:
            env["WINDOWSSDKDIR"] = existing_sdk
            return

        explicit_sdk = env.get("PYV8_WINDOWS_SDK")
        if explicit_sdk:
            sdk_path = Path(explicit_sdk).resolve()
            if not sdk_path.is_dir():
                raise V8BuildError(f"PYV8_WINDOWS_SDK does not exist: {sdk_path}")
            env["WINDOWSSDKDIR"] = str(sdk_path)
            return

        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
            ) as key:
                sdk_value, _ = winreg.QueryValueEx(key, "KitsRoot10")
        except (ImportError, OSError):
            sdk_value = None

        if sdk_value:
            sdk_path = Path(sdk_value)
            if sdk_path.is_dir():
                env["WINDOWSSDKDIR"] = str(sdk_path)

    @staticmethod
    def _windows_git_cmd_dir() -> Path | None:
        executable = shutil.which("git")
        search_roots = []
        if executable:
            search_roots.append(Path(executable).resolve().parent)
        try:
            result = subprocess.run(
                ["git", "--exec-path"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            search_roots.append(Path(result.stdout.strip()).resolve())

        for search_root in search_roots:
            for candidate in (search_root, *search_root.parents):
                git_cmd_dir = candidate / "cmd"
                if (git_cmd_dir / "git.exe").is_file():
                    return git_cmd_dir
        return None

    def _windows_bootstrap_git_cmd_dir(self, git_cmd_dir: Path) -> Path:
        git_root = git_cmd_dir.parent
        if git_root.name.lower() == "git":
            return git_cmd_dir

        # depot_tools only recognizes Git for Windows when an ancestor directory
        # is literally named "Git". Scoop installs it under git/<version>, so a
        # cache-local junction presents the standard layout without copying Git.
        junction = self.build_root / "bootstrap-tools" / "Git"
        junction.parent.mkdir(parents=True, exist_ok=True)
        if junction.exists():
            try:
                if os.path.samefile(junction, git_root):
                    return junction / "cmd"
            except OSError:
                pass
            raise V8BuildError(
                f"depot_tools Git bootstrap path points elsewhere: {junction}"
            )

        command_line = subprocess.list2cmdline(
            ["mklink", "/J", str(junction), str(git_root)]
        )
        self._run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/s",
                "/c",
                command_line,
            ],
            cwd=junction.parent,
        )
        if not (junction / "cmd" / "git.exe").is_file():
            raise V8BuildError(
                f"failed to expose Git for Windows to depot_tools: {junction}"
            )
        return junction / "cmd"

    def _run(
        self,
        command: Sequence[str | Path],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        retries: int = 0,
    ) -> None:
        args = [str(value) for value in command]
        command_line = subprocess.list2cmdline(args)
        for attempt in range(retries + 1):
            print(f"[{cwd}] {command_line}")
            result = subprocess.run(args, cwd=cwd, env=env, check=False)
            if result.returncode == 0:
                return
            if attempt < retries:
                delay = 2 ** attempt
                print(
                    f"Command failed with exit code {result.returncode}; "
                    f"retrying in {delay}s ({attempt + 1}/{retries})"
                )
                time.sleep(delay)
        raise V8BuildError(
            f"command failed with exit code {result.returncode}: {command_line}"
        )

    def _depot_command(self, name: str, *arguments: str) -> list[str]:
        suffixes = (".bat", ".cmd", ".exe", ".py", "") if os.name == "nt" else ("", ".py")
        tool = next(
            (
                self.depot_tools_home / f"{name}{suffix}"
                for suffix in suffixes
                if (self.depot_tools_home / f"{name}{suffix}").is_file()
            ),
            None,
        )
        if tool is None:
            raise V8BuildError(f"depot_tools command was not found: {name}")
        if tool.suffix.lower() == ".py":
            return [sys.executable, str(tool), *arguments]
        if os.name == "nt" and tool.suffix.lower() in {".bat", ".cmd"}:
            return self._windows_batch_command(tool, *arguments)
        return [str(tool), *arguments]

    def _depot_python_command(self, name: str, *arguments: str) -> list[str]:
        script = self.depot_tools_home / f"{name}.py"
        if not script.is_file():
            raise V8BuildError(f"depot_tools Python command was not found: {name}")
        # Current depot_tools scripts may require a newer Python than the
        # interpreter used to build the extension (for example StrEnum in
        # autoninja). Prefer the hermetic vpython supplied by depot_tools on
        # Windows, while retaining the normal interpreter fallback for tests,
        # source checkouts without a bootstrap, and non-Windows platforms.
        if os.name == "nt":
            vpython = self.depot_tools_home / ".cipd_bin" / "vpython3.exe"
            if vpython.is_file():
                return [str(vpython), str(script), *arguments]
        return [sys.executable, str(script), *arguments]

    @staticmethod
    def _windows_batch_command(script: Path, *arguments: str) -> list[str]:
        command_line = subprocess.list2cmdline([str(script), *arguments])
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            command_line,
        ]

    def _ensure_depot_tools(self) -> None:
        if (self.depot_tools_home / ".git").is_dir():
            return
        if self.depot_tools_home.exists():
            raise V8BuildError(
                f"depot_tools path exists but is not a Git checkout: {self.depot_tools_home}"
            )
        self.depot_tools_home.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                DEPOT_TOOLS_GIT_URL,
                self.depot_tools_home,
            ],
            cwd=self.depot_tools_home.parent,
            env=self._command_environment(),
        )

    def _bootstrap_depot_tools(self) -> None:
        if os.name != "nt":
            return
        env = self._command_environment()
        git_wrapper = self.depot_tools_home / "git.bat"
        if not git_wrapper.is_file():
            git_cmd_dir = self._windows_git_cmd_dir()
            if git_cmd_dir is None:
                raise V8BuildError(
                    "Git for Windows was not found; install it or add its cmd directory to PATH"
                )
            bootstrap_git_cmd_dir = self._windows_bootstrap_git_cmd_dir(git_cmd_dir)
            env["PATH"] = str(bootstrap_git_cmd_dir) + os.pathsep + env["PATH"]
            win_tools = self.depot_tools_home / "bootstrap" / "win_tools.bat"
            if not win_tools.is_file():
                raise V8BuildError(f"depot_tools bootstrap was not found: {win_tools}")
            self._run(
                self._windows_batch_command(win_tools),
                cwd=self.depot_tools_home,
                env=env,
                retries=2,
            )
            if not git_wrapper.is_file():
                raise V8BuildError(
                    "depot_tools bootstrap did not create git.bat; install Git for Windows "
                    "or set PATH to its cmd directory"
                )

        bootstrap_marker = self.depot_tools_home / ".cipd_bin" / ".cipd_manifest.txt"
        if bootstrap_marker.is_file():
            return
        self._run(
            self._depot_command("cipd_bin_setup"),
            cwd=self.depot_tools_home,
            env=env,
            retries=2,
        )

    def _ensure_gclient_config(self, workspace: Path) -> Path:
        gclient_file = workspace / ".gclient"
        if gclient_file.is_file():
            content = gclient_file.read_text(encoding="utf-8")
            if '"name": "v8"' not in content or V8_GIT_URL not in content:
                raise V8BuildError(
                    f"existing gclient workspace does not describe the V8 checkout: {gclient_file}"
                )
            return gclient_file

        gclient_file.write_text(
            "solutions = [\n"
            "  {\n"
            '    "name": "v8",\n'
            f'    "url": "{V8_GIT_URL}",\n'
            '    "deps_file": "DEPS",\n'
            '    "custom_deps": {},\n'
            "  },\n"
            "]\n",
            encoding="utf-8",
        )
        return gclient_file

    def _initialize_v8_checkout(self, workspace: Path) -> None:
        if self.v8_home.name != "v8":
            raise V8BuildError(
                "an automatically managed PYV8_V8_HOME must end with a 'v8' directory"
            )
        self._ensure_gclient_config(workspace)
        if self.v8_home.exists():
            if any(self.v8_home.iterdir()):
                raise V8BuildError(
                    f"V8 checkout path exists but is not a Git repository: {self.v8_home}"
                )
        else:
            self.v8_home.mkdir()
        self._run(
            ["git", "init"],
            cwd=self.v8_home,
        )
        self._run(
            ["git", "remote", "add", "origin", V8_GIT_URL],
            cwd=self.v8_home,
        )

    def _gclient_sync_command(self) -> list[str]:
        selected_tag = f"refs/tags/{self.version}"
        return self._depot_command(
            "gclient",
            "sync",
            "-D",
            "--no-history",
            "--revision",
            f"v8@{selected_tag}",
            f"-j{self.jobs}",
        )

    def _configure_v8_fetch_refspec(self) -> None:
        selected_tag = f"refs/tags/{self.version}"
        self._run(
            [
                "git",
                "config",
                "--replace-all",
                "remote.origin.fetch",
                f"+{selected_tag}:{selected_tag}",
            ],
            cwd=self.v8_home,
        )

    def _checkout_v8(self) -> None:
        workspace = self.v8_home.parent
        workspace.mkdir(parents=True, exist_ok=True)
        env = self._command_environment()

        if not (self.v8_home / ".git").is_dir():
            if not self.v8_home.exists() or not any(self.v8_home.iterdir()):
                self._initialize_v8_checkout(workspace)
            else:
                raise V8BuildError(
                    f"cannot switch versions because V8_HOME is not a Git checkout: {self.v8_home}"
                )

        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=self.v8_home,
            check=False,
            capture_output=True,
            text=True,
        )
        if origin.returncode != 0:
            self._run(
                ["git", "remote", "add", "origin", V8_GIT_URL],
                cwd=self.v8_home,
            )
        elif origin.stdout.strip().rstrip("/") != V8_GIT_URL.rstrip("/"):
            raise V8BuildError(
                f"V8 checkout origin is {origin.stdout.strip()!r}, expected {V8_GIT_URL!r}"
            )
        self._configure_v8_fetch_refspec()

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.v8_home,
            check=False,
            capture_output=True,
            text=True,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise V8BuildError(
                f"V8 checkout must be clean before selecting {self.version}: {self.v8_home}"
            )

        selected_tag = f"refs/tags/{self.version}"
        tag_check = subprocess.run(
            ["git", "rev-parse", "--verify", selected_tag],
            cwd=self.v8_home,
            check=False,
            capture_output=True,
            text=True,
        )
        if tag_check.returncode != 0:
            self._run(
                [
                    "git",
                    "fetch",
                    "--depth",
                    "1",
                    "origin",
                    f"{selected_tag}:{selected_tag}",
                ],
                cwd=self.v8_home,
                env=env,
                retries=2,
            )
        self._run(
            ["git", "checkout", "--detach", selected_tag],
            cwd=self.v8_home,
        )
        self._run(
            self._gclient_sync_command(),
            cwd=workspace,
            env=env,
            retries=2,
        )

    def _build_v8(self) -> None:
        env = self._command_environment()
        try:
            output_relative = self.output_dir.relative_to(self.v8_home)
        except ValueError as exc:
            raise V8BuildError(
                f"V8 output directory must be inside the checkout when building: {self.output_dir}"
            ) from exc
        with self._icu_datetime_patch(), self._source_compatibility_patches():
            self._run(
                self._depot_python_command(
                    "gn", "gen", str(output_relative), f"--args={self.gn_args}"
                ),
                cwd=self.v8_home,
                env=env,
            )
            self._run(
                self._depot_python_command(
                    "autoninja",
                    "-C",
                    str(output_relative),
                    "-j",
                    str(self.jobs),
                    "v8_monolith",
                ),
                cwd=self.v8_home,
                env=env,
            )

    @contextmanager
    def _icu_datetime_patch(self):
        patch = ["git", "apply", "--whitespace=nowarn"]
        self._run([*patch, "--check", ICU_DATETIME_PATCH], cwd=self.v8_home)
        paths = (
            self.v8_home / "third_party/icu/source/i18n/timezone.cpp",
            self.v8_home / "third_party/icu/source/common/locid.cpp",
        )
        originals = {path: path.read_bytes() for path in paths}
        try:
            self._run([*patch, ICU_DATETIME_PATCH], cwd=self.v8_home)
            yield
        finally:
            for path, original in originals.items():
                path.write_bytes(original)

    @contextmanager
    def _source_compatibility_patches(self):
        originals: dict[Path, bytes] = {}
        try:
            sdk_version = os.environ.get("PYV8_WINDOWS_SDK_VERSION")
            if sdk_version:
                if not re.fullmatch(r"10\.0\.\d+\.\d+", sdk_version):
                    raise V8BuildError(f"invalid Windows SDK version: {sdk_version!r}")
                sdk_root = Path(os.environ.get("PYV8_WINDOWS_SDK") or os.environ.get("WINDOWSSDKDIR") or "")
                required = [sdk_root / "Include" / sdk_version / part for part in ("um", "shared", "ucrt")]
                required += [sdk_root / "Lib" / sdk_version / part / "x64" for part in ("um", "ucrt")]
                if not all(path.is_dir() for path in required):
                    raise V8BuildError(f"Windows SDK {sdk_version} is incomplete under {sdk_root}")
                setup_toolchain = self.v8_home / "build" / "toolchain" / "win" / "setup_toolchain.py"
                original = setup_toolchain.read_bytes()
                updated, count = re.subn(rb"(?m)^SDK_VERSION = ['\"]10\.0\.\d+\.\d+['\"]", f"SDK_VERSION = '{sdk_version}'".encode("ascii"), original)
                if count != 1:
                    raise V8BuildError("could not locate the Windows SDK version in setup_toolchain.py")
                if updated != original:
                    originals[setup_toolchain] = original
                    setup_toolchain.write_bytes(updated)
                    print(f"Using Windows SDK {sdk_version} for V8")
                if _version_tuple(sdk_version) < (10, 0, 26100, 0):
                    raise V8BuildError("Windows SDK 10.0.26100.0 or newer is required")
                if _version_tuple(sdk_version) < (10, 0, 28000, 0):
                    build_gn = self.v8_home / "build" / "config" / "win" / "BUILD.gn"
                    original_gn = build_gn.read_bytes()
                    old_macro = b"NTDDI_VERSION=NTDDI_WIN11_BR"
                    new_macro = b"NTDDI_VERSION=NTDDI_WIN11_GE"
                    if old_macro in original_gn:
                        originals[build_gn] = original_gn
                        build_gn.write_bytes(original_gn.replace(old_macro, new_macro))
                        print("Using Windows 11 GE target for the selected SDK")
            if sys.platform.startswith("linux") and self.target_cpu == "x64":
                mutable_page = self.v8_home / "src" / "heap" / "mutable-page.cc"
                if mutable_page.is_file():
                    original = mutable_page.read_bytes()
                    old = b"std::hardware_destructive_interference_size"
                    # Older libstdc++ headers omit this constant under Clang.
                    # It is used only in a cache-line placement assertion here.
                    if old in original:
                        originals[mutable_page] = original
                        mutable_page.write_bytes(original.replace(old, b"64"))
            backing_store = self.v8_home / "src" / "objects" / "backing-store.cc"
            if os.name == "nt" and backing_store.is_file():
                original = backing_store.read_bytes()
                direct_function_ref = (
                    b"return isolate->heap()->allocator()->RetryCustomAllocate(\n"
                    b"        fn, internal::AllocationType::kOld,\n"
                )
                wrapped_function_ref = (
                    b"return isolate->heap()->allocator()->RetryCustomAllocate(\n"
                    b"        [&fn]() { return fn(); }, internal::AllocationType::kOld,\n"
                )
                if direct_function_ref in original:
                    originals[backing_store] = original
                    backing_store.write_bytes(
                        original.replace(direct_function_ref, wrapped_function_ref, 1)
                    )
                    print(
                        "Applying V8 std::function/FunctionRef compatibility patch "
                        f"for {self.version}"
                    )

            atomics_header = (
                self.v8_home / "src" / "objects" / "js-atomics-synchronization.h"
            )
            if os.name == "nt" and atomics_header.is_file():
                original = atomics_header.read_bytes()
                implicit_tail_padding = (
                    b"  ExternalPointerMember<kWaiterQueueNodeTag> waiter_queue_head_;\n"
                    b"  std::atomic<uint32_t> state_;\n"
                )
                explicit_tail_padding = (
                    b"  ExternalPointerMember<kWaiterQueueNodeTag> waiter_queue_head_;\n"
                    b"  std::atomic<uint32_t> state_;\n"
                    b"#if !V8_COMPRESS_POINTERS && TAGGED_SIZE_8_BYTES\n"
                    b"  uint32_t no_pointer_compression_padding_;\n"
                    b"#endif\n"
                )
                if (implicit_tail_padding in original
                        and b"no_pointer_compression_padding_" not in original):
                    originals[atomics_header] = original
                    atomics_header.write_bytes(
                        original.replace(
                            implicit_tail_padding, explicit_tail_padding, 1
                        )
                    )
                    print(
                        "Applying V8 no-pointer-compression object layout patch "
                        f"for {self.version}"
                    )

            extended_map_replacements = {
                self.v8_home / "src" / "objects" / "map.h": (
                    b"  std::atomic<uint8_t> bit_field_ex_;\n"
                    b"  // Leaves kTaggedSize-1 unused bytes, they will be used by subclasses.\n",
                    b"  std::atomic<uint8_t> bit_field_ex_;\n"
                    b"  uint8_t extended_map_padding_[3];\n"
                    b"  // Leaves the remaining bytes up to kTaggedSize for subclasses.\n",
                ),
                self.v8_home / "src" / "objects" / "map.tq": (
                    b"  bit_field_ex: ExtendedMapBitFields;\n"
                    b"  // Leaves kTaggedSize-1 unused bytes, they will be used by subclasses.\n",
                    b"  bit_field_ex: ExtendedMapBitFields;\n"
                    b"  extended_map_padding[3]: uint8;\n"
                    b"  // Leaves the remaining bytes up to kTaggedSize for subclasses.\n",
                ),
                self.v8_home / "src" / "objects" / "js-interceptor-map.h": (
                    b"  uint8_t extended_padding_[kTaggedSize - 1];\n",
                    b"#if TAGGED_SIZE_8_BYTES\n"
                    b"  uint8_t extended_padding_[kTaggedSize - 4];\n"
                    b"#endif\n",
                ),
                self.v8_home / "src" / "objects" / "js-interceptor-map.tq": (
                    b"  @ifnot(TAGGED_SIZE_8_BYTES) extended_padding[3]: uint8;\n"
                    b"  @if(TAGGED_SIZE_8_BYTES) extended_padding[7]: uint8;\n",
                    b"  @if(TAGGED_SIZE_8_BYTES) extended_padding[4]: uint8;\n",
                ),
                self.v8_home / "src" / "objects" / "js-interceptor-map-inl.h": (
                    b"void JSInterceptorMap::clear_extended_padding() {\n"
                    b"  memset(extended_padding_, 0, sizeof(extended_padding_));\n"
                    b"}\n",
                    b"void JSInterceptorMap::clear_extended_padding() {\n"
                    b"  memset(extended_map_padding_, 0, sizeof(extended_map_padding_));\n"
                    b"#if TAGGED_SIZE_8_BYTES\n"
                    b"  memset(extended_padding_, 0, sizeof(extended_padding_));\n"
                    b"#endif\n"
                    b"}\n",
                ),
            }
            # Newer V8 revisions add a flags byte to JSInterceptorMap; its
            # padding must shrink when the base gains explicit C++ tail padding.
            newer_interceptor = self.v8_home / "src" / "objects" / "js-interceptor-map.h"
            if newer_interceptor.is_file() and b"  uint8_t flags_;\n  uint8_t extended_padding_[kTaggedSize - 2];\n" in newer_interceptor.read_bytes():
                extended_map_replacements[newer_interceptor] = (
                    b"  uint8_t flags_;\n  uint8_t extended_padding_[kTaggedSize - 2];\n",
                    b"  uint8_t flags_;\n  uint8_t extended_padding_[3];\n",
                )
                extended_map_replacements[self.v8_home / "src" / "objects" / "js-interceptor-map.tq"] = (
                    b"  @ifnot(TAGGED_SIZE_8_BYTES) extended_padding[2]: uint8;\n"
                    b"  @if(TAGGED_SIZE_8_BYTES) extended_padding[6]: uint8;\n",
                    b"  extended_padding[3]: uint8;\n",
                )
                extended_map_replacements[self.v8_home / "src" / "objects" / "js-interceptor-map-inl.h"] = (
                    b"void JSInterceptorMap::init_flags_and_clear_extended_padding() {\n"
                    b"  flags_ = 0;\n  memset(extended_padding_, 0, sizeof(extended_padding_));\n}\n",
                    b"void JSInterceptorMap::init_flags_and_clear_extended_padding() {\n"
                    b"  flags_ = 0;\n  memset(extended_map_padding_, 0, sizeof(extended_map_padding_));\n"
                    b"  memset(extended_padding_, 0, sizeof(extended_padding_));\n}\n",
                )
            extended_map_sources = {
                path: path.read_bytes()
                for path in extended_map_replacements
                if path.is_file()
            }
            if os.name == "nt" and len(extended_map_sources) == len(extended_map_replacements) and all(
                old in extended_map_sources[path]
                for path, (old, _new) in extended_map_replacements.items()
            ):
                for path, (old, new) in extended_map_replacements.items():
                    original = extended_map_sources[path]
                    originals[path] = original
                    path.write_bytes(original.replace(old, new, 1))
                print(
                    "Applying V8 ExtendedMap object layout compatibility patch "
                    f"for {self.version}"
                )
            yield
        finally:
            for path, original in originals.items():
                path.write_bytes(original)
