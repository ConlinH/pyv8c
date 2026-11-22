"""Fetch Chromium and generate Blink V8 binding sources.

The resulting workspace is compatible with ``tools/extract_idl.py``.
Generated files are written below ``<root>/src/out/Cronet145``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence


CHROMIUM_GIT_URL = "https://chromium.googlesource.com/chromium/src.git"
DEPOT_TOOLS_GIT_URL = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = PROJECT_ROOT / ".chromium-build"
DEFAULT_WORKSPACE = DEFAULT_BUILD_ROOT / "chromium"
DEFAULT_DEPOT_TOOLS = DEFAULT_BUILD_ROOT / "depot_tools"


class ChromiumFetchError(RuntimeError):
    """Raised when a Chromium source checkout cannot be prepared."""


def read_default_chromium_version(project_root: str | Path = PROJECT_ROOT) -> str:
    version_file = Path(project_root) / "CHROMIUM_VERSION"
    if not version_file.is_file():
        raise ChromiumFetchError(f"missing default Chromium version file: {version_file}")
    version = version_file.read_text(encoding="ascii").strip()
    version_key(version)
    return version


def version_key(version: str) -> tuple[int, int, int, int]:
    if not VERSION_PATTERN.fullmatch(version):
        raise ChromiumFetchError(
            f"invalid Chromium version {version!r}; expected e.g. 140.0.7339.82"
        )
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def parse_chromium_version_refs(output: str) -> list[str]:
    """Extract numeric release tags from ``git ls-remote`` output."""

    prefix = "refs/tags/"
    versions: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith(prefix):
            continue
        candidate = fields[1][len(prefix) :]
        if VERSION_PATTERN.fullmatch(candidate):
            versions.add(candidate)
    return sorted(versions, key=version_key, reverse=True)


def recommended_versions(versions: Sequence[str], limit: int = 12) -> list[str]:
    """Return the newest tag for each Chromium milestone."""

    if limit < 1:
        raise ChromiumFetchError("version list limit must be at least 1")
    latest_by_milestone: dict[int, str] = {}
    for version in versions:
        version_key(version)
        milestone = int(version.split(".", 1)[0])
        current = latest_by_milestone.get(milestone)
        if current is None or version_key(version) > version_key(current):
            latest_by_milestone[milestone] = version
    return sorted(latest_by_milestone.values(), key=version_key, reverse=True)[:limit]


def _network_environment(proxy: str | None = None, no_proxy: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    if no_proxy:
        for key in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "all_proxy", "http_proxy", "https_proxy", "no_proxy"):
            env.pop(key, None)
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
        return env
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = proxy
    return env


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    retries: int = 0,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    last: subprocess.CompletedProcess[str] | None = None
    for attempt in range(retries + 1):
        try:
            if not capture_output:
                print(f"[{cwd or Path.cwd()}] {subprocess.list2cmdline(list(command))}")
            result = subprocess.run(
                list(command), cwd=cwd, env=env, check=False, text=True,
                capture_output=capture_output,
            )
        except OSError as exc:
            raise ChromiumFetchError(f"failed to start {command[0]!r}: {exc}") from exc
        last = result
        if result.returncode == 0:
            return result
        if attempt < retries:
            time.sleep(2**attempt)
    detail = ((last.stderr or last.stdout) or "").strip() if last and capture_output else ""
    if len(detail) > 2000:
        detail = detail[-2000:]
    raise ChromiumFetchError(
        f"command failed ({last.returncode if last else 'unknown'}): "
        f"{' '.join(command)}\n{detail}"
    )


def query_versions(proxy: str | None = None, no_proxy: bool = False) -> list[str]:
    result = _run(
        ["git", "ls-remote", "--tags", "--refs", CHROMIUM_GIT_URL],
        env=_network_environment(proxy, no_proxy), retries=2,
    )
    versions = parse_chromium_version_refs(result.stdout)
    if not versions:
        raise ChromiumFetchError("Chromium repository returned no numeric release tags")
    return versions


def print_version_choices(
    versions: Sequence[str],
    limit: int = 12,
    output: Callable[[str], None] = print,
) -> list[str]:
    choices = recommended_versions(versions, limit)
    output("Chromium release tags (latest tag per milestone):")
    for index, version in enumerate(choices, 1):
        output(f"  {index:2}. {version}")
    output("Enter an exact four-part version when selecting a tag.")
    return choices


def select_version(
    versions: Sequence[str],
    *,
    limit: int = 12,
    input_func: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> str:
    choices = print_version_choices(versions, limit, output)
    available = set(versions)
    while True:
        answer = input_func(
            f"Select Chromium version [1-{len(choices)}] or enter an exact tag (default 1): "
        ).strip()
        if not answer:
            return choices[0]
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        if VERSION_PATTERN.fullmatch(answer) and answer in available:
            return answer
        output("Invalid selection or unknown Chromium tag; try again.")


def _windows_batch_command(script: Path, *arguments: str) -> list[str]:
    command_line = subprocess.list2cmdline([str(script), *arguments])
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line]


def _windows_git_cmd_dir() -> Path | None:
    """Find Git for Windows' ``cmd`` directory, including Scoop installs."""

    executable = shutil.which("git")
    search_roots: list[Path] = []
    if executable:
        search_roots.append(Path(executable).resolve().parent)
    try:
        result = subprocess.run(
            ["git", "--exec-path"], check=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
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


def _windows_bootstrap_git_cmd_dir(depot_tools: Path, git_cmd_dir: Path) -> Path:
    """Expose nonstandard Git layouts under an ancestor named ``Git``."""

    depot_tools = depot_tools.resolve()
    git_cmd_dir = git_cmd_dir.resolve()
    git_root = git_cmd_dir.parent
    if git_root.name.lower() == "git":
        return git_cmd_dir
    junction = depot_tools.parent / "bootstrap-tools" / "Git"
    junction.parent.mkdir(parents=True, exist_ok=True)
    if junction.exists():
        try:
            if os.path.samefile(junction, git_root):
                return junction / "cmd"
        except OSError:
            pass
        raise ChromiumFetchError(
            f"bootstrap Git path points to a different installation: {junction}"
        )
    command_line = subprocess.list2cmdline(["mklink", "/J", str(junction), str(git_root)])
    _run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line],
        cwd=junction.parent, capture_output=False,
    )
    if not (junction / "cmd" / "git.exe").is_file():
        raise ChromiumFetchError(f"failed to expose Git for Windows at {junction}")
    return junction / "cmd"


