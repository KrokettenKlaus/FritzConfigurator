"""Startet die grafische Oberfläche (Doppelklick unter Windows, ohne Konsolenfenster)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fritzconf.gui import main  # noqa: E402

if __name__ == "__main__":
    main()
