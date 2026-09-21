"""JSONL shell protocol; no arguments means read-only preflight, never start."""

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from . import RuntimeFailure
from .resources import Bundle, Layout
from .supervisor import Events, Supervisor


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, default=Path("runtime/windows-x64"))
    parser.add_argument("--instance", type=Path)
    parser.add_argument("--isolation-root", type=Path,
                        help="explicit repository-local .desktop-runtime-tests root for packaged bootstrap")
    parser.add_argument("--start", action="store_true", help="start or reopen a repository-local isolated instance")
    parser.add_argument("--desktop", action="store_true",
                        help="controlled packaged shell launch; auto-authorize the verified owned instance")
    parser.add_argument("--recover", action="store_true", help="explicitly retry an unclean existing PG16 instance")
    parser.add_argument("--enable-writes-for-instance", help="explicit per-launch opt-in for the persisted instance ID")
    args = parser.parse_args(argv)
    events = Events(sys.stdout)
    supervisor = None
    stopping = False

    def stop_signal(_signum, _frame):
        nonlocal stopping
        stopping = True

    old_signals = {}
    try:
        bundle = Bundle.load(args.resources)
        events.emit("verified", "resources", release_accepted=False)
        if not args.start:
            events.emit("completed", "preflight", dry_run=True, started=False)
            return 0
        if args.instance is None:
            raise RuntimeFailure("isolated_instance_required")
        if args.desktop and args.isolation_root is None:
            raise RuntimeFailure("desktop_isolation_root_required")
        repo = Path(__file__).resolve().parents[2]
        if args.isolation_root is not None:
            isolation_root = args.isolation_root.absolute()
            if isolation_root.name != ".desktop-runtime-tests" or isolation_root != isolation_root.resolve():
                raise RuntimeFailure("invalid_isolation_root")
            repo = isolation_root.parent
        layout = Layout(bundle.root, args.instance.resolve())
        supervisor = Supervisor(bundle, layout, repo, events, recover=args.recover,
                                enable_writes_for_instance=args.enable_writes_for_instance,
                                desktop=args.desktop)
        supervisor.shell_token = os.environ.get("RECRUITOPS_DESKTOP_SHELL_TOKEN")
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_signals[signum] = signal.signal(signum, stop_signal)
        supervisor.start()
        commands = queue.Queue(maxsize=32)
        threading.Thread(target=read_commands, args=(sys.stdin, commands), daemon=True).start()
        while not stopping:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                command = None
            if command == "stop":
                break
            if command == "backup":
                supervisor.backup()
            elif command == "invalid":
                events.emit("rejected", "control", code="invalid_command")
            supervisor.tick()
            time.sleep(1)
        return 0
    except RuntimeFailure as exc:
        if supervisor:
            supervisor.failed = True
        diagnostics = {key: value for key, value in {"exit_code": exc.exit_code, "os_error": exc.os_error}.items() if value is not None}
        events.emit("failed", supervisor.stage if supervisor else "preflight", code=exc.code, **diagnostics)
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        if supervisor:
            supervisor.failed = True
        # Never emit arbitrary exception text: it can contain a DSN or credential.
        events.emit("failed", supervisor.stage if supervisor else "preflight", code="runtime_io_or_contract_error")
        return 2
    finally:
        shutdown_failed = False
        if supervisor:
            try:
                supervisor.stop()
            except (RuntimeFailure, OSError, ValueError, TypeError):
                shutdown_failed = True
                events.emit("failed", "runtime", code="owned_shutdown_failed")
        for signum, handler in old_signals.items():
            signal.signal(signum, handler)
        if shutdown_failed:
            return 2


def read_commands(stream, commands):
    """Private child stdin only; EOF stops an orphaned shell child."""
    while True:
        line = stream.readline(4097)
        if not line:
            commands.put("stop")
            return
        try:
            value = json.loads(line)
            command = value.get("command") if isinstance(value, dict) and len(value) == 1 else None
        except ValueError:
            command = None
        if len(line) > 4096:
            commands.put("stop")
            return
        commands.put(command if isinstance(command, str) and command in {"stop", "backup"} else "invalid")
        if command == "stop":
            return


if __name__ == "__main__":
    raise SystemExit(main())
