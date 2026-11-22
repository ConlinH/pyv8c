"""Serial Chrome/pyv8c evidence; each side owns an independently bounded process."""

from __future__ import annotations
import argparse
import hashlib
from importlib import resources
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback
from urllib.parse import urldefrag
from process_runner import run_worker
from probes import PROBES

ENCODE = r"""(function(value) {
 const seen=new Set();
 function enc(v) {
  if(v===undefined)return {type:'undefined'};
  if(v===null)return {type:'null'};
  const t=typeof v;
  if(t==='number')return {type:t,value:Number.isFinite(v)?(Object.is(v,-0)?'-0':v):String(v)};
  if(t==='string'||t==='boolean')return {type:t,value:v};
  if(t==='bigint'||t==='symbol'||t==='function')return {type:t,value:String(v)};
  if(Object.prototype.toString.call(v)==='[object Promise]')throw new TypeError('probe must return synchronously');
  if(seen.has(v))return {type:'cycle'};
  seen.add(v);let out;
  if(Array.isArray(v))out={type:'array',value:Array.from({length:v.length},(_,i)=>
    Object.prototype.hasOwnProperty.call(v,i)?enc(v[i]):{type:'missing'})};
  else out={type:'object',value:Object.fromEntries(Object.keys(v).map(k=>[k,enc(v[k])]))};
  seen.delete(v);return out;
 }
 return JSON.stringify(enc(value));
})"""
STATE = "({url:location.href,readyState:document.readyState,title:document.title,frames:frames.length})"


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def idl_evidence(browser):
    """Hash the actual archive or JSON, including packaged-resource labels."""
    label = str(browser.idl_path)
    if label.startswith("pyv8c:"):
        parts = label[len("pyv8c:"):].replace("\\", "/").split("/")
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"invalid packaged IDL label: {label}")
        data = resources.files("pyv8c").joinpath(*parts).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
    else:
        digest = sha(label)
    return {"idl_path": label, "idl_sha256": digest}


def source_state():
    roots = []
    for folder in Path(__file__).resolve().parents:
        if (folder / "pyv8c" / "pyproject.toml").is_file():
            roots = [folder]
            break
    output = []
    for root in roots:
        if not root.exists():
            continue
        entry = {"root": str(root)}
        for key, args in [
            ("head", ["rev-parse", "HEAD"]),
            ("status", ["status", "--short"]),
            ("diff", ["diff", "--binary", "HEAD"]),
        ]:
            try:
                run = subprocess.run(
                    ["git", "-C", str(root), *args], capture_output=True, timeout=10
                )
                entry[key if key != "diff" else "diff_sha256"] = (
                    hashlib.sha256(run.stdout).hexdigest()
                    if key == "diff"
                    else run.stdout.decode("utf-8", errors="replace").strip()
                )
                if run.returncode:
                    entry[key + "_error"] = run.stderr.decode("utf-8", errors="replace")
            except (OSError, subprocess.TimeoutExpired) as exc:
                entry[key + "_error"] = str(exc)
        output.append(entry)
    return output


def observe(page, expression):
    try:
        return {
            "status": "value",
            "value": json.loads(page.evaluate(f"{ENCODE}(({expression}))")),
        }
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return {
            "status": "error",
            "error": {"class": type(exc).__name__, "message": str(exc)},
        }