def _short_gclient_root(root: Path) -> Path:
    """Use a short junction path for gclient's Windows tar extraction."""

    if os.name != "nt":
        return root
    root = root.resolve()
    junction = root.parent / "c"
    if junction.exists():
        try:
            if os.path.samefile(junction, root):
                return junction
        except OSError:
            pass
        raise ChromiumFetchError(
            f"short Chromium workspace path points to a different directory: {junction}"
        )
    command_line = subprocess.list2cmdline(["mklink", "/J", str(junction), str(root)])
    _run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line],
        cwd=junction.parent, capture_output=False,
    )
    if not junction.is_dir():
        raise ChromiumFetchError(f"failed to create short Chromium workspace path: {junction}")
    return junction


def configure_windows_git_environment(depot_tools: Path, env: dict[str, str]) -> None:
    if os.name != "nt":
        return
    git_cmd_dir = _windows_git_cmd_dir()
    if git_cmd_dir is None:
        raise ChromiumFetchError(
            "Git for Windows was not found; install it or add its cmd directory to PATH"
        )
    bootstrap_git_cmd_dir = _windows_bootstrap_git_cmd_dir(depot_tools, git_cmd_dir)
    env["PATH"] = str(bootstrap_git_cmd_dir) + os.pathsep + env.get("PATH", "")


def _depot_command(depot_tools: Path, name: str, *arguments: str) -> list[str]:
    suffixes = (".bat", ".cmd", ".exe", ".py", "") if os.name == "nt" else ("", ".py")
    tool = next(
        (depot_tools / f"{name}{suffix}" for suffix in suffixes if (depot_tools / f"{name}{suffix}").is_file()),
        None,
    )
    if tool is None:
        raise ChromiumFetchError(f"depot_tools command was not found: {name}")
    if tool.suffix.lower() == ".py":
        return [sys.executable, str(tool), *arguments]
    if os.name == "nt" and tool.suffix.lower() in {".bat", ".cmd"}:
        return _windows_batch_command(tool, *arguments)
    return [str(tool), *arguments]


def bootstrap_depot_tools(path: Path, env: dict[str, str]) -> None:
    """Run the lightweight Windows bootstrap required by gclient, if needed."""

    if os.name != "nt":
        return
    git_wrapper = path / "git.bat"
    if not git_wrapper.is_file():
        win_tools = path / "bootstrap" / "win_tools.bat"
        if win_tools.is_file():
            _run(_windows_batch_command(win_tools), cwd=path, env=env, retries=2, capture_output=False)
    marker = path / ".cipd_bin" / ".cipd_manifest.txt"
    if not marker.is_file():
        try:
            command = _depot_command(path, "cipd_bin_setup")
        except ChromiumFetchError:
            return
        _run(command, cwd=path, env=env, retries=2, capture_output=False)


