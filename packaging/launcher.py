"""Entry point for the stand-alone ReadyStamp app (Windows .exe / macOS build).

Double-clicking the app starts the local web screen and opens the browser.
Passing arguments runs the normal command line instead, for example:
    ReadyStamp.exe run --input VOL001.opt --output out
"""
import multiprocessing
import sys


def _utf8_console() -> None:
    # Windows consoles default to an old code page that cannot print
    # characters such as "↔" used in QC messages.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    multiprocessing.freeze_support()
    _utf8_console()
    if len(sys.argv) > 1:
        from readystamp.cli import app

        app()
        return

    from readystamp.server.app import serve

    print("Starting ReadyStamp Lite. Your browser will open in a moment.")
    print("Keep this window open while you work. Close it to quit.")
    try:
        serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
