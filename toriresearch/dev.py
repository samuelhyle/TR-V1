"""Dev supervisor: run the dashboard and restart it when the code changes.

Plain polling of mtimes rather than a file-watch library, because the project
takes a hard dependency position of no third-party packages and FSEvents needs
a watcher on macOS where a stat loop is equally fast at this scale.

Restart rather than hot-reload is the honest choice here. The dashboard holds a
database handle, a robots cache and a rate limiter; swapping those underneath
live requests produces exactly the kind of confusing state that makes a dev loop
slower than restarting. A restart takes well under a second and cannot leave a
stale connection behind.

Bind stays loopback in dev exactly as in production. A dev server reachable on
the network is the same exposure as the production one, and "it's only dev" is
how it ends up in a screenshot.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH_DIRS = ("toriresearch", "bin")
WATCH_SUFFIXES = (".py", ".html", ".yaml")
POLL_SECONDS = 0.4
RESTART_DEBOUNCE = 0.35


def _snapshot():
    out = {}
    for name in WATCH_DIRS:
        base = ROOT / name
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix in WATCH_SUFFIXES and path.is_file():
                try:
                    out[str(path)] = path.stat().st_mtime
                except OSError:
                    continue
    vocab = ROOT / "config" / "vocabulary.yaml"
    if vocab.exists():
        out[str(vocab)] = vocab.stat().st_mtime
    return out


class Supervisor:
    def __init__(self, host="127.0.0.1", port=8787, refresh=10, open_browser=False):
        self.host = host
        self.port = port
        self.refresh = refresh
        self.open_browser = open_browser
        self.child = None
        self.restarts = 0
        self._stop = False

    def _argv(self):
        argv = [sys.executable, "-m", "toriresearch.cli", "dashboard",
                "--host", self.host, "--port", str(self.port),
                "--refresh", str(self.refresh)]
        if self.open_browser:
            argv.append("--open")
        return argv

    def _spawn(self, reason):
        self.restarts += 1
        label = "start" if self.restarts == 1 else f"restart {self.restarts - 1}"
        print(f"[dev] {label}: {reason}", flush=True)
        env = dict(os.environ)
        # Unbuffered, or the child's prints sit in a pipe and nothing appears.
        env["PYTHONUNBUFFERED"] = "1"
        self.child = subprocess.Popen(self._argv(), cwd=str(ROOT), env=env)

    def stop_child(self, sig=signal.SIGTERM):
        if self.child and self.child.poll() is None:
            try:
                self.child.send_signal(sig)
                self.child.wait(timeout=5)
                return
            except Exception:
                pass
            # The graceful path failed. If the kill also fails, the process is
            # still holding the port, and the supervisor would go on to start a
            # replacement that cannot bind -- leaving two servers where one was
            # intended and no explanation for either. Say so instead of
            # restarting into a silent conflict.
            try:
                self.child.kill()
                self.child.wait(timeout=5)
            except Exception:
                print(f"[dev] WARNING: pid {self.child.pid} ignored "
                      f"{getattr(sig, 'name', sig)} and did not respond to SIGKILL either. "
                      f"A replacement may be started and fail to bind the port; if so, "
                      f"terminate pid {self.child.pid} by hand.", flush=True)
                return
            print(f"[dev] pid {self.child.pid} needed SIGKILL after {getattr(sig, 'name', sig)}",
                  flush=True)

    def run(self):
        print(f"[dev] watching {', '.join(WATCH_DIRS)} for "
              f"{'/'.join(WATCH_SUFFIXES)} changes", flush=True)
        print(f"[dev] dashboard  http://{self.host}:{self.port}/", flush=True)
        print("[dev] loopback only, same as production. Ctrl-C to stop.",
              flush=True)

        state = _snapshot()
        self._spawn("cold start")

        try:
            while not self._stop:
                time.sleep(POLL_SECONDS)
                current = _snapshot()
                changed = [p for p in set(state) | set(current)
                           if state.get(p) != current.get(p)]
                if not changed:
                    # A crash should be visible and restartable, not a silent
                    # dead server that keeps answering nothing.
                    if self.child.poll() is not None:
                        print(f"[dev] server exited with {self.child.returncode}; "
                              f"restarting", flush=True)
                        self._spawn("crash")
                    continue

                time.sleep(RESTART_DEBOUNCE)
                state = _snapshot()
                names = sorted(Path(p).name for p in changed)[:6]
                self.stop_child()
                self._spawn(f"changed {', '.join(names)}")
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_child()
            print("[dev] stopped", flush=True)
        return 0


def main(argv=None):
    import argparse

    from . import compliance

    ap = argparse.ArgumentParser(prog="toriresearch dev")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--refresh", type=int, default=10)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args(argv)

    # Same enforcement as the server itself. Dev is not an exemption.
    host = compliance.assert_not_exposed(args.host)
    sup = Supervisor(host=host, port=args.port, refresh=args.refresh,
                     open_browser=args.open)
    signal.signal(signal.SIGTERM, lambda *_: setattr(sup, "_stop", True))
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())