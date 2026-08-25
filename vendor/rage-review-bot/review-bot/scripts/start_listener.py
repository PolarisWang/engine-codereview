# -*- coding: utf-8 -*-
"""Start lark-cli event +subscribe as a background process.

Writes events as JSON files to cfg/events/ directory.
Saves PID to cfg/listener.pid for lifecycle management.

Two launch modes:

* default (``python start_listener.py``) — fallback path. Uses
  ``subprocess_util.detached_popen`` with ``DETACHED_PROCESS |
  CREATE_NO_WINDOW`` so the lark-cli process survives the parent shell
  and the Claude Code session. Required when Claude Code's
  ``Bash run_in_background`` opens a visible console window for
  ``node.exe`` (historical anthropics/claude-code#14828 popup).

* ``--foreground`` — preferred path on Claude Code 2.1.128+. Does the
  same setup (kill old listener, rotate logs, write the PID file), then
  ``execvp``s into the lark-cli command so the caller's
  ``Bash run_in_background`` task IS the listener. No detached spawn,
  no console popup. Lifetime is tied to the Claude Code session — on
  ``/clear`` or session exit the listener dies, and the next ``start``
  respawns it.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subprocess_util


def _setup(foreground=False):
    """Common pre-launch work: log rotation, dir creation, kill old PID,
    return (cmd_args, log_file, cfg_dir, pid_file). When foreground=True
    the caller is responsible for writing its own PID after exec; in the
    detached path the parent records the child PID after Popen."""
    script_dir = Path(__file__).resolve().parent
    skill_dir = script_dir.parent
    cfg_dir = skill_dir / "cfg"
    events_dir = cfg_dir / "events"
    pid_file = cfg_dir / "listener.pid"

    events_dir.mkdir(parents=True, exist_ok=True)

    for logname in ("listener.log", "listener.err"):
        log_path = cfg_dir / logname
        if not log_path.exists():
            continue
        try:
            if log_path.stat().st_size > 5_242_880:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
                log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    old_pid = subprocess_util.read_pid_file(str(pid_file))
    if old_pid is not None:
        subprocess_util.kill_process(old_pid)

    # Going through lark-cli.exe directly (not `node run.js`) keeps the
    # whole process tree windowless — see subprocess_util.lark_cli_argv_prefix.
    cmd_args = subprocess_util.lark_cli_argv_prefix() + [
        "event", "+subscribe",
        "--event-types", "im.message.receive_v1,im.message.recalled_v1",
        "--as", "bot", "--force",
        "--output-dir", "events",
    ]
    log_file = str(cfg_dir / "listener.log")
    return cmd_args, log_file, cfg_dir, pid_file, events_dir


def _run_foreground(cmd_args, log_file, cfg_dir, pid_file, events_dir):
    """Foreground mode: stamp our own PID, redirect stdio to the log
    file, then execvp into lark-cli. The current process becomes the
    listener — Claude Code's ``Bash run_in_background`` task tracks it
    directly, no detached_popen / no console popup."""
    subprocess_util.write_pid_file(str(pid_file), pid=os.getpid())

    sys.stdout.write(f"Listener foreground PID {os.getpid()}\n")
    sys.stdout.write(f"Events directory: {events_dir}\n")
    sys.stdout.flush()

    os.chdir(str(cfg_dir))
    log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)  # stdout -> listener.log
    os.dup2(log_fd, 2)  # stderr -> listener.log (lark-cli logs to stderr)
    os.close(log_fd)
    os.execvp(cmd_args[0], cmd_args)
    # execvp does not return on success
    return 1


def _run_detached(cmd_args, log_file, cfg_dir, pid_file, events_dir):
    """Fallback: spawn lark-cli as a detached, console-less process."""
    with open(log_file, "ab") as lf:
        popen_kwargs = dict(stdout=lf, stderr=subprocess.STDOUT, cwd=str(cfg_dir))
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        proc = subprocess_util.detached_popen(cmd_args, **popen_kwargs)

    subprocess_util.write_pid_file(str(pid_file), pid=proc.pid)
    print(f"Listener started with PID {proc.pid}")
    print(f"Events directory: {events_dir}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--foreground", action="store_true",
        help="Replace this process with lark-cli (preferred when invoked "
             "via Claude Code Bash run_in_background; no console popup).")
    args = parser.parse_args()

    cmd_args, log_file, cfg_dir, pid_file, events_dir = _setup(
        foreground=args.foreground)

    if args.foreground:
        return _run_foreground(cmd_args, log_file, cfg_dir, pid_file, events_dir)
    return _run_detached(cmd_args, log_file, cfg_dir, pid_file, events_dir)


if __name__ == "__main__":
    sys.exit(main())