def ensure_depot_tools(path: Path, env: dict[str, str]) -> None:
    if (path / ".git").is_dir():
        return
    if path.exists():
        raise ChromiumFetchError(
            f"depot_tools path exists but is not a Git checkout: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["git", "clone", "--depth", "1", DEPOT_TOOLS_GIT_URL, str(path)],
        cwd=path.parent, env=env, retries=2, capture_output=False,
    )


def _write_gclient(path: Path) -> None:
    config = path / ".gclient"
    if config.is_file():
        content = config.read_text(encoding="utf-8")
        has_src_solution = re.search(r"['\"]name['\"]\s*:\s*['\"]src['\"]", content)
        if not has_src_solution or CHROMIUM_GIT_URL not in content:
            raise ChromiumFetchError(
                f"existing .gclient does not describe the Chromium src checkout: {config}"
            )
        return
    config.write_text(
        "solutions = [\n"
        "  {\n"
        '    "name": "src",\n'
        f'    "url": "{CHROMIUM_GIT_URL}",\n'
        '    "deps_file": "DEPS",\n'
        '    "managed": False,\n'
        '    "custom_deps": {},\n'
        '    "custom_vars": {},\n'
        "  },\n"
        "]\n",
        encoding="utf-8",
    )


def _prepare_src_checkout(root: Path, env: dict[str, str]) -> Path:
    src = root / "src"
    root.mkdir(parents=True, exist_ok=True)
    if src.exists() and not (src / ".git").is_dir():
        if any(src.iterdir()):
            raise ChromiumFetchError(f"Chromium source path is not a Git checkout: {src}")
        src.rmdir()
    _write_gclient(root)
    if not src.exists():
        src.mkdir()
    if not (src / ".git").is_dir():
        _run(["git", "init"], cwd=src, env=env, capture_output=False)
        _run(["git", "remote", "add", "origin", CHROMIUM_GIT_URL], cwd=src, env=env, capture_output=False)
    origin = _run(["git", "remote", "get-url", "origin"], cwd=src, env=env)
    if origin.stdout.strip().rstrip("/") != CHROMIUM_GIT_URL.rstrip("/"):
        raise ChromiumFetchError(
            f"Chromium checkout origin is {origin.stdout.strip()!r}, expected {CHROMIUM_GIT_URL!r}"
        )
    status = _run(["git", "status", "--porcelain"], cwd=src, env=env)
    if status.stdout.strip():
        raise ChromiumFetchError(f"Chromium checkout must be clean before switching: {src}")
    return src


def fetch_chromium(
    version: str,
    *,
    root: Path,
    depot_tools: Path,
    jobs: int,
    proxy: str | None = None,
    no_proxy: bool = False,
) -> Path:
    version_key(version)
    if jobs < 1:
        raise ChromiumFetchError("jobs must be at least 1")
    root = root.resolve()
    depot_tools = depot_tools.resolve()
    env = _network_environment(proxy, no_proxy)
    ensure_depot_tools(depot_tools, env)
    env["PATH"] = str(depot_tools) + os.pathsep + env.get("PATH", "")
    configure_windows_git_environment(depot_tools, env)
    bootstrap_depot_tools(depot_tools, env)
    src = _prepare_src_checkout(root, env)
    selected_ref = f"refs/tags/{version}"

    _run(
        ["git", "fetch", "--depth", "1", "origin", f"{selected_ref}:{selected_ref}"],
        cwd=src, env=env, retries=2, capture_output=False,
    )
    _run(["git", "checkout", "--detach", selected_ref], cwd=src, env=env, capture_output=False)

    sync_root = _short_gclient_root(root)
    _run(
        _depot_command(
            depot_tools, "gclient", "sync", "-D", "--no-history", "--nohooks",
            "--revision", f"src@{selected_ref}", f"-j{jobs}",
        ),
        cwd=sync_root, env=env, retries=2, capture_output=False,
    )
    return src


def _prepare_lastchange(src: Path, env: dict[str, str]) -> None:
    """Create GN source metadata when sync intentionally skipped hooks."""
    output = src / "build" / "util" / "LASTCHANGE"
    if output.is_file() and output.with_name("LASTCHANGE.committime").is_file():
        return
    python = src / "third_party" / "cpython3" / "host" / "bin" / "python3.exe"
    script = src / "build" / "util" / "lastchange.py"
    if python.is_file() and script.is_file():
        _run([str(python), str(script), "-o", str(output)], cwd=src, env=env, capture_output=False)


