"""The application. This is what the packaged exe runs.

    CannaCabana.exe                 start it
    CannaCabana.exe --no-tunnel     local and LAN only
    CannaCabana.exe --set-password  change the admin password
    CannaCabana.exe --selftest      prove it works, touching nothing

A foreground console app on purpose. The index build it exists to run takes
between 3 and 49 minutes depending on the province, and progress for that
belongs in front of you, not in a log file you would have to go and find.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

# Three things at once:
#   utf-8          the banner and store names should not become mojibake
#   errors=replace a store name with an accent must never kill a worker thread
#                  on a cp1252 console
#   line_buffering Python block-buffers stdout the moment it is a pipe rather
#                  than a terminal, so a wrapper, a log capture, or a service
#                  manager would see nothing at all for minutes. For an app
#                  whose entire point is watchable progress, that is a bug.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace",
                            line_buffering=True)
    except (AttributeError, ValueError):
        pass

BANNER_W = 62


def lan_ip() -> str:
    """Best-guess LAN address, so the phone URL can be printed."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="CannaCabana",
                                description="Canna Cabana stock, as a server")
    p.add_argument("--port", type=int, default=None,
                   help="override the saved port")
    p.add_argument("--no-tunnel", action="store_true",
                   help="local and LAN only, no public URL")
    p.add_argument("--no-browser", action="store_true",
                   help="do not open a browser window on start")
    p.add_argument("--set-password", nargs="?", const="", metavar="PASSWORD",
                   help="set the admin password and exit; omit the value to "
                        "be prompted (this is what 'Set password.bat' does)")
    p.add_argument("--selftest", action="store_true",
                   help="run the self-test and exit")
    return p.parse_args(argv)


def run_selftest() -> int:
    """Run selftest.py in a clean process, so it can sandbox its data dir."""
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "--selftest-child"]
    else:
        argv = [sys.executable, os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "selftest.py")]
    return subprocess.call(argv)


def row(label: str, value: str) -> None:
    print(f"  {label:<14}:  {value}")


def have_console() -> bool:
    """Is there a real console we can prompt on?

    `sys.stdin.isatty()` is not trustworthy here: under Git Bash with stdin
    redirected from /dev/null it still reports True. GetConsoleMode only
    succeeds on an actual console handle, which is the thing Windows getpass
    needs, so ask that instead.
    """
    if os.name != "nt":
        return bool(sys.stdin and sys.stdin.isatty())
    import ctypes

    STD_INPUT_HANDLE = -10
    k32 = ctypes.windll.kernel32
    mode = ctypes.c_ulong()
    return bool(k32.GetConsoleMode(k32.GetStdHandle(STD_INPUT_HANDLE),
                                   ctypes.byref(mode)))


def prompt_password() -> str | None:
    """Ask for a new password, twice, without echoing it.

    Exists because a flag you have to type at a command line is the wrong
    recovery path for an app people launch by double-clicking. 'Set
    password.bat' next to the exe runs this.
    """
    import getpass

    # Windows getpass reads the console device directly rather than stdin, so
    # with no real console it blocks forever instead of seeing EOF. Refuse up
    # front and name the form that works without one.
    if not have_console():
        print("  No interactive console here, so there is nothing to type "
              "into.")
        print("  Pass the password directly instead:\n")
        print('      CannaCabana.exe --set-password "your-password"\n')
        return None

    print("=" * BANNER_W)
    print("  Set the admin password")
    print("=" * BANNER_W)
    print("  This gates province rebuilds and live stock checks.")
    print("  Searching stays open to anyone with the link.")
    print("  Anyone already signed in will be signed out.\n")

    for attempt in range(3):
        try:
            first = getpass.getpass("  New password : ").strip()
            again = getpass.getpass("  Type it again: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled. Nothing changed.")
            return None
        if len(first) < 6:
            print("  Too short - use at least 6 characters.\n")
            continue
        if first != again:
            print("  Those two did not match.\n")
            continue
        return first
    print("  Three tries, no match. Nothing changed.")
    return None


