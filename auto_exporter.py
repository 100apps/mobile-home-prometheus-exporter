"""Local adapter: auto-discover account sockets every 300s; preserve upstream read-only telemetry."""
import http.client
_original_getresponse = http.client.HTTPSConnection.getresponse
def _logged_response(connection):
    response = _original_getresponse(connection)
    if response.status != 200:
        print('Cloud HTTP status: %d' % response.status, flush=True)
    return response
http.client.HTTPSConnection.getresponse = _logged_response

import threading
from collections import deque
import hashlib
import time
from socket_exporter import Cloud, Exporter, Failure, PREFIX, Server, Handler, configuration, label

class DiscoverCloud(Cloud):
    def request(self, host, path, **kwargs):
        # Read existing cloud telemetry without triggering a synchronous connection check.
        path = path.replace("checkConnected=True", "checkConnected=false")
        started = time.monotonic()
        stage = path.split('?')[0].split('/')[-1]
        print('Cloud request started: ' + stage, flush=True)
        try:
            return super().request(host, path, **kwargs)
        finally:
            print('Cloud request finished: ' + stage + ' %.1fs' % (time.monotonic() - started), flush=True)

    def __init__(self, timeout, owner):
        super().__init__(timeout)
        self.owner = owner

    def resolve(self, key, selectors):
        data, headers = self.request('andlink.komect.com', PREFIX + 'list', key=key)
        self.accepted(data, headers)
        entries = data.get('devices')
        if not isinstance(entries, list):
            raise Failure('invalid_response')
        devices, names = {}, {}
        for item in entries:
            if not isinstance(item, dict) or str(item.get('type')) not in ('30944', '590513'):
                continue
            device_id = item.get('id')
            if not isinstance(device_id, str) or not 1 <= len(device_id) <= 128:
                raise Failure('invalid_response')
            alias = 'plug_' + hashlib.sha256(device_id.encode()).hexdigest()[:16]
            if alias in devices:
                raise Failure('device_selection_failed')
            devices[alias] = device_id
            names[alias] = str(item.get('name') or item.get('desc') or alias)
        with self.owner.lock:
            self.owner.selectors = {a: 'id:' + i for a, i in devices.items()}
            self.owner.names = names
            self.owner.discovered_at = time.monotonic()
        return devices