def build_bindings(
    src: Path,
    depot_tools: Path,
    *,
    build_dir: Path | None = None,
    target: str = "generate_bindings_all",
    env: dict[str, str] | None = None,
) -> Path:
    """Run GN and Ninja to generate Chromium's Blink ``v8_*.cc`` files."""
    src = src.resolve()
    depot_tools = depot_tools.resolve()
    build_dir = (build_dir or src / "out" / "Cronet145").resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    build_args = build_dir / "args.gn"
    if not build_args.is_file():
        build_args.write_text(
            "is_debug = false\n"
            "is_component_build = false\n"
            "symbol_level = 0\n"
            "blink_symbol_level = 0\n",
            encoding="ascii",
        )
    build_env = dict(env or os.environ)
    build_env.setdefault("DEPOT_TOOLS_WIN_TOOLCHAIN", "0")
    configured_toolchains = ("vs2026_install", "vs2022_install", "vs2019_install")
    if not any(build_env.get(name) and Path(build_env[name]).is_dir() for name in configured_toolchains):
        for candidate in (Path(r"D:\soft\Microsoft\vs2026"), Path(r"D:\Microsoft\vs\2022\Community")):
            if candidate.is_dir():
                build_env["vs2026_install" if "vs2026" in candidate.name.lower() else "vs2022_install"] = str(candidate)
                break
    for key in ("include", "INCLUDE", "lib", "LIB"):
        value = build_env.get(key)
        if value and any(not Path(part).exists() for part in value.split(os.pathsep) if part):
            build_env.pop(key, None)
    build_env.setdefault("WINDOWSSDKDIR", r"D:\Windows Kits\10")
    build_env["PATH"] = str(depot_tools) + os.pathsep + build_env.get("PATH", "")
    if os.name == "nt":
        configure_windows_git_environment(depot_tools, build_env)
    _prepare_lastchange(src, build_env)
    _run(_depot_command(depot_tools, "gn", "gen", str(build_dir)), cwd=src, env=build_env, capture_output=False)
    _run(_depot_command(depot_tools, "autoninja", "-C", str(build_dir), target), cwd=src, env=build_env, capture_output=False)
    if not any(build_dir.rglob("v8_*.cc")):
        raise ChromiumFetchError(f"binding generation completed but no v8_*.cc files were found under {build_dir}")
    return build_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="查看并拉取指定 Chromium 源码（不编译、不运行 hooks）。"
    )
    parser.add_argument("--version", help="四段 Chromium 版本号，例如 140.0.7339.82")
    parser.add_argument("--select", action="store_true", help="从远端列表交互选择版本并缓存")
    parser.add_argument("--list", action="store_true", help="列出近期 release tag 后退出")
    parser.add_argument("--limit", type=int, default=12, help="列表中显示的 milestone 数量（默认 12）")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("CHROMIUM_ROOT", DEFAULT_WORKSPACE)), help="workspace 根目录，源码位于 <root>/src")
    parser.add_argument("--depot-tools", type=Path, default=Path(os.environ.get("DEPOT_TOOLS_ROOT", DEFAULT_DEPOT_TOOLS)), help="depot_tools 路径；不存在时自动 clone")
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1), help="gclient 并发数")
    parser.add_argument("--proxy", help="Git/DEPS 下载使用的 HTTP(S) 代理")
    parser.add_argument("--no-proxy", action="store_true", help="禁用环境中的代理设置")
    parser.add_argument("--build-dir", type=Path, help="GN output directory, default <root>/src/out/Cronet145")
    parser.add_argument("--target", default="generate_bindings_all", help="Ninja generation target")
    parser.description = "Fetch Chromium and generate Blink V8 binding sources."
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.limit < 1:
            raise ChromiumFetchError("--limit must be at least 1")
        if args.proxy and args.no_proxy:
            raise ChromiumFetchError("--proxy and --no-proxy cannot be used together")
        if args.version:
            version_key(args.version)
        if args.list:
            print("Querying Chromium release tags...", flush=True)
            versions = query_versions(args.proxy, args.no_proxy)
            print(f"Found {len(versions)} release tags.")
            print_version_choices(versions, args.limit)
            return 0
        if args.version and args.select:
            raise ChromiumFetchError("--version and --select cannot be used together")
        if args.version:
            version = args.version
            version_key(version)
        elif args.select:
            print("Querying Chromium release tags...", flush=True)
            versions = query_versions(args.proxy, args.no_proxy)
            version = select_version(versions, limit=args.limit)
        else:
            version = os.environ.get("PYV8C_CHROMIUM_VERSION") or read_default_chromium_version()
            print(f"Using default Chromium version: {version}")
        src = fetch_chromium(
            version, root=args.root, depot_tools=args.depot_tools, jobs=args.jobs,
            proxy=args.proxy, no_proxy=args.no_proxy,
        )
        build_dir = build_bindings(src, args.depot_tools, build_dir=args.build_dir, target=args.target)
        print(f"Chromium {version} fetched to: {src}")
        print(f"Blink V8 bindings generated under: {build_dir}")
        return 0
    except ChromiumFetchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
