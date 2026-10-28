"""Detects a broken unzip before it turns into a confusing traceback.

On Windows, folder names are case-insensitive, so extracting the project zip
(top folder "SheetGuard") into an existing "SheetGuard" folder merges the
project's top level into the package folder "sheetguard": the cloud entry point
server.py overwrites sheetguard/server.py and README.md, tests/, web/ ... appear
inside the package.
"""
from __future__ import annotations

import os

PKG = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG)
_FOREIGN = ("README.md", "CHECK_SETUP.bat", "START_SheetGuard.bat", "requirements.txt", "tests", "web")


def install_problem() -> str | None:
    """A one-paragraph explanation if the installation is damaged, else None."""
    foreign = [f for f in _FOREIGN if os.path.exists(os.path.join(PKG, f))]
    server_ok = True
    try:
        with open(os.path.join(PKG, "server.py"), encoding="utf-8") as fh:
            server_ok = "class Handler" in fh.read()
    except OSError:
        server_ok = False
    if not foreign and server_ok:
        return None
    return (f"The project files are mixed up: the zip was extracted into the existing project "
            f"folder, so a second copy landed inside the code folder '{PKG}'"
            + (f" (found {', '.join(foreign)} there)" if foreign else "")
            + ("" if server_ok else " and sheetguard/server.py was overwritten")
            + ". Fix: delete this whole project folder, then right-click the zip > Extract All "
              "into an EMPTY folder (e.g. Documents\\class\\cd) and open the folder it creates.")