class AutoExporter(Exporter):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.lock = threading.RLock()
        self.poll_interval = 2.0
        self.max_age = 60.0
        self.history = {}
        self.fetch_up = {}
        self.shutdown = threading.Event()
        self.worker = None
        self.names = {}
        self.discovered_at = 0
        self.device_retry = {}
        self.cloud = DiscoverCloud(cfg['timeout'], self)

    def refresh(self):
        try:
            if not self.attempted:
                self.attempted = True
                try:
                    self.key = self.cloud.login(self.username, self.password)
                finally:
                    self.username = self.password = None
            if self.devices is None or time.monotonic() - self.discovered_at >= 300:
                self.devices = self.cloud.resolve(self.key, self.selectors)
            with self.lock:
                selected = set(self.devices)
                self.cache = {a: v for a, v in self.cache.items() if a in selected}
                self.history = {a: v for a, v in self.history.items() if a in selected}
                self.fetch_up = {a: v for a, v in self.fetch_up.items() if a in selected}
            errors = []
            for alias, device_id in self.devices.items():
                retry_at, count, reason = self.device_retry.get(alias, (0, 0, 'none'))
                if time.monotonic() < retry_at:
                    errors.append(reason)
                    continue
                try:
                    sample = self.cloud.detail(self.key, device_id)
                    with self.lock:
                        self.cache[alias] = sample
                        self.fetch_up[alias] = 1
                        history = self.history.setdefault(alias, deque())
                        history.append(sample)
                        self.prune_history()
                    self.device_retry.pop(alias, None)
                except Failure as exc:
                    if exc.code in ('authentication_failed', 'device_identity_mismatch', 'rate_limited'):
                        raise
                    with self.lock:
                        self.fetch_up[alias] = 0
                    count += 1
                    delay = min(900, self.poll_interval * 2 ** min(count - 1, 8))
                    self.device_retry[alias] = (time.monotonic() + delay, count, exc.code)
                    errors.append(exc.code)
            with self.lock:
                self.error = errors[0] if errors else 'none'
                self.errors += int(bool(errors))
                self.failures = self.failures + 1 if errors else 0
                self.due = time.monotonic() + self.poll_interval
        except Exception as exc:
            code = exc.code if isinstance(exc, Failure) else 'internal_error'
            delay = exc.retry if isinstance(exc, Failure) else 0
            with self.lock:
                self.error = code
                self.fetch_up = {a: 0 for a in self.selectors}
                self.errors += 1
                self.failures += 1
                self.stopped = self.key is None or code in ('authentication_failed', 'device_selection_failed', 'device_identity_mismatch')
                if self.stopped:
                    self.key = None
                self.due = time.monotonic() + max(delay, min(900, self.poll_interval * 2 ** min(self.failures - 1, 8)))
        finally:
            with self.lock:
                self.busy = False

    def prune_history(self):
        cutoff = time.monotonic() - self.max_age
        for history in self.history.values():
            while history and history[0]['mono'] < cutoff:
                history.popleft()

    def start(self):
        if self.worker is not None:
            return
        self.worker = threading.Thread(target=self.run, name='cloud-poller', daemon=True)
        self.worker.start()

    def run(self):
        next_cycle = time.monotonic()
        while not self.shutdown.is_set():
            with self.lock:
                stopped, due = self.stopped, self.due
            if stopped:
                self.shutdown.wait(1)
                continue
            wait = max(0, next_cycle - time.monotonic(), due - time.monotonic())
            if self.shutdown.wait(wait):
                break
            started = time.monotonic()
            with self.lock:
                self.busy = True
            self.refresh()
            next_cycle = started + self.poll_interval
            # Successful cycles follow their start-time cadence; global failures retain backoff.
            with self.lock:
                if self.error == 'none':
                    self.due = next_cycle

    def metrics(self):
        with self.lock:
            now = time.monotonic()
            self.prune_history()
            names, selectors, fetch_up = dict(self.names), dict(self.selectors), dict(self.fetch_up)
            cache, error, busy, stopped = dict(self.cache), self.error, self.busy, self.stopped
            errors, wait = self.errors, max(0, self.due - now) if error not in ("none", "not_fetched") else 0
        lines = []
        def gauge(name, help_text, samples):
            lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
            lines.extend(f"{name}{tags} {value}" for tags, value in samples)
        fresh = {a: s for a, s in cache.items() if now - s["mono"] <= self.max_age}
        gauge("andlink_exporter_up", "Latest cloud cycle succeeded and cached responses have not expired; not physical sample freshness.",
              [("", int(error == "none" and len(fresh) == len(selectors)))])
        gauge("andlink_exporter_refresh_in_progress", "One serialized cloud refresh is in flight.", [("", int(busy))])
        gauge("andlink_exporter_stopped", "Collection stopped; manual investigation and restart required.", [("", int(stopped))])
        gauge("andlink_exporter_retry_after_seconds", "Minimum remaining local backoff; restart does not bypass vendor limits.", [("", wait)])
        gauge("andlink_exporter_error", "Fixed local error reason; contains no upstream message.", [(f'{{reason="{error}"}}', 1)])
        lines.extend(("# HELP andlink_exporter_refresh_errors_total Failed refresh cycles.", "# TYPE andlink_exporter_refresh_errors_total counter", f"andlink_exporter_refresh_errors_total {errors}"))
        raw, connected, fetched, available = [], [], [], []
        for alias in selectors:
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
        gauge("andlink_device_info", "Auto-discovered socket display name.",
              [(f'{{device="{label(a)}",name="{label(n)}"}}', 1) for a, n in names.items()])
        gauge("andlink_device_data_age_seconds", "Age of last successful cloud response, not device measurement time.",
              [(f'{{device="{label(a)}"}}', max(0, now - sample["mono"])) for a, sample in cache.items()])
        gauge("andlink_device_fetch_up", "Last cloud read succeeded; cached values can remain on failure for 60 seconds.",
              [(f'{{device="{label(a)}"}}', fetch_up.get(a, 0)) for a in selectors])
        gauge("andlink_device_cache_stale", "Cached value follows a failed read or is older than the polling interval.",
              [(f'{{device="{label(a)}"}}', int(fetch_up.get(a, 0) == 0 or now - sample["mono"] > 2 * self.poll_interval)) for a, sample in fresh.items()])
        return "\n".join(lines) + "\n"

if __name__ == '__main__':
    import threading
    cfg, check = configuration()
    if check:
        print('Configuration valid; no network access.')
    else:
        server = Server((cfg['listen'], cfg['port']), Handler)
        server.exporter = AutoExporter(cfg)
        server.exporter.start()
        print('Auto-discovery exporter ready; discovery interval 300s, poll scheduling 2s, memory retention 60s.', flush=True)
        server.serve_forever()
