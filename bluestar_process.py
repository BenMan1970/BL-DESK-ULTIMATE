"""Execution isolee d'une etape dans un processus separe.

Pourquoi ce module existe
-------------------------
Sur Streamlit Community Cloud, une etape bloquee ne doit JAMAIS figer
l'application. Un thread Python ne peut pas etre tue : si un scanner reste
bloque sur une requete reseau, il tourne indefiniment et l'utilisateur ne
voit rien.

La seule facon fiable de reprendre la main est d'executer l'etape dans un
PROCESSUS separe. On peut alors le terminer de force au bout du delai.

Les 7 programmes d'origine ne sont pas modifies : on les importe et on les
appelle exactement comme avant, simplement dans un autre processus.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import traceback
from pathlib import Path


def _executer(module: str, fonction: str, args: tuple, file_retour: str) -> None:
    """Point d'entree du processus fils : importe le module et appelle.

    Le resultat (ou l'erreur) est ecrit dans un fichier JSON, car une
    exception levee dans un processus fils doit etre transportee
    explicitement vers le parent.
    """
    charge = {"ok": False}
    try:
        import importlib

        mod = importlib.import_module(module)
        cible = getattr(mod, fonction)
        resultat = cible(*args)
        charge = {"ok": True, "valeur": resultat}
    except BaseException as exc:                       # noqa: BLE001
        charge = {
            "ok": False,
            "type": type(exc).__name__,
            "message": str(exc),
            "trace": traceback.format_exc()[-4000:],
        }
    try:
        Path(file_retour).write_text(
            json.dumps(charge, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except Exception:                                  # noqa: BLE001
        pass


def _cible(module, fonction, args, file_retour, dossier):
    """Adaptateur : fixe le repertoire courant puis execute."""
    if dossier:
        try:
            os.chdir(dossier)
        except OSError:
            pass
    _executer(module, fonction, args, file_retour)


def lancer_isole(
    module: str,
    fonction: str,
    args: tuple,
    delai: float,
    dossier: str | Path | None = None,
    fichier_retour: str | Path | None = None,
) -> object:
    """Execute ``module.fonction(*args)`` dans un processus, avec un delai dur.

    Levee ``TimeoutError`` si le delai est depasse, ``RuntimeError`` si le
    processus fils a echoue. Dans les deux cas, le processus est termine de
    force : l'appelant recupere TOUJOURS la main.
    """
    # On utilise « fork » et non « spawn » : spawn reimporte le script
    # principal dans le processus fils, ce qui est incompatible avec
    # Streamlit (le script tourne sous un runtime qui ne se reimporte pas).
    # fork est disponible sur Linux, donc sur Streamlit Community Cloud.
    try:
        contexte = mp.get_context("fork")
    except ValueError:
        contexte = mp.get_context("spawn")
    fichier_retour = str(fichier_retour or (Path(os.environ.get("TMPDIR", "/tmp"))
                                            / f"bluestar_retour_{os.getpid()}_{id(args)}.json"))
    proc = contexte.Process(
        target=_cible,
        args=(module, fonction, args, fichier_retour, str(dossier) if dossier else None),
        daemon=True,
    )
    proc.start()
    proc.join(delai)

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        try:
            Path(fichier_retour).unlink()
        except OSError:
            pass
        raise TimeoutError(
            "delai de {0:.0f} s depasse dans un processus separe".format(delai)
        )

    sortie = Path(fichier_retour)
    if not sortie.is_file():
        raise RuntimeError(
            "le processus s'est termine sans rien produire (code "
            + str(proc.exitcode) + ")"
        )
    try:
        charge = json.loads(sortie.read_text(encoding="utf-8"))
    finally:
        try:
            sortie.unlink()
        except OSError:
            pass

    if not charge.get("ok"):
        raise RuntimeError(
            "{0}: {1}".format(charge.get("type", "Erreur"), charge.get("message", ""))
        )
    return charge.get("valeur")


# ---------------------------------------------------------------------------
# Pilotage par lot : on demarre tous les processus, puis on les attend.
#
# C'est le mode utilise par la chaine. Il permet de lancer les 4 scanners
# en parallele tout en gardant la possibilite de tuer chacun d'eux.
# ---------------------------------------------------------------------------

def contexte():
    """Rend un contexte multiprocessing compatible avec Streamlit.

    On prefere « fork » : spawn reimporte le script principal dans le
    processus fils, ce qui est incompatible avec le runtime Streamlit.
    """
    try:
        return mp.get_context("fork")
    except ValueError:                                 # pragma: no cover
        return mp.get_context("spawn")


def demarrer(ctx, fonction, args, file_retour, dossier, delai,
             module="bluestar_workers"):
    """Demarre un travailleur et rend un descripteur inerte."""
    proc = ctx.Process(
        target=_cible,
        args=(module, fonction, args, str(file_retour),
              str(dossier) if dossier else None),
        daemon=True,
    )
    proc.start()
    return proc


def attendre(proc, file_retour, delai):
    """Attend un travailleur au plus ``delai`` secondes.

    Rend ``(bilan, erreur)``. ``bilan`` est le dictionnaire rendu par le
    travailleur ; ``erreur`` est un message lisible, ou None. Dans TOUS les
    cas le processus est termine : l'appelant ne peut pas rester bloque.
    """
    proc.join(delai)

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        return None, (
            "delai depasse ({0:.0f} s) - l'etape n'a pas rendu la main. "
            "La source a probablement limite le debit (HTTP 429 ou 504) ; "
            "relancez dans quelques minutes.".format(delai)
        )

    if not file_retour:
        return None, "le processus s'est termine sans rien produire (code " \
            + str(proc.exitcode) + ")"

    if not Path(file_retour).is_file():
        return None, (
            "le processus s'est arrete sans rendre de resultat (code "
            + str(proc.exitcode) + "). Cause probable : memoire insuffisante "
            "ou arret brutal du conteneur."
        )

    try:
        charge = json.loads(Path(file_retour).read_text(encoding="utf-8"))
    except Exception as exc:                           # noqa: BLE001
        return None, "resultat illisible : {0}".format(exc)

    if not charge.get("ok"):
        message = "{0}: {1}".format(
            charge.get("type", "Erreur"), charge.get("message", "")
        )
        return None, message
    return charge.get("valeur"), None