def main(argv=None) -> int:
    args = parse_args(argv)

    import auth

    # `is not None` rather than truthiness: `--set-password` with no value
    # means "ask me", and argparse hands that over as an empty string.
    if args.set_password is not None:
        import paths

        new = args.set_password or prompt_password()
        if not new:
            return 1
        auth.set_password(new)
        print("\n  Password updated. Anyone signed in was signed out.")
        print(f"  Stored (hashed) in {paths.data('settings.json')}")
        print("  The running app, if any, picks this up on the next attempt.")
        return 0

    if args.selftest:
        return run_selftest()

    import paths
    import jobs
    import server
    import tunnel as tunnel_mod
    import uvicorn

    settings = auth.load()
    port = args.port or int(settings["port"])
    generated = auth.ensure_configured()

    # Job progress goes to this console, like everything else.
    jobs.echo = print
    jobs.start()

    # Serve first, so the tunnel does not spend its first seconds proxying to
    # a closed port.
    conf = uvicorn.Config(server.app, host="0.0.0.0", port=port,
                          log_level="warning")
    srv = uvicorn.Server(conf)
    web = threading.Thread(target=srv.run, name="uvicorn", daemon=True)
    web.start()
    while not srv.started and web.is_alive():
        time.sleep(0.05)
    if not web.is_alive():
        print(f"\nCould not bind port {port}. Something else is using it — "
              f"try --port 8080.")
        return 1

    print("=" * BANNER_W)
    print("  Canna Cabana stock")
    print("=" * BANNER_W)
    # 127.0.0.1, not "localhost": we bind IPv4, and on Windows
    # localhost resolves to ::1 first, which costs a ~2s stall per
    # request before the client falls back.
    row("This computer", f"http://127.0.0.1:{port}")
    row("Phone / LAN", f"http://{lan_ip()}:{port}")

    tun = None
    mode = settings.get("tunnel", "quick")
    if args.no_tunnel or mode == "off":
        row("Public", "off (--no-tunnel)")
    else:
        tun = tunnel_mod.Tunnel(echo=print)
        if not tun.start(port, mode, settings.get("tunnel_token", "")):
            row("Public", f"unavailable — {tun.error}")
            tun = None
        else:
            print(f"  {'Public':<14}:  starting {mode} tunnel...")
            url = tun.wait_for_url()
            if url:
                row("Public", url)
            else:
                row("Public", tun.error or "no URL yet — watch for it below")

    # Name the recovery path that actually exists here. In the packaged app
    # that is a file you can double-click; from source it is the flag.
    how = ('run "Set password.bat" to change' if getattr(sys, "frozen", False)
           else "python app.py --set-password")
    if generated:
        row("Admin password", f"{generated}    <-- write this down")
        row("", f"lost it? {how}")
    else:
        row("Admin password", f"already set  ({how})")
    row("Data", str(paths.DATA_DIR))
    import buildinfo
    build_line = buildinfo.summary()
    if build_line:
        row("Build", build_line)
    print("-" * BANNER_W)
    print("  Reading is open to anyone with the link.")
    print("  Refreshing a province needs the password.")
    print("\n  Ctrl-C to stop.\n")

    if settings.get("open_browser", True) and not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{port}")

    try:
        while web.is_alive():
            web.join(0.5)
            if server.rebuild_requested():
                print("\n  Rebuild requested from the page -- stopping to "
                      "hand off to the new build...")
                break
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        srv.should_exit = True
        if tun:
            tun.stop()
        running = [j for j in jobs.active()]
        if running:
            print(f"  {len(running)} job(s) were still running; an index run "
                  f"resumes with the Resume button next start.")
        web.join(timeout=5)
    print("Stopped.")
    return 0


if __name__ == "__main__":
    # The frozen exe re-invokes itself for the self-test, because the sandbox
    # has to be chosen before anything imports paths.
    if "--selftest-child" in sys.argv:
        import shutil

        import selftest
        try:
            sys.exit(selftest.main())
        finally:
            shutil.rmtree(selftest._SANDBOX, ignore_errors=True)
    sys.exit(main())
