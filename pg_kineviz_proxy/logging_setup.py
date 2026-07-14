"""Logging setup mirroring the reference proxies: file + error + console streams."""

from __future__ import annotations

import logging
import os


def setup_logging(debug: bool = False) -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    detailed = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s")
    simple = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    fh = logging.FileHandler("logs/pg_kineviz_server.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(detailed)

    eh = logging.FileHandler("logs/pg_kineviz_errors.log")
    eh.setLevel(logging.ERROR)
    eh.setFormatter(detailed)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(simple)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(fh)
    root.addHandler(eh)
    root.addHandler(ch)
    return logging.getLogger("pg_kineviz")
