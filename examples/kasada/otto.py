"""Fetch OTTO search HTML using pyv8c and the current, unmodified challenge SDK.

Each run starts a fresh profile. HAR files, captured tokens, browser automation,
and saved challenge payloads are never used as runtime inputs.
Responses and products stay in memory; results are logged without writing files.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from loguru import logger
from pyv8c import Browser, register_transport
from pyv8c.network.curl_cffi import CurlCffiTransport

DEFAULT_URL = "https://www.otto.de/suche/banana/"
CHALLENGE_FILES = {"ips.js", "p.js", "rc.js", "fp", "mfc", "tl"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}


class _ProductParser(HTMLParser):
    """Read the server-rendered product tiles in their document order."""

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.products = []
        self.current = None
        self.stack = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if self.current is None:
            if tag != "article" or attrs.get("data-qa") != "reptile-product-tile":
                return
            self.current = {"product_id": attrs.get("data-product-id"),
                            "variation_id": attrs.get("data-variation-id"),
                            "position": attrs.get("data-list-position"),
                            "sponsored": attrs.get("data-origin") == "sponsored",
                            "_text": {"brand": [], "title": []}}
        classes = (attrs.get("class") or "").split()
        placeholder_link = tag == "a" and "reptile-tile-placeholder__link" in classes
        role = next((name for name in ("brand", "title")
                     if "reptile-product-" + name in classes), None)
        if placeholder_link:
            role = "title"
        if tag not in VOID_TAGS:
            self.stack.append((tag, role))
        if attrs.get("data-qa") == "find_tile__productLink" or placeholder_link:
            href = attrs.get("href")
            if not href and attrs.get("base64-href"):
                href = base64.b64decode(attrs["base64-href"], validate=True).decode("utf-8")
            if href:
                self.current["url"] = urljoin(self.base_url, href)
        elif tag == "ofc-pricing-item-v1" and "price" not in self.current:
            for source, field in (("retail-price", "price"),
                                  ("suggested-retail-price", "original_price")):
                raw = attrs.get(source)
                self.current[field] = (format(Decimal(raw) / 100, ".2f")
                                       if raw is not None else None)
            self.current["currency"] = "EUR"
            self.current["starting_price"] = attrs.get("is-starting-price") == "true"
        elif tag == "img" and "reptile-product-image__image" in classes:
            self.current["image"] = urljoin(self.base_url, attrs.get("src") or "")
            self.current["_alt"] = attrs.get("alt") or ""

    def handle_data(self, data):
        if self.current is not None:
            for role in {role for _, role in self.stack if role}:
                self.current["_text"][role].append(data)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1)
                      if self.stack[i][0] == tag), None)
        if index is None:
            return
        del self.stack[index:]
        if self.stack or self.current is None:
            return
        product = self.current
        texts = product.pop("_text")
        for field, parts in texts.items():
            product[field] = re.sub(r"\s+", " ", "".join(parts)).strip()
        alternative = product.pop("_alt", "")
        product["name"] = (" ".join(filter(None, (product["brand"], product["title"])))
                           if product["title"] else alternative)
        if product["position"] and product["position"].isdigit():
            product["position"] = int(product["position"])
        for field in ("price", "original_price", "currency", "image"):
            product.setdefault(field, None)
        product.setdefault("starting_price", False)
        self.products.append(product)
        self.current = None


def extract_products(html, base_url=DEFAULT_URL):
    parser = _ProductParser(base_url)
    parser.feed(html)
    parser.close()
    products, seen = [], set()
    for product in parser.products:
        if not product.get("product_id") or not product.get("name") or not product.get("url"):
            raise ValueError("商品卡片缺少 ID、名称或链接，请检查当前页面结构")
        key = product.get("variation_id") or product["product_id"]
        if key not in seen:
            seen.add(key)
            products.append(product)
    return products


def log_products(products):
    logger.info("共整理 {} 条商品，按页面顺序打印前 {} 条", len(products), min(10, len(products)))
    for index, product in enumerate(products[:10], 1):
        price = product.get("price")
        price_text = (f"{price} {product['currency']}" if price is not None else "未提供价格")
        if product.get("starting_price"):
            price_text += " 起"
        logger.info("商品 {:02d}: {}{} | {}", index, product["name"],
                    " [广告]" if product["sponsored"] else "", price_text)
        logger.info("  ID={}，规格={}，链接={}", product["product_id"],
                    product.get("variation_id") or "--", product["url"])


def run(args) -> dict:
    target = urlsplit(args.url)
    if target.scheme != "https" or target.hostname != "www.otto.de":
        raise ValueError("The target must be an HTTPS URL on www.otto.de")
    report = {"url": args.url, "runtime": "pyv8c", "proxy": args.proxy,
              "uses_har": False, "uses_browser_automation": False,
              "sdk_modified": False, "events": [], "documents": [], "tl": [],
              "success": False}
    logger.info("查询 {}，代理 {}", args.url, args.proxy)
    lock = threading.RLock()
    finished = threading.Event()
    started = time.monotonic()
    search_html = ""

    def record(event, **fields):
        with lock:
            item = {"at": round(time.monotonic() - started, 3), "event": event, **fields}
            report["events"].append(item)
            if event == "request":
                logger.info("{} {}，请求体 {} B", fields["method"], fields["path"], fields["body_bytes"])
                if fields["kpsdk"]:
                    logger.info("SDK 参数头长度：{}", json.dumps(fields["kpsdk"], ensure_ascii=False))
            elif event == "response":
                logger.info("{} {} -> HTTP {}, {} B", fields["method"], fields["path"],
                            fields["status"], fields["bytes"])
            elif event == "tl_result":
                log = logger.success if fields["reload"] else logger.error
                log("/tl HTTP {}，响应 {}", fields["status"],
                    json.dumps(fields["response"], ensure_ascii=False))
            else:
                logger.error("{}: {}", event, json.dumps(fields, ensure_ascii=False))

    def header_lengths(headers):
        return {str(k).lower(): len(str(v)) for k, v in headers.items()
                if str(k).lower().startswith("x-kpsdk-")}

    def relevant(url):
        parts = urlsplit(url)
        return parts.hostname == target.hostname and (
            parts.path == target.path or parts.path.rsplit("/", 1)[-1] in CHALLENGE_FILES)

    def intercept(url, request):
        parts = urlsplit(str(url))
        allowed = relevant(str(url)) or parts.scheme in {"about", "data", "blob", "javascript"}
        return None if allowed else False

    class ObservedTransport(CurlCffiTransport):
        def _call(self, request):
            nonlocal search_html
            observe = relevant(request.url)
            path = urlsplit(request.url).path
            if observe:
                record("request", method=request.method, path=path,
                       kpsdk=header_lengths(request.headers or {}),
                       body_bytes=len(request.body or b""))
            try:
                response = super()._call(request)
            except Exception as error:
                if observe:
                    record("transport_error", path=path, error=type(error).__name__, detail=str(error)[:240])
                    finished.set()
                raise
            if not observe:
                return response
            content = response.content
            record("response", method=request.method, path=path,
                   status=response.status, bytes=len(content),
                   sha256=hashlib.sha256(content).hexdigest(),
                   kpsdk=header_lengths(response.headers or {}))
            with lock:
                if path == target.path and request.method.upper() == "GET":
                    text = response.text
                    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
                    business_html = (response.status == 200 and len(content) > 20000
                                     and "banana" in text.lower())
                    report["documents"].append({"status": response.status,
                        "bytes": len(content),
                        "title": title.group(1).strip() if title else None,
                        "business_html": business_html})
                    if business_html:
                        search_html = text
                        finished.set()
                elif path.endswith("/tl") and request.method.upper() == "POST":
                    try:
                        body = response.json()
                    except (ValueError, TypeError):
                        body = None
                    accepted = (response.status == 200 and isinstance(body, dict)
                                and body.get("reload") is True)
                    report["tl"].append({"status": response.status, "json": body,
                                         "reload": accepted, "bytes": len(content)})
                    record("tl_result", status=response.status, reload=accepted,
                           response=body if isinstance(body, dict) else None)
                    if not accepted:
                        finished.set()
            return response

    register_transport("otto_search", ObservedTransport, proxy=True)
    environment = (json.loads(args.environment.read_text(encoding="utf-8"))
                   if args.environment else None)
    browser = Browser(real_network=True, proxy=args.proxy, cache=False,
                      threaded_loaders=True, environment=environment,
                      resource_interceptor=intercept, http_engine="otto_search",
                      http_options={"impersonate": "chrome150", "timeout": 25},
                      ws_engine=None, stub_logging="off")
    try:
        page = browser.new_profile().new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=min(35, args.timeout))
        except Exception as error:
            record("navigation_error", error=type(error).__name__)
        finished.wait(max(0, args.timeout - (time.monotonic() - started)))
    finally:
        browser.close()
    report["success"] = any(d["business_html"] for d in report["documents"])
    report["challenge_verified"] = any(t["reload"] for t in report["tl"])
    report["product_count"] = 0
    if report["success"]:
        try:
            products = extract_products(search_html, args.url)
            if not products:
                raise ValueError("搜索页未提取到商品，请检查当前页面结构")
            report["product_count"] = len(products)
            logger.success("搜索页 HTTP 200，商品提取成功")
            log_products(products)
        except (ValueError, InvalidOperation) as error:
            report["success"] = False
            report["product_error"] = str(error)
            logger.error("商品提取失败：{}", error)
    else:
        logger.error("未取得搜索页 HTTP 200 业务响应")
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    logger.info("success={}，challenge_verified={}，商品={} 条，耗时 {}s",
                report["success"], report["challenge_verified"], report["product_count"],
                report["elapsed_seconds"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--proxy", default="http://127.0.0.1:6696")
    parser.add_argument("--timeout", type=float, default=65)
    parser.add_argument("--environment", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}",
               filter=lambda record: record["name"] == __name__)
    try:
        return 0 if run(args)["success"] else 1
    except Exception as error:
        logger.error("运行失败：{}: {}", type(error).__name__, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
