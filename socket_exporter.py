# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Read-only AndLink raw telemetry exporter. No runtime files or third-party packages."""
import argparse
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import ssl
import threading
import time
import tomllib
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

FIELDS = ("power", "volts", "current", "powerConsumption")
PREFIX = "/espapi/v3/cloud/json/family/devices/"
MAX_BODY = 1_048_576


class Failure(Exception):
    def __init__(self, code, retry=0):
        self.code, self.retry = code, retry
        super().__init__(code)  # Only local fixed codes; never upstream messages.


def retry_after(value):
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            return 900
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 900
    return max(0, seconds)


def decode_json(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError()
            result[k] = v
        return result
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise Failure("invalid_response") from None


def valid_key(value):
    return isinstance(value, str) and 1 <= len(value) <= 16384 and all(33 <= ord(c) <= 126 for c in value)


class Cloud:
    def __init__(self, timeout):
        self.timeout = timeout
        self.tls = ssl.create_default_context()
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2

    def request(self, host, path, *, key=None, body=None):
        # Fixed HTTPS hosts only; no redirects, custom URL or proxy credentials.
        if host not in ("base.hjq.komect.com", "andlink.komect.com"):
            raise Failure("invalid_endpoint")
        conn = http.client.HTTPSConnection(host, timeout=self.timeout, context=self.tls)
        headers = {"Accept": "application/json", "User-Agent": "andlink-raw-exporter/1"}
        if key is not None:
            if not valid_key(key):
                raise Failure("invalid_response")
            headers["API_KEY"] = key
        if body is not None:
            headers["Content-Type"] = "application/json"
        deadline = time.monotonic() + self.timeout
        try:
            conn.request("POST" if body is not None else "GET", path, body=body, headers=headers)
            response = conn.getresponse()
            if response.status == 429:
                raise Failure("rate_limited", retry_after(response.getheader("Retry-After")))
            if response.status in (401, 403):
                raise Failure("authentication_failed")
            if response.status != 200:
                raise Failure("http_error")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                chunk = response.read1(min(65536, MAX_BODY + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BODY:
                    raise Failure("response_too_large")
            return decode_json(b"".join(chunks)), response.headers
        except TimeoutError:
            raise Failure("timeout") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise Failure("connection_failed") from None
        finally:
            conn.close()

    @staticmethod
    def accepted(data, headers):
        if type(data.get("resultCode")) is int and data["resultCode"] == 0:
            return
        message = " ".join(str(data.get(k, "")) for k in ("message", "msg", "resultCodeDesc", "error")).lower()
        if any(s in message for s in ("频繁", "限流", "限额", "rate limit", "quota", "too many")):
            raise Failure("rate_limited", retry_after(headers.get("Retry-After")))
        if any(s in message for s in ("登录", "认证", "token", "api_key", "unauthor", "session", "expired")):
            raise Failure("authentication_failed")
        raise Failure("upstream_rejected")

    def login(self, username, password):
        body = json.dumps({"userAccount": username, "authType": "10",
                           "virtualAuthdata": hashlib.md5(password.encode()).hexdigest(),
                           "authdata": hashlib.sha1(("fetion.com.cn:" + password).encode()).hexdigest()}).encode()
        data, headers = self.request("base.hjq.komect.com", "/base/user/passwdLogin", body=body)
        if data.get("code") != "1000000":
            raise Failure("authentication_failed")
        matches = []
        try:
            for header in headers.get_all("Set-Cookie", []):
                cookie = SimpleCookie()
                cookie.load(header)
                if "JSESSIONID" in cookie:
                    matches.append(cookie["JSESSIONID"].value)
        except CookieError:
            raise Failure("invalid_response") from None
        if len(matches) != 1 or not valid_key(matches[0]):
            raise Failure("invalid_response")
        data, headers = self.request("andlink.komect.com", "/espapi/cloud/json/loginByApp?cloudName=CMCC&keyType=0",
                                     key=matches[0] + ":010108:15")
        self.accepted(data, headers)
        if not valid_key(data.get("key")):
            raise Failure("invalid_response")
        return data["key"]

    def resolve(self, key, selectors):
        devices = []
        if any(s.startswith("name:") for s in selectors.values()):
            data, headers = self.request("andlink.komect.com", PREFIX + "list", key=key)
            self.accepted(data, headers)
            devices = data.get("devices")
            if not isinstance(devices, list):
                raise Failure("invalid_response")
        result = {}
        for alias, selector in selectors.items():
            kind, value = selector.split(":", 1)
            matches = [value] if kind == "id" else [d.get("id") for d in devices if isinstance(d, dict)
                and str(d.get("type")) in ("30944", "590513") and value in (d.get("name"), d.get("desc"))]
            if len(matches) != 1 or not isinstance(matches[0], str) or not 1 <= len(matches[0]) <= 128:
                raise Failure("device_selection_failed")
            result[alias] = matches[0]
        if len(set(result.values())) != len(result):
            raise Failure("device_selection_failed")
        return result

    def detail(self, key, device_id):
        data, headers = self.request("andlink.komect.com", PREFIX + "detail/get?" +
                                     urlencode({"checkConnected": True, "deviceId": device_id}), key=key)
        self.accepted(data, headers)
        device = data.get("device")
        if not isinstance(device, dict) or not isinstance(device.get("parameters"), list):
            raise Failure("invalid_response")
        if any(k in device and device[k] != device_id for k in ("id", "deviceId")):
            raise Failure("device_identity_mismatch")
        values, seen = {}, set()
        for p in device["parameters"]:
            if not isinstance(p, dict) or p.get("name") not in FIELDS:
                continue
            name, value = p["name"], p.get("value")
            if name in seen:
                raise Failure("invalid_response")
            seen.add(name)
            try:
                if isinstance(value, bool) or value is None:
                    continue
                value = float(value)
                if math.isfinite(value):
                    values[name] = value
            except (ValueError, TypeError, OverflowError):
                pass
        return {"values": values, "connected": device.get("connected") if type(device.get("connected")) is bool else None,
                "at": time.time(), "mono": time.monotonic()}


def label(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Exporter:
    def __init__(self, config, cloud=None):
        self.username, self.password = config.pop("username"), config.pop("password")
        self.selectors, self.ttl = config["devices"], config["cache_ttl"]
        self.cloud = cloud or Cloud(config["timeout"])
        self.lock = threading.Lock()
        self.key, self.devices = None, None
        self.cache, self.error = {}, "not_fetched"
        self.busy = self.stopped = self.attempted = False
        self.due, self.errors, self.failures = 0., 0, 0

    def refresh(self):
        try:
            if not self.attempted:
                self.attempted = True
                try:
                    self.key = self.cloud.login(self.username, self.password)
                finally:
                    self.username = self.password = None
            if self.devices is None:
                self.devices = self.cloud.resolve(self.key, self.selectors)
            cache = {a: self.cloud.detail(self.key, i) for a, i in self.devices.items()}
            with self.lock:
                self.cache, self.error, self.failures = cache, "none", 0
                self.due = time.monotonic() + self.ttl
        except Exception as exc:
            code = exc.code if isinstance(exc, Failure) else "internal_error"
            delay = exc.retry if isinstance(exc, Failure) else 0
            with self.lock:
                self.cache, self.error = {}, code
                self.errors += 1
                self.failures += 1
                self.stopped = self.key is None or code in ("authentication_failed", "device_selection_failed", "device_identity_mismatch")
                if self.stopped:
                    self.key = None
                self.due = time.monotonic() + max(delay, min(900, self.ttl * 2 ** min(self.failures - 1, 8)))
        finally:
            with self.lock:
                self.busy = False

    def metrics(self):
        with self.lock:
            now = time.monotonic()
            if not self.busy and not self.stopped and now >= self.due:
                self.busy = True
                threading.Thread(target=self.refresh, daemon=True).start()
            cache, error, busy, stopped = dict(self.cache), self.error, self.busy, self.stopped
            errors, wait = self.errors, max(0, self.due - now) if error not in ("none", "not_fetched") else 0
        lines = []
        def gauge(name, help_text, samples):
            lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
            lines.extend(f"{name}{tags} {value}" for tags, value in samples)
        fresh = {a: s for a, s in cache.items() if now - s["mono"] <= 2 * self.ttl}
        gauge("andlink_exporter_up", "Latest cloud cycle succeeded and cached responses have not expired; not physical sample freshness.",
              [("", int(error == "none" and len(fresh) == len(self.selectors)))])
        gauge("andlink_exporter_refresh_in_progress", "One serialized cloud refresh is in flight.", [("", int(busy))])
        gauge("andlink_exporter_stopped", "Collection stopped; manual investigation and restart required.", [("", int(stopped))])
        gauge("andlink_exporter_retry_after_seconds", "Minimum remaining local backoff; restart does not bypass vendor limits.", [("", wait)])
        gauge("andlink_exporter_error", "Fixed local error reason; contains no upstream message.", [(f'{{reason="{error}"}}', 1)])
        lines.extend(("# HELP andlink_exporter_refresh_errors_total Failed refresh cycles.", "# TYPE andlink_exporter_refresh_errors_total counter", f"andlink_exporter_refresh_errors_total {errors}"))
        raw, connected, fetched, available = [], [], [], []
        for alias in self.selectors:
            tags = f'{{device="{label(alias)}"}}'
            sample = fresh.get(alias)
            usable = sample is not None and sample["connected"] is not False
            available.append((tags, int(usable and bool(sample["values"]))))
            if sample:
                fetched.append((tags, sample["at"]))
                if sample["connected"] is not None:
                    connected.append((tags, int(sample["connected"])))
            if usable:
                raw.extend((f'{{device="{label(alias)}",parameter="{name}",unit_status="unverified"}}', value) for name, value in sample["values"].items())
        gauge("andlink_device_sample_available", "At least one finite raw field available from a recent cloud response; units unverified.", available)
        gauge("andlink_cloud_connected", "Vendor connection flag, omitted when unknown.", connected)
        gauge("andlink_last_fetch_timestamp_seconds", "Cloud HTTP fetch time, NOT device measurement time.", fetched)
        gauge("andlink_raw_parameter", "Unscaled raw parameter; units and measurement freshness unverified. Not W/V/A/kWh.", raw)
        return "\n".join(lines) + "\n"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def get_request(self):
        conn, address = super().get_request()
        conn.settimeout(5)
        return conn, address
    def handle_error(self, *_):
        pass  # Never print exception context or request contents.


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def reply(self, status, body):
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        self.close_connection = True
    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, "ok\n")
        if self.path == "/metrics":
            return self.reply(200, self.server.exporter.metrics())
        self.reply(404, "not_found\n")
    def send_error(self, code, message=None, explain=None):
        self.reply(code, "request_rejected\n")
    def reject(self):
        self.reply(405, "read_only\n")
    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = reject


class Parser(argparse.ArgumentParser):
    def error(self, _):
        self.exit(2, "Invalid arguments; use --help. Values are not logged.\n")


def configuration(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--config", help="TOML file; otherwise SOCKET_EXPORTER_CONFIG")
    for key in ("username", "password", "listen", "port", "cache-ttl", "timeout"):
        parser.add_argument("--" + key)
    parser.add_argument("--device", action="append", metavar="ALIAS=id:ID_OR_name:EXACT_NAME")
    parser.add_argument("--check-config", action="store_true", help="Validate without login or network access")
    args = parser.parse_args(argv)
    path = args.config or os.getenv("SOCKET_EXPORTER_CONFIG")
    try:
        cfg = tomllib.loads(Path(path).read_text(encoding="utf-8-sig")) if path else {}
        defaults = {"username": "", "password": "", "listen": "127.0.0.1", "port": 9188, "cache_ttl": 60, "timeout": 10, "devices": {}}
        if set(cfg) - set(defaults):
            raise ValueError()
        for key, default in defaults.items():
            env = os.getenv("SOCKET_EXPORTER_" + key.upper())
            cli = getattr(args, key, None)
            cfg[key] = cli if cli is not None else env if env is not None else cfg.get(key, default)
        if isinstance(cfg["devices"], str):
            cfg["devices"] = json.loads(cfg["devices"])
        if args.device:
            devices = [s.split("=", 1) for s in args.device]
            cfg["devices"] = dict(devices)
            if len(cfg["devices"]) != len(devices):
                raise ValueError()
        if not isinstance(cfg["username"], str) or not re.fullmatch(r"1[0-9]{10}", cfg["username"]):
            raise ValueError()
        password = cfg["password"]
        if not isinstance(password, str) or not 1 <= len(password) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in password):
            raise ValueError()
        if ipaddress.ip_address(cfg["listen"]).version != 4:
            raise ValueError()
        for key, low, high in (("port", 1, 65535), ("cache_ttl", 10, 3600), ("timeout", 1, 60)):
            if isinstance(cfg[key], bool) or str(cfg[key]) != str(int(cfg[key])) or not low <= int(cfg[key]) <= high:
                raise ValueError()
            cfg[key] = int(cfg[key])
        if not isinstance(cfg["devices"], dict) or not 1 <= len(cfg["devices"]) <= 8:
            raise ValueError()
        for alias, selector in cfg["devices"].items():
            if not isinstance(alias, str) or not 1 <= len(alias) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in alias):
                raise ValueError()
            if not isinstance(selector, str) or not re.fullmatch(r"(?:id|name):[^\x00-\x1f\x7f]{1,128}", selector):
                raise ValueError()
        return cfg, args.check_config
    except (OSError, ValueError, TypeError, OverflowError):
        parser.exit(2, "Invalid configuration; check credentials, devices and limits. Values are not logged.\n")


def main():
    cfg, check = configuration()
    if check:
        print("Configuration valid; no network access.")
        return
    try:
        server = Server((cfg["listen"], cfg["port"]), Handler)
        server.exporter = Exporter(cfg)
    except Exception:
        raise SystemExit("Startup failed; check listen address, port and TLS environment.") from None
    print("Exporter ready; GET /metrics and /healthz. Credentials and responses are never logged.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
