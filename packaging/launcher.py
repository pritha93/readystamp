"""Entry point for the stand-alone ReadyStamp app (Windows .exe / macOS build).

Double-clicking the app starts the local web screen and opens the browser.
Passing arguments runs the normal command line instead, for example:
    ReadyStamp.exe run --input VOL001.opt --output out
"""
import multiprocessing
import sys


def main() -> None:
    multiprocessing.freeze_support()
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
