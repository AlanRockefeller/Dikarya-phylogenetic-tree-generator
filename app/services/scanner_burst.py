"""Bounded, per-process backup for the shared missing-route burst counter."""

from collections import OrderedDict
from threading import Lock
from time import monotonic


class ScannerBurstFallback:
    def __init__(self, window=60, max_clients=4096):
        self.window = window
        self.max_clients = max_clients
        self.entries = OrderedDict()
        self.lock = Lock()

    def count(self, key, increment=False):
        with self.lock:
            now = monotonic()
            expires, count = self.entries.pop(key, (now + self.window, 0))
            if expires <= now:
                expires, count = now + self.window, 0
            count += int(increment)
            if count:
                self.entries[key] = (expires, count)
            while len(self.entries) > self.max_clients:
                self.entries.popitem(last=False)
            return count


# Increment and expiry must be atomic: a timeout between INCR and EXPIRE
# otherwise leaves a permanent counter. Also repair legacy non-expiring keys.
INCREMENT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""
