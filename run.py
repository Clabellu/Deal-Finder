#!/usr/bin/env python3
"""Avvia Deal Finder con interfaccia grafica."""

import sys
import os

# Assicura che la working directory sia quella dello script
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from gui.app import run

if __name__ == "__main__":
    run()
