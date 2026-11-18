#!/usr/bin/env python3
"""Verifie que les 10 fichiers de programme n'ont pas ete modifies.

Ce script compare l'empreinte SHA-256 de chaque fichier de programme a la
valeur attendue. Si l'une d'elles differe, le fichier a ete modifie.

    python verifier_integrite.py

Code de sortie : 0 si tout est conforme, 1 sinon.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent
PROGRAMMES = RACINE / "programmes"

# Empreintes des fichiers d'origine. Toute modification les fait echouer.
ATTENDU = {
    "Scanner  MTF GPS/app.py":
        "b6936f54f4a6ab2b1b168777cdabd11087b2dd502061f847f3b81191e82a5f56",
    "Scanner RSI/app.py":
        "1b76c0037e8c0a78d4f2790a5a7a1ce6524929d012fe1c181695b3fc5a35d03e",
    "Scanner CHoCH/app.py":
        "be4c86ef6d010e09254eb5e03e104fb0f5f9a7bd17f6eb42e31cdf57da3bf05b",
    "Scanner Supports & R\u00e9sistances/app.py":
        "7b396e5cd41c265cd836c26d87c3c1ed062aa5c3a3c4f8a74a0427b7bc4fb182",
    "Scanner Supports & R\u00e9sistances/bluestar_core.py":
        "743948aab02ab0306209b861ac25910e08f7a6f2eb7686faf4f970ee980a4e72",
    "Merge/merge_app.py":
        "aecab076d2c6d7a9a3c6810db29d2f81da48791da01f0f06192b979ee51dac56",
    "BLS V10 Pro/app.py":
        "7ceeb61a106626e56ebfedbfc42ca328bbf316702c263265ec56e9e27b2b8ff1",
    "BLS V10 Pro/ENGINE.V10.py":
        "c62830d9f9d06086dc4c3bd0fdded7e6d7977845eefa177a3ccfdf5180791b7a",
    "Calendrier/app.py":
        "6b6c0394379a8d8f22ada67bdfc585afdc3098d80cb86117db93d98835aad8bc",
    "Calendrier/pipeline_calendar.py":
        "5bb5d1ef6fc8ee642723cdb33c71f9ca39c78da3e00fed560e1ba885f0bca13f",
}


def sha256(chemin: Path) -> str:
    h = hashlib.sha256()
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def main() -> int:
    print("Verification de l'integrite des 10 fichiers de programme")
    print("=" * 62)
    conformes = 0
    manquants = []
    modifies = []

    for rel, attendu in ATTENDU.items():
        chemin = PROGRAMMES / rel
        if not chemin.is_file():
            manquants.append(rel)
            print("  MANQUANT  " + rel)
            continue
        obtenu = sha256(chemin)
        if obtenu == attendu:
            conformes += 1
            print("  OK        " + rel)
        else:
            modifies.append(rel)
            print("  MODIFIE   " + rel)
            print("            attendu : " + attendu)
            print("            obtenu  : " + obtenu)

    print("=" * 62)
    print("  " + str(conformes) + " / " + str(len(ATTENDU)) + " fichiers conformes")

    if modifies:
        print()
        print("  ATTENTION : des fichiers de programme ont ete modifies.")
        print("  Ces fichiers doivent rester tels quels.")
        return 1
    if manquants:
        print()
        print("  ATTENTION : des fichiers sont absents du depot.")
        return 1
    print()
    print("  Les 10 fichiers de programme sont intacts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())