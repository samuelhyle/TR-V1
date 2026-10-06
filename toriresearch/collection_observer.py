"""Independent local freshness observation; never fetches listings or runs inference."""

import json
import signal
import threading

from . import collection_health, config
from .fileio import write_text


def observe():
    result = collection_health.freshness()
    result['context'] = collection_health.context()
    write_text(config.REPORTS_DIR/'collection_observer.json', json.dumps(result,indent=2,default=str)+'\n')
    return result


def run(stop=None):
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_args: stop.set())
        signal.signal(signal.SIGINT, lambda *_args: stop.set())
    while not stop.is_set():
        try:
            result = observe()
            print(json.dumps({'observer':result['status'],'checked_at':result['checked_at']}),flush=True)
        except Exception as exc:
            print(json.dumps({'observer':'error','error_class':type(exc).__name__}),flush=True)
        stop.wait(config.COLLECTION_OBSERVER_SECONDS)
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
