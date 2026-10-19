"""Travailleur execute DANS LE PROCESSUS FILS pour chaque scanner.

Ce fichier est le seul endroit qui connait la facon d'appeler chacun des
7 programmes. Les programmes eux-memes ne sont jamais modifies : on les
importe et on appelle leurs fonctions, exactement comme le faisait
l'interface d'origine.

Chaque fonction renvoie un dictionnaire serialisable, ecrit par le
processus fils puis relu par le processus parent.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent
PROGRAMMES = RACINE / "programmes"

DOSSIER_GPS = PROGRAMMES / "Scanner  MTF GPS"
DOSSIER_CHOCH = PROGRAMMES / "Scanner CHoCH"
DOSSIER_RSI = PROGRAMMES / "Scanner RSI"
DOSSIER_SR = PROGRAMMES / "Scanner Supports & Résistances"
DOSSIER_CALENDRIER = PROGRAMMES / "Calendrier"
DOSSIER_MERGE = PROGRAMMES / "Merge"
DOSSIER_BLS = PROGRAMMES / "BLS V10 Pro"


class _BarreInerte:
    """Remplace la barre de progression Streamlit d'origine.

    Quand on importe l'application d'origine hors de Streamlit, les appels
    de progression tombent sur None. Cette classe absorbe ces appels : on
    ne modifie pas le programme, on lui fournit un objet compatible.
    """

    def progress(self, *_a, **_k):
        return self

    def empty(self):
        return self

    def write(self, *_a, **_k):
        return None

    def text(self, *_a, **_k):
        return None

    def markdown(self, *_a, **_k):
        return None

    def caption(self, *_a, **_k):
        return None

    def __getattr__(self, _nom):
        return lambda *a, **k: None


def _taire_bruit():
    """Silence le bruit des bibliotheques DANS le processus fils.

    Les avertissements « missing ScriptRunContext » viennent de Streamlit
    quand un module d'interface est importe hors du thread principal. Ils
    sont benins mais ils noient les logs de la chaine. Comme chaque scanner
    tourne desormais dans son propre processus, il faut aussi les taire ici
    et pas seulement dans le processus parent, ou cela n'a aucun effet.

    On ne touche QU'AU niveau des modules concernes : aucun message de nos
    propres journalisations n'est perdu.
    """
    import logging

    for nom in ("streamlit",
                "streamlit.runtime.scriptrunner.script_run_context",
                "streamlit.runtime.scriptrunner_utils.script_run_context",
                "streamlit.runtime.scriptrunner",
                "streamlit.runtime.caching",
                "streamlit.delta_generator",
                "oandapyV20.oandapyV20",
                "urllib3",
                "matplotlib",
                "matplotlib.font_manager"):
        try:
            logging.getLogger(nom).setLevel(logging.CRITICAL)
        except Exception:                              # noqa: BLE001
            pass


def _charger(nom, chemin, dossier=None):
    """Importe un programme par son chemin, comme le faisait la chaine."""
    dossier = Path(dossier or chemin.parent)
    if str(dossier) not in sys.path:
        sys.path.insert(0, str(dossier))
    ancien = os.getcwd()
    try:
        os.chdir(dossier)
        spec = importlib.util.spec_from_file_location(nom, chemin)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[nom] = mod
        spec.loader.exec_module(mod)
    finally:
        os.chdir(ancien)
    return mod


def _ecrire(cible, donnees):
    if isinstance(donnees, str):
        donnees = donnees.encode("utf-8")
    cible.parent.mkdir(parents=True, exist_ok=True)
    tmp = cible.with_suffix(cible.suffix + ".tmp")
    tmp.write_bytes(donnees)
    tmp.replace(cible)
    return len(donnees)


# ---------------------------------------------------------------------------
# Chaque fonction ci-dessous correspond a UN scanner. Elle est appelee dans
# son propre processus : un blocage ici ne peut pas figer l'application.
# ---------------------------------------------------------------------------

def scanner_gps(travail, jeton, compte):
    """Scanner MTF GPS : detecte les configurations multi-timeframes."""
    _taire_bruit()
    mod = _charger("bs_gps", DOSSIER_GPS / "app.py", DOSSIER_GPS)
    jeton_ok = mod.SecretToken(jeton)
    df, _erreurs, meta, _avertissements = mod.analyze_all_core(compte, jeton_ok)
    octets = mod.export_json(df, meta)
    n = len(df) if hasattr(df, "__len__") else 0
    cible = Path(travail) / "gps.json"
    taille = _ecrire(cible, octets)
    return {"cle": "gps", "cible": str(cible), "message": f"{n} elements",
            "octets": taille}


def scanner_choch(travail, jeton, environnement):
    """Scanner CHoCH : detecte les changements de caractere."""
    _taire_bruit()
    mod = _charger("bs_choch", DOSSIER_CHOCH / "app.py", DOSSIER_CHOCH)
    creds = mod.OandaCredentials(jeton, environnement, "OANDA_ACCESS_TOKEN")
    resultat = mod.run_scan(creds, 0)
    octets = mod.serialize_pipeline(
        resultat.payloads, resultat.scan_time, resultat.errors,
        resultat.coverage_counts, resultat.precision, environnement,
    )
    n = len(resultat.payloads or [])
    cible = Path(travail) / "choch.json"
    taille = _ecrire(cible, octets)
    return {"cle": "choch", "cible": str(cible), "message": f"{n} signaux",
            "octets": taille}


def scanner_rsi(travail, jeton, compte, environnement):
    """Scanner RSI : mesure la force relative sur 165 cellules."""
    _taire_bruit()
    mod = _charger("bs_rsi", DOSSIER_RSI / "app.py", DOSSIER_RSI)
    resultat = mod.run_scan()
    if resultat is None:
        raise RuntimeError(
            "scan refuse par le verrou interne du scanner - relancez"
        )
    if isinstance(resultat, dict) and "cooldown" in resultat:
        raise RuntimeError(
            "cooldown actif ({0:.0f} s) - le scanner refuse deux executions "
            "trop rapprochees".format(float(resultat.get("cooldown") or 0))
        )
    bruts = resultat.get("results") if isinstance(resultat, dict) else resultat
    if not bruts:
        raise RuntimeError(
            "le scan n'a retourne aucun instrument - verifiez le jeton OANDA"
        )
    stats = mod.compute_statistics(bruts)
    import datetime

    # On reproduit EXACTEMENT le calcul de l'interface d'origine, qui lit le
    # bilan du scan pour declarer sa completude. Auparavant la chaine annoncait
    # une completude de 1.0 en dur : le document etait alors marque OK alors que
    # le scan pouvait etre partiel, et le merge refusait l'export.
    scan_ts = datetime.datetime.now(datetime.timezone.utc)
    bilan = resultat if isinstance(resultat, dict) else {}
    scan_id = str(bilan.get("scan_id", "") or "")
    attendues = int(bilan.get("expected_cells")
                    or len(getattr(mod, "ASSETS", []) or [])
                    * len(getattr(mod, "TIMEFRAMES_DISPLAY", []) or []))
    reussies = int(bilan.get("successful_cells") or 0)
    completude = {
        "instruments_received": len(bruts) if isinstance(bruts, list) else 0,
        "cells_expected": attendues,
        "cells_ok": reussies,
        "cells_failed": max(0, attendues - reussies),
    }
    octets = mod.create_json_export(
        bruts, stats, scan_ts, scan_id=scan_id, completeness=completude,
    )
    cible = Path(travail) / "rsi.json"
    taille = _ecrire(cible, octets)
    taux = (100.0 * reussies / attendues) if attendues else 0.0
    return {"cle": "rsi", "cible": str(cible),
            "message": "{0} instruments ({1:.0f}% des cellules)".format(
                len(bruts), taux),
            "octets": taille}


def scanner_sr(travail, jeton, compte, environnement):
    """Scanner Supports & Resistances : cartographie les zones de prix."""
    _taire_bruit()
    mod = _charger("bs_sr", DOSSIER_SR / "app.py", DOSSIER_SR)
    symboles = list(getattr(mod, "ALL_SYMBOLS", []) or [])
    if not symboles:
        raise RuntimeError("liste d'instruments S/R introuvable (ALL_SYMBOLS)")
    brut = mod._run_async_isolated(
        lambda: mod.run_institutional_scan(
            symboles, jeton, compte, 2, oanda_env=environnement,
        )
    )
    agg = mod._accumulate_scan_results(brut, _BarreInerte())
    conf = mod._compute_all_confluences(symboles, agg, 0.8)
    resumes = mod._build_summaries(symboles, agg)
    octets = mod.create_json_export(
        resumes, conf, None, 0.0, ("Vierge", "Testee", "Role Reverse")
    )
    n = len(resumes or [])
    cible = Path(travail) / "sr.json"
    taille = _ecrire(cible, octets)
    return {"cle": "sr", "cible": str(cible), "message": f"{n} actifs",
            "octets": taille}


def etape_calendrier(travail, seuil_fraicheur=12.0, seuil_peremption=48.0):
    """Calendrier economique. Aucun secret OANDA : sources publiques."""
    _taire_bruit()
    import datetime
    import logging

    dossier = Path(travail) / "calendrier"
    dossier.mkdir(parents=True, exist_ok=True)

    if str(DOSSIER_CALENDRIER) not in sys.path:
        sys.path.insert(0, str(DOSSIER_CALENDRIER))
    ancien = os.getcwd()
    try:
        os.chdir(DOSSIER_CALENDRIER)
        mod = _charger("bs_calendrier",
                       DOSSIER_CALENDRIER / "pipeline_calendar.py",
                       DOSSIER_CALENDRIER)
        tampon = []
        manche = logging.StreamHandler()

        class _Capture(logging.Handler):
            def emit(self, record):
                try:
                    tampon.append(self.format(record))
                except Exception:
                    pass

        capture = _Capture()
        capture.setLevel(logging.INFO)
        racine = logging.getLogger()
        racine.addHandler(capture)
        try:
            charge = mod.run_once(Path(dossier))
        finally:
            racine.removeHandler(capture)
    finally:
        os.chdir(ancien)

    cible = dossier / "calendar.json"
    if not cible.is_file():
        motif = ""
        sante = dossier / "health.json"
        if sante.is_file():
            try:
                h = json.loads(sante.read_bytes())
                motif = str(h.get("error") or h.get("status") or "")
                mode = str(h.get("mode") or "").lower()
                if mode:
                    motif = (mode + " " + motif).strip()
            except Exception:
                motif = ""
        if "429" in motif.upper() or "RATE" in motif.upper() or "COOLDOWN" in motif.upper():
            raise RuntimeError(
                "la source a limite le debit (HTTP 429) - relancez dans "
                "quelques minutes : rien n'est casse"
            )
        if "FLOOR" in motif.upper():
            raise RuntimeError(
                "plancher anti-429 actif (source recemment interrogee) - "
                "relancez dans une a deux minutes"
            )
        raise RuntimeError(
            "aucun fichier produit" + (" (" + motif + ")" if motif else "")
        )

    doc = json.loads(cible.read_bytes())
    n = len(doc.get("events") or [])
    age = None
    brut = (doc.get("metadata") or {}).get("generated_at_utc") or doc.get("generated_at_utc")
    if brut:
        try:
            t = datetime.datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=datetime.timezone.utc)
            age = (datetime.datetime.now(datetime.timezone.utc)
                   - t).total_seconds() / 3600.0
        except ValueError:
            age = None

    if age is not None and age > seuil_peremption:
        raise RuntimeError(
            "artefact perime ({0:.1f} h) - desk refuse".format(age)
        )
    message = f"{n} evenements"
    if age is not None and age > seuil_fraicheur:
        message += " (servi depuis le cache, {0:.1f} h)".format(age)
    return {"cle": "calendrier", "cible": str(cible), "message": message,
            "octets": cible.stat().st_size, "age_h": age,
            "journal": tampon[-12:]}


def etape_merge(travail, ordre):
    """Merge : fusionne les 4 exports en un document unique.

    On suit exactement le chemin de l'interface d'origine : on construit le
    pipeline par get_pipeline(), on enveloppe chaque fichier dans un
    IngestedFile, puis on appelle pipeline.run(). On ne devine rien.
    """
    _taire_bruit()
    mod = _charger("bs_merge", DOSSIER_MERGE / "merge_app.py", DOSSIER_MERGE)

    ingeres = []
    for cle in ordre:
        chemin = Path(travail) / (cle + ".json")
        if not chemin.is_file():
            raise RuntimeError("export introuvable pour '{0}'".format(cle))
        charge = json.loads(chemin.read_text(encoding="utf-8"))
        ingeres.append(mod.IngestedFile(name=cle + ".json", payload=charge))

    if not ingeres:
        raise RuntimeError("aucun export a fusionner")

    pipeline = mod.get_pipeline()
    resultat = pipeline.run(ingeres)

    # pipeline.run() rend un objet Result : le document fusionne est dans
    # .value (un MergeOutput pydantic). On suit exactement cette structure,
    # comme le fait run_pipeline_cached() dans l'interface d'origine.
    if resultat is None:
        raise RuntimeError("le merge n'a rien rendu")
    if not getattr(resultat, "ok", False):
        details = []
        for d in getattr(resultat, "diagnostics", []) or []:
            try:
                details.append(str(d.to_dict().get("message") or d))
            except Exception:
                details.append(str(d))
        raise RuntimeError(
            "le merge a signale une erreur : " + (" | ".join(details)[:400]
                                                  or "cause inconnue")
        )

    sortie = getattr(resultat, "value", None)
    if sortie is None:
        raise RuntimeError("le merge n'a produit aucun document")
    document = sortie.model_dump(mode="json") if hasattr(sortie, "model_dump") \
        else sortie
    if not isinstance(document, dict):
        raise RuntimeError("document fusionne illisible")

    meta = document.get("meta") or {}
    detectes = list(meta.get("scanners_detected") or [])
    if len(detectes) < 4:
        reconnus = sorted({str(d).rsplit(":", 1)[-1].strip()
                           for d in detectes if str(d).strip()})
        manquants = sorted(set(ordre) - set(reconnus))
        raise RuntimeError(
            "merge incomplet : {0}/4 scanners reconnus (non reconnus : {1}). "
            "Un scanner non reconnu a produit un export vide : aucun "
            "signal/actif ce jour, ou jeton OANDA refuse.".format(
                len(detectes), ", ".join(manquants) if manquants else "?")
        )

    actifs = meta.get("assets_count") or 0
    if not actifs:
        actifs = len(document.get("assets") or [])
    if not actifs:
        raise RuntimeError("merge sans actif")

    signaux = len(document.get("signals") or [])
    cible = Path(travail) / "merged.json"
    taille = _ecrire(cible, json.dumps(document, ensure_ascii=False, default=str))
    return {"cle": "merge", "cible": str(cible),
            "message": "{0} actifs - {1} signaux - {2}/4 scanners".format(
                actifs, signaux, len(detectes)),
            "octets": taille}


def etape_bls(merged, calendar, sortie):
    """BLS V10 Pro : produit le desk HTML final."""
    _taire_bruit()
    mod = _charger("bs_bls", DOSSIER_BLS / "app.py", DOSSIER_BLS)
    resultat = mod.run_pipeline(
        merged_path=str(merged), calendar_json_path=str(calendar),
        output_path=str(sortie),
    )
    cible = Path(sortie)
    if not cible.is_file():
        raise RuntimeError("le desk n'a pas ete produit")
    return {"cle": "bls", "cible": str(cible),
            "message": "{0} octets".format(cible.stat().st_size),
            "octets": cible.stat().st_size}


