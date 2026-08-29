"""Run this to pair with a Harmony Hub and print remote button events.

For options (e.g. --address to skip pairing, --verbose for raw packets), run:
    python main.py --help
"""

from harmony_receiver.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
