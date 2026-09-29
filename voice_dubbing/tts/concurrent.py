"""Bounded orchestration and isolated adapter instances; no provider HTTP logic."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from ..errors import ValidationError


def validate_concurrency(value):
    if type(value) is not int or not 1 <= value <= 8:
        raise ValidationError("TTS concurrency must be an integer between 1 and 8")
    return value


class IsolatedTTSProvider:
    """Each call owns its adapter/counters; diagnostics are merged under a lock."""

    def __init__(self, prototype, factory):
        self.prototype = prototype
        self.factory = factory
        self.lock = Lock()
        self.cache_hits = self.provider_calls = self.supplier_requests = 0
        self.cache_events = []
        self.warnings = []
        self._sentence_locks = {}
        self._failed_sentences = {}

    def __getattr__(self, name):
        return getattr(self.prototype, name)

    def synthesize(self, request, *, output_dir):
        key = (request.text, request.voice_id, request.language)
        with self.lock:
            sentence_lock = self._sentence_locks.setdefault(key, Lock())
        with sentence_lock:
            if key in self._failed_sentences:
                with self.lock:
                    self.cache_events.append({"segment_id": request.segment_id, "cache_hit": False,
                        "generated": False, "generation_attempted": False, "supplier_requests": 0,
                        "status": "failed"})
                raise self._failed_sentences[key]
            try:
                return self._synthesize(request, output_dir=output_dir)
            except Exception as exc:
                self._failed_sentences[key] = exc
                raise

    def _synthesize(self, request, *, output_dir):
        worker = self.factory()
        succeeded = False
        try:
            result = worker.synthesize(request, output_dir=output_dir)
            succeeded = True
            return result
        finally:
            with self.lock:
                self.cache_hits += getattr(worker, "cache_hits", 0)
                self.provider_calls += getattr(worker, "provider_calls", 1)
                self.supplier_requests += getattr(worker, "supplier_requests",
                    getattr(worker, "synthesis_http_requests", 1))
                self.warnings.extend(getattr(worker, "warnings", []))
                self.cache_events.extend(getattr(worker, "cache_events", [{
                    "segment_id": request.segment_id, "cache_hit": False,
                    "generated": succeeded, "generation_attempted": True,
                    "supplier_requests": getattr(worker, "synthesis_http_requests", 1),
                    "status": "succeeded" if succeeded else "failed",
                }]))


def ordered_synthesis(items, synthesize, concurrency):
    """At most N outstanding requests; yield in subtitle order, stop on failure.

    Already-running paid requests finish and retain their files/diagnostics.
    No further requests are dispatched after a worker fails.
    """
    validate_concurrency(concurrency)
    if concurrency == 1:
        for item in items:
            yield synthesize(item)
        return
    stopped = Event()

    def work(item):
        if stopped.is_set():
            return None
        try:
            return synthesize(item)
        except Exception:
            stopped.set()
            raise

    iterator = iter(items)
    pending = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="tts") as pool:
        try:
            for _ in range(concurrency):
                item = next(iterator, None)
                if item is not None:
                    pending.append(pool.submit(work, item))
            while pending:
                result = pending.pop(0).result()
                if not stopped.is_set():
                    item = next(iterator, None)
                    if item is not None:
                        pending.append(pool.submit(work, item))
                # Skipped requests only follow an actual failed future.
                if result is not None:
                    yield result
        finally:
            stopped.set()
            for future in pending:
                future.cancel()
