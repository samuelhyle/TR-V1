#!/usr/bin/env python3
"""Render and install the launchd agent.

The repository's plist ships with `__REPO__` placeholders because the absolute
path of the checkout is not knowable in version control. Nothing substituted
them, so the installed agent pointed at a directory that no longer existed and
exited 126 on every fire. Substitution happens here, once, at install time,
and `install` refuses to write a job whose paths do not exist.

    python3 bin/install_scheduler.py --render      # show the plist
    python3 bin/install_scheduler.py --install     # write and load it
    python3 bin/install_scheduler.py --uninstall
"""

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "bin" / "com.toriresearch.collect.plist"
LABEL = "com.toriresearch.collect"
INSTALLED = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def render(interval=None, env=None):
    return render_plist(interval, env)


def render_plist(interval=None, env=None):
    interval = 3600 if interval is None else int(interval)
    if interval < 900:
        raise ValueError("collection interval must be at least 900 seconds")
    runtime = {"TORI_PYTHON": sys.executable, "PYTHONUNBUFFERED": "1", "TORI_RUN_ORIGIN": "scheduled"}
    if interval != 3600:
        runtime['TORI_COLLECTION_INTERVAL_SECONDS'] = str(interval)
    runtime.update(env or {})
    # One cadence drives launchd and the durable coordinator, even for overrides.
    if interval != 3600 or 'TORI_COLLECTION_INTERVAL_SECONDS' in runtime:
        runtime['TORI_COLLECTION_INTERVAL_SECONDS'] = str(interval)
    payload = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(ROOT / "bin" / "collect.sh")],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": False,
        "StandardOutPath": str(ROOT / "data" / "launchd.out.log"),
        "StandardErrorPath": str(ROOT / "data" / "launchd.err.log"),
        "EnvironmentVariables": runtime,
    }
    if interval <= 3600 and 3600 % interval == 0 and interval % 60 == 0:
        minutes = list(range(0, 60, interval // 60))
        payload["StartCalendarInterval"] = ({"Minute":0} if len(minutes) == 1 else [{"Minute":m} for m in minutes])
    else:
        payload["StartInterval"] = interval
    return plistlib.dumps(payload, sort_keys=True).decode()


def _env_from_local_file():
    """Read TORI_*/EBAY_* out of config/local.env for the job's environment.

    Passing `--env TORI_CONTACT_EMAIL=...` puts a deliverable address into shell
    history and into every process listing for the length of the install. The
    same value is already in the gitignored file `collect.sh` sources, so the
    installer reads it from there by default. An explicit --env still wins.
    """
    path = ROOT / "config" / "local.env"
    if not path.is_file():
        return {}
    found = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith(("TORI_", "EBAY_")):
            found[key] = value.strip().strip('"').strip("'")
    return found


def check_paths():
    """Refuse to install a job that cannot possibly run."""
    problems = []
    if not (ROOT / "bin" / "collect.sh").exists():
        problems.append(f"missing {ROOT / 'bin' / 'collect.sh'}")
    if not (ROOT / "toriresearch" / "cli.py").exists():
        problems.append(f"missing {ROOT / 'toriresearch' / 'cli.py'}")
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    if ROOT == Path.home() / "Desktop" or Path.home() / "Desktop" in ROOT.parents:
        problems.append(
            "checkout lives in ~/Desktop; macOS TCC blocks launchd from reading it. "
            "Move the repository out of Desktop before installing."
        )
    if problems:
        for p in problems:
            print(f"  ERROR {p}", file=sys.stderr)
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    action = ap.add_mutually_exclusive_group()
    action.add_argument("--render", action="store_true")
    action.add_argument("--install", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    ap.add_argument("--interval", type=int, default=None)
    ap.add_argument("--env", action="append", default=[],
                    help="KEY=VALUE passed through to the job. Defaults to "
                         "config/local.env so a contact address never has to "
                         "appear on the command line.")
    args = ap.parse_args()
    env = _env_from_local_file()
    for pair in args.env:
        if "=" in pair:
            k, v = pair.split("=", 1)
            env[k] = v
    args.interval = int(args.interval if args.interval is not None else env.get('TORI_COLLECTION_INTERVAL_SECONDS',3600))
    if args.interval < 900:
        ap.error("--interval must be at least 900 seconds")
    if args.interval != int(env.get('TORI_COLLECTION_INTERVAL_SECONDS',3600)):
        env['TORI_COLLECTION_INTERVAL_SECONDS'] = str(args.interval)

    if args.render:
        print(render_plist(args.interval, dict(pair.split("=", 1) for pair in args.env if "=" in pair)))
        return 0

    if args.uninstall:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                       capture_output=True)
        if INSTALLED.exists():
            INSTALLED.unlink()
        print(f"removed {INSTALLED}")
        return 0

    if args.install:
        if not check_paths():
            return 1
        if not env.get("TORI_CONTACT_EMAIL"):
            print("  ERROR no TORI_CONTACT_EMAIL in config/local.env; "
                  "add it there or pass --env TORI_CONTACT_EMAIL=you@example.com",
                  file=sys.stderr)
            return 1
        explicit = {}
        for pair in args.env:
            if "=" in pair:
                k, v = pair.split("=", 1)
                explicit[k] = v
        if args.interval != int(_env_from_local_file().get('TORI_COLLECTION_INTERVAL_SECONDS',3600)):
            explicit['TORI_COLLECTION_INTERVAL_SECONDS'] = str(args.interval)
        # Only values given on the command line go into the plist. The rest are
        # read from config/local.env by collect.sh at run time, so a contact
        # address does not get copied out of the gitignored file into a second
        # file that is not gitignored.
        INSTALLED.parent.mkdir(parents=True, exist_ok=True)
        python = Path(explicit.get("TORI_PYTHON", sys.executable))
        if not python.is_absolute() or not os.access(python, os.X_OK):
            print("  ERROR TORI_PYTHON must be an executable absolute path", file=sys.stderr)
            return 1
        # Reinstalling must not terminate a collector already holding the lock.
        sys.path.insert(0, str(ROOT))
        from toriresearch import db, local_runtime
        locks = (local_runtime.active_locks() if local_runtime.owner() and not local_runtime.in_container()
                 else db.active_locks())
        if any(name in locks for name in ("collection", "collection-source", "sweep", "itempage", "enrich")):
            print("  ERROR collection is active; reinstall after this run finishes", file=sys.stderr)
            return 1
        INSTALLED.write_text(render_plist(args.interval, explicit or None))
        INSTALLED.chmod(0o600)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                       capture_output=True)
        result = subprocess.run(["launchctl", "bootstrap",
                                 f"gui/{os.getuid()}", str(INSTALLED)],
                                capture_output=True, text=True)
        print(f"installed {INSTALLED}")
        if result.returncode != 0:
            print(f"  launchctl bootstrap failed: {result.stderr.strip()}", file=sys.stderr)
            return result.returncode
        schedule = (f"every {args.interval // 60} minutes on calendar boundaries" if args.interval <= 3600 and 3600 % args.interval == 0
                    else f"interval {args.interval}s")
        print(f"  {schedule}; waits for the next scheduled time, path {ROOT / 'bin' / 'collect.sh'}")
        print(f"  {len(env)} value(s) available to collect.sh via config/local.env")
        print("  verify:  launchctl list com.toriresearch.collect")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