def worker(config, result_path):
    side = config["side"]
    start = time.monotonic()
    events_path = Path(result_path).with_name("events.jsonl")
    result = {
        "schema": 1,
        "side": side,
        "status": "running",
        "conditions": config,
        "stages": {},
        "observation": {"status": "not_observed"},
        "errors": [],
        "cleanup": {"status": "not_started"},
        "evidence": {"events": str(events_path)},
        "runtime": {"python": sys.version, "platform": platform.platform()},
    }
    browser = playwright = None
    events = events_path.open("w", encoding="utf-8")

    def record(kind, **data):
        events.write(
            json.dumps(
                {"elapsed": time.monotonic() - start, "kind": kind, **data},
                ensure_ascii=False,
                default=repr,
            )
            + "\n"
        )
        events.flush()

    def save():
        write_json(result_path, result)

    def error(phase, exc):
        result["errors"].append(
            {
                "phase": phase,
                "class": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        record("error", phase=phase, error=str(exc))
        result["status"] = "error"

    def fixture_response(url):
        return config["resources"].get(urldefrag(str(url))[0])

    save()
    try:
        if side == "here":
            from pyv8c.async_api import Browser
            from pyv8c import pyv8
            from pyv8c.pyv8 import _pyv8 as native

            result["runtime"].update(
                native_binary=native.__file__,
                native_sha256=sha(native.__file__),
                pyv8_version=getattr(pyv8, "__version__", "unavailable"),
                v8_version=native.v8_version(),
            )

            def loader(url, request):
                entry = fixture_response(url)
                record(
                    "fixture_request",
                    url=str(url),
                    method=request.get("method"),
                    matched=entry is not None,
                )
                return (
                    entry
                    if entry is not None
                    else {
                        "status": 404,
                        "body": "",
                        "headers": {"content-type": "text/plain"},
                    }
                )

            options = {
                "real_network": config["mode"] == "online",
                "stub_logging": "off",
                "trace": config["trace"],
                "threaded_loaders": False,
                "environment": {
                    "viewport.width": config["viewport"]["width"],
                    "viewport.height": config["viewport"]["height"],
                },
            }
            if config.get("idl_path"):
                options["idl_path"] = config["idl_path"]
            if config["mode"] == "fixture":
                options["resource_loader"] = loader
            if config["trace"]:

                def trace_value(value):
                    if value is pyv8.Undefined:
                        return {"type": "undefined"}
                    if value is pyv8.Null or value is None:
                        return {"type": "null"}
                    if isinstance(value, (str, int, float, bool)):
                        return {"type": type(value).__name__, "value": value}
                    if isinstance(value, (tuple, list)):
                        return [trace_value(v) for v in value]
                    return {
                        "type": type(value).__module__ + "." + type(value).__qualname__
                    }

                def trace(
                    access,
                    receiver,
                    member,
                    arguments,
                    value,
                    failure,
                    construct,
                    realm,
                ):
                    record(
                        "trace",
                        realm=str(realm),
                        access=str(access),
                        member=str(member),
                        receiver_type=type(receiver).__qualname__,
                        arguments=trace_value(arguments),
                        value=trace_value(value),
                        error=trace_value(failure),
                        construct=bool(construct),
                    )

                options["trace_callback"] = trace
            browser = Browser(**options)
            result["runtime"].update(idl_evidence(browser))
            profile_resource = resources.files("pyv8c").joinpath("data", "chrome-environment.json")
            if profile_resource.is_file():
                result["runtime"]["profile_sha256"] = hashlib.sha256(profile_resource.read_bytes()).hexdigest()
            page = browser.new_profile().new_page()
        else:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            launch = {
                "executable_path": config["chrome_path"],
                "headless": config["headless"],
            }
            result["runtime"].update(
                launch=launch,
                automation_defaults="Playwright defaults; not hidden",
                service_workers="block" if config["mode"] == "fixture" else "allow",
            )
            browser = playwright.chromium.launch(**launch)
            result["runtime"]["chrome_version"] = browser.version
            session = browser.new_browser_cdp_session()
            try:
                result["runtime"]["chrome_build"] = session.send("Browser.getVersion")
                try:
                    result["runtime"]["command_line"] = session.send(
                        "Browser.getBrowserCommandLine"
                    )
                except Exception as exc:
                    # This CDP method requires --enable-automation, which a locally
                    # configured Playwright may omit. Never alter launch flags to
                    # make metadata collection succeed. Inspect only owned children.
                    result["runtime"]["command_line"] = {
                        "status": "unavailable",
                        "cdp_error": str(exc),
                    }
                    try:
                        import psutil

                        candidates = [
                            child.cmdline()
                            for child in psutil.Process().children(recursive=True)
                            if Path(child.exe()).resolve()
                            == Path(config["chrome_path"]).resolve()
                        ]
                        result["runtime"]["command_line"]["owned_processes"] = (
                            candidates
                        )
                        result["runtime"]["command_line"]["status"] = (
                            "process_inspection" if candidates else "unavailable"
                        )
                    except Exception as failure:
                        result["runtime"]["command_line"]["inspection_error"] = str(
                            failure
                        )
            finally:
                session.detach()
            page = browser.new_page(
                viewport=config["viewport"],
                service_workers="block" if config["mode"] == "fixture" else "allow",
            )
            page.on(
                "request",
                lambda req: record(
                    "request",
                    url=req.url,
                    method=req.method,
                    resource_type=req.resource_type,
                ),
            )
            page.on(
                "response",
                lambda resp: record("response", url=resp.url, status=resp.status),
            )
            page.on(
                "requestfailed",
                lambda req: record("request_failed", url=req.url, failure=req.failure),
            )
            page.on("pageerror", lambda exc: record("page_error", error=str(exc)))
            page.on(
                "console", lambda msg: record("console", level=msg.type, text=msg.text)
            )
            for event in ("domcontentloaded", "load"):
                page.on(event, lambda event=event: record(event))
            page.on(
                "framenavigated",
                lambda frame: record("frame_navigation", url=frame.url),
            )
            page.on("worker", lambda obj: record("worker", url=obj.url))
            if config["mode"] == "fixture":

                def route_request(route):
                    entry = fixture_response(route.request.url)
                    record(
                        "fixture_request",
                        url=route.request.url,
                        matched=entry is not None,
                    )
                    if entry is None:
                        route.fulfill(status=404, body="", content_type="text/plain")
                    else:
                        route.fulfill(
                            status=entry.get("status", 200),
                            body=entry.get("body", ""),
                            headers=entry.get("headers", {}),
                        )

                page.route("**/*", route_request)
        result["stages"]["navigation"] = {"status": "started"}
        save()
        record("navigation_start", url=config["url"])
        if side == "chrome":
            page.goto(
                config["url"],
                wait_until="commit",
                timeout=config["navigation_timeout"] * 1000,
            )
        else:
            page.goto(config["url"])
        result["stages"]["navigation"] = {
            "status": "returned",
            "state": observe(page, STATE),
        }
        save()
        record("navigation_returned")
        deadline = time.monotonic() + config["seconds"]
        ready = config.get("wait_for")
        last_ready = {"status": "not_observed"}
        while True:
            if side == "here":
                page.run_until_idle(timeout=0)
            if ready:
                last_ready = observe(page, f"Boolean(({ready}))")
                if last_ready == {
                    "status": "value",
                    "value": {"type": "boolean", "value": True},
                }:
                    break
                if last_ready["status"] == "error":
                    record("ready_error", observation=last_ready)
            if time.monotonic() >= deadline:
                break
            if side == "chrome":
                page.wait_for_timeout(50)
            else:
                time.sleep(0.05)
        passed = last_ready == {
            "status": "value",
            "value": {"type": "boolean", "value": True},
        }
        result["stages"]["ready"] = {
            "status": ("reached" if passed else "not_reached")
            if ready
            else "not_requested",
            "last": last_ready,
        }
        result["stages"]["final"] = observe(page, STATE)
        result["runtime"]["environment"] = observe(
            page,
            "({ua:navigator.userAgent,language:navigator.language,"
            "width:innerWidth,height:innerHeight,secure:isSecureContext})",
        )
        if config.get("probe"):
            result["observation"] = observe(page, config["probe"])
        result["status"] = "ok" if not ready or passed else "not_ready"
        if any(
            item["status"] == "error"
            for item in (
                result["observation"],
                result["stages"]["final"],
                result["runtime"]["environment"],
                last_ready,
            )
        ):
            result["status"] = "error"
        save()
    except BaseException as exc:
        error("execution", exc)
        save()
    finally:
        result["cleanup"] = {"status": "running"}
        save()
        for label, action in [
            ("browser", browser.close if browser else None),
            ("playwright", playwright.stop if playwright else None),
        ]:
            if action:
                try:
                    action()
                except BaseException as exc:
                    error("cleanup." + label, exc)
        result["cleanup"] = {
            "status": "error"
            if any(e["phase"].startswith("cleanup.") for e in result["errors"])
            else "ok"
        }
        result["elapsed"] = time.monotonic() - start
        save()
        events.close()


def compare(left, right):
    if any(item["status"] not in ("ok", "not_ready") for item in (left, right)):
        return "inconclusive"
    if any(item["status"] == "not_ready" for item in (left, right)):
        return "target_not_reached"
    if any(item["observation"]["status"] == "not_observed" for item in (left, right)):
        return "not_observed"
    return "equal" if left["observation"] == right["observation"] else "different"


def load_fixture(path):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    resources = manifest["resources"]
    if urldefrag(manifest["url"])[0] not in resources:
        raise ValueError("fixture must explicitly include its navigation URL")
    for url, entry in resources.items():
        if not url.startswith(("http://", "https://")):
            raise ValueError("fixture keys must be HTTP(S) URLs")
        if "body_file" in entry:
            entry["body"] = (path.parent / entry.pop("body_file")).read_text(
                encoding="utf-8"
            )
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--url")
    target.add_argument("--fixture")
    parser.add_argument("--probe", action="append", default=[])
    parser.add_argument("--probe-file", help="UTF-8 JS expression")
    parser.add_argument("--wait-for", help="JS expression that must become true")
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument(
        "--timeout",
        type=float,
        default=45,
        help="total seconds per side, including close",
    )
    parser.add_argument("--navigation-timeout", type=float, default=15)
    parser.add_argument("--idl-path", default=os.environ.get("PYV8C_TEST_IDL_PATH"))
    parser.add_argument(
        "--chrome-path",
        default=os.environ.get("PYV8C_CHROME") or os.environ.get("CHROME_PATH"),
    )
    parser.add_argument(
        "--headless", action="store_true", help="recorded variant; default headed"
    )
    parser.add_argument(
        "--trace", action="store_true", help="pyv8c instrumentation variant"
    )
    parser.add_argument("--side", choices=["here", "chrome", "both"], default="both")
    parser.add_argument("--out", default="parity-result.json")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        for name, spec in PROBES.items():
            print(
                name,
                "(fixture)" if spec.get("fixture") else "(needs URL)",
                spec["note"],
            )
        return 0
    if not (args.url or args.fixture or args.probe):
        parser.error("select --url, --fixture or --probe")
    if (
        not all(
            math.isfinite(v)
            for v in (args.seconds, args.timeout, args.navigation_timeout)
        )
        or args.seconds < 0
        or min(args.timeout, args.navigation_timeout) <= 0
    ):
        parser.error("invalid time budget")
    if args.side != "here" and (
        not args.chrome_path or not Path(args.chrome_path).is_file()
    ):
        parser.error("provide an existing --chrome-path; no browser fallback is used")
    if args.fixture and args.probe:
        parser.error("--fixture uses its own probe or --probe-file")
    names = list(PROBES) if "all" in args.probe else args.probe or ["page"]
    unknown = set(names) - set(PROBES) - {"page"}
    if unknown:
        parser.error("unknown probes: " + ", ".join(sorted(unknown)))
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = output.parent / (output.stem + "-" + str(time.time_ns()))
    evidence.mkdir()
    summary = {
        "schema": 1,
        "source": source_state(),
        "evidence": str(evidence),
        "cases": {},
    }
    failed = False
    for index, name in enumerate(names):
        spec = PROBES.get(name, {})
        config = {
            "url": args.url,
            "mode": "online",
            "resources": {},
            "probe": spec.get("probe"),
            "seconds": args.seconds,
            "wait_for": args.wait_for,
            "trace": args.trace,
            "timeout": args.timeout,
            "navigation_timeout": args.navigation_timeout,
            "viewport": {"width": 1280, "height": 720},
            "headless": args.headless,
            "idl_path": str(Path(args.idl_path).resolve()) if args.idl_path else None,
            "chrome_path": str(Path(args.chrome_path).resolve())
            if args.chrome_path
            else None,
        }
        if args.fixture:
            manifest = load_fixture(args.fixture)
            config.update(
                url=manifest["url"],
                resources=manifest["resources"],
                mode="fixture",
                probe=manifest.get("probe"),
                wait_for=args.wait_for or manifest.get("ready"),
            )
        elif not args.url and spec.get("fixture"):
            url = "https://probe.test/" + name + ".html"
            config.update(
                url=url,
                mode="fixture",
                resources={
                    url: {
                        "body": spec["fixture"][0],
                        "headers": {"content-type": "text/html"},
                    }
                },
            )
        if not config["url"]:
            parser.error(f"{name} requires --url")
        if args.probe_file:
            config["probe"] = Path(args.probe_file).read_text(encoding="utf-8")
        entry = summary["cases"][name] = {}
        for side in ["here", "chrome"] if args.side == "both" else [args.side]:
            directory = evidence / f"{index}-{side}"
            directory.mkdir()
            entry[side] = run_worker(
                Path(__file__), dict(config, side=side), directory, args.timeout
            )
            failed |= entry[side]["status"] != "ok"
            write_json(output, summary)
        if args.side == "both":
            entry["comparison"] = compare(entry["here"], entry["chrome"])
            failed |= entry["comparison"] == "different"
        print(
            name,
            {
                side: entry[side]["status"]
                for side in ("here", "chrome")
                if side in entry
            },
            entry.get("comparison", "single-side"),
        )
    write_json(output, summary)
    print("Evidence:", output)
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        if sys.stdin.readline().strip() != "start":
            raise SystemExit(2)
        cfg = Path(sys.argv[2])
        worker(
            json.loads(cfg.read_text(encoding="utf-8")), cfg.with_name("result.json")
        )
    else:
        raise SystemExit(main())
