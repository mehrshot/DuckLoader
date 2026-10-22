"""
Runs the bot's song recognition on a local audio/video file, to check that
Shazam (and ACRCloud, if configured) work on this server.

    cd /home/mahshot/DuckLoaderBot
    /home/mahshot/venv/bin/python tools/find_song.py /path/to/video.mp4
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import song_finder  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2 or not os.path.isfile(sys.argv[1]):
        raise SystemExit("Usage: python tools/find_song.py <audio or video file>")
    print("Recognition available:", song_finder.available())
    print("Result:", song_finder.recognize(sys.argv[1]) or "no song recognized")


if __name__ == "__main__":
    main()
