"""BLUESTAR DESK - chaine complete, en un seul bouton (Streamlit).

Ce module fait EXACTEMENT ce que la page HTML locale : il enchaine les
7 programmes d'origine sans en modifier une seule ligne.

    4 scanners (en parallele) + calendrier
                 |
        merge  ->  merged.json
                 |
        BLS V10 Pro  ->  desk HTML

Etape A : les 4 scanners tournent en parallele (ThreadPoolExecutor).
Etape B : le merge recoit exactement les 4 fichiers qu'il attend.
Etape C : BLS V10 Pro recoit le merged + le calendar.

Les 7 programmes sont importes tels quels, via importlib, sans patch ni
reecriture. Aucun de leurs fichiers n'est touche.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger("bluestar.chain")

# ---------------------------------------------------------------------------
# Emplacements - tout est relatif au repo, aucun chemin absolu.
# ---------------------------------------------------------------------------
RACINE = Path(__file__).resolve().parent
PROGRAMMES = RACINE / "programmes"

DOSSIER_GPS = PROGRAMMES / "Scanner  MTF GPS"
DOSSIER_CHOCH = PROGRAMMES / "Scanner CHoCH"
DOSSIER_RSI = PROGRAMMES / "Scanner RSI"
DOSSIER_SR = PROGRAMMES / "Scanner Supports & Résistances"
DOSSIER_MERGE = PROGRAMMES / "Merge"
DOSSIER_CALENDRIER = PROGRAMMES / "Calendrier"
DOSSIER_BLS = PROGRAMMES / "BLS V10 Pro"

FICHIER_CALENDAR = "calendar.json"

# Un calendrier plus vieux que ce seuil est refuse : le desk serait trompeur.
SEUIL_FRAICHEUR_CALENDRIER_H = 12.0
SEUIL_PEREMPTION_CALENDRIER_H = 48.0

DELAI_SCANNER = 600.0


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------
def _configurer_journal() -> None:
    """Fait apparaitre nos messages dans les logs du serveur.

    Sans cela, sur Streamlit Cloud, un blocage ne laisse AUCUNE trace
    exploitable : on voit seulement le bruit des threads de Streamlit.
    """
    racine = logging.getLogger()
    if racine.level > logging.INFO or not racine.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
            force=False,
        )
    racine.setLevel(logging.INFO)


def _taire_bruit_streamlit() -> None:
    """Empeche les avertissements de thread de noyer les logs.

    Quand les 4 scanners tournent en parallele, Streamlit emet des dizaines
    de « missing ScriptRunContext » par seconde. C'est normal (nos threads
    ne sont pas le thread principal) mais cela masque nos propres messages.
    On limite donc le bruit des modules de Streamlit, rien d'autre.
    """
    for nom in ("streamlit.runtime.scriptrunner.script_run_context",
                "streamlit.runtime.scriptrunner_utils.script_run_context",
                "streamlit.runtime.scriptrunner",
                "oandapyV20.oandapyV20",
                "urllib3",
                "matplotlib"):
        logging.getLogger(nom).setLevel(logging.WARNING)


def _charger_module(nom: str, chemin: Path, cwd: Path | None = None):
    """Importe un fichier .py isole, sans toucher au fichier lui-meme.

    Certains programmes importent des modules freres (par exemple le scanner
    S/R importe ``bluestar_core``). On ajoute donc le dossier du programme au
    chemin d'import le temps du chargement, puis on restaure.
    """
    ancien = os.getcwd()
    dossier = str(chemin.parent)
    ajoute = dossier not in sys.path
    if ajoute:
        sys.path.insert(0, dossier)
    if cwd is not None:
        os.chdir(cwd)
    try:
        spec = importlib.util.spec_from_file_location(nom, chemin)
        if spec is None or spec.loader is None:
            raise ImportError("impossible de charger " + str(chemin))
        module = importlib.util.module_from_spec(spec)
        sys.modules[nom] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(ancien)
        if ajoute:
            try:
                sys.path.remove(dossier)
            except ValueError:
                pass


class _BarreInerte:
    """Barre de progression factice pour les fonctions qui en attendent une.

    Le scanner S/R appelle progress_bar.progress(...) a chaque actif. En
    dehors de Streamlit, on lui fournit cet objet : il accepte les memes
    appels et ne fait rien.
    """

    def progress(self, *_args, **_kwargs) -> None:
        return None

    def empty(self) -> None:
        return None

    def write(self, *_args, **_kwargs) -> None:
        return None

    def text(self, *_args, **_kwargs) -> None:
        return None


def _ecrire_atomique(cible: Path, donnees: bytes) -> None:
    """Ecrit via un fichier temporaire puis renomme."""
    cible.parent.mkdir(parents=True, exist_ok=True)
    tmp = cible.with_suffix(cible.suffix + ".tmp")
    tmp.write_bytes(donnees)
    tmp.replace(cible)


def _sha256(chemin: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def _age_heures(doc: dict):
    """Age d'un artefact calendrier, en heures."""
    import datetime
    md = doc.get("metadata") or {}
    brut = md.get("generated_at_utc") or md.get("generated_at")
    if not brut:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    ecart = datetime.datetime.now(datetime.timezone.utc) - t
    return ecart.total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Identifiants - lus depuis st.secrets, jamais ecrits dans le repo
# ---------------------------------------------------------------------------
@dataclass
class Identifiants:
    """Ce que l'application a besoin de connaitre pour demarrer."""

    jeton: str = ""
    compte: str = ""
    environnement: str = "practice"


def lire_identifiants() -> Identifiants:
    """Lit les identifiants OANDA dans st.secrets, comme les apps d'origine.

    Les programmes d'origine attendent ces cles dans les secrets Streamlit
    (Settings -> Secrets) :

        OANDA_ACCESS_TOKEN = "..."
        OANDA_ACCOUNT_ID   = "101-002-..."
        OANDA_ENV          = "practice"      (facultatif)

    On lit donc st.secrets en premier, puis l'environnement en secours :
    c'est exactement l'ordre utilise par le scanner GPS d'origine.
    """
    import streamlit as st

    def _chercher(*noms):
        # 1. secrets Streamlit (le chemin normal sur Streamlit Cloud)
        for nom in noms:
            try:
                val = st.secrets.get(nom)
            except Exception:
                val = None
            if val:
                return str(val).strip()
        # 2. variables d'environnement (secours, comme les apps d'origine)
        for nom in noms:
            val = os.environ.get(nom)
            if val:
                return val.strip()
        return ""

    jeton = _chercher("OANDA_ACCESS_TOKEN", "OANDA_TOKEN")
    compte = _chercher("OANDA_ACCOUNT_ID", "OANDA_ACCOUNT")
    env = _chercher("OANDA_ENV", "OANDA_ENVIRONMENT", "ENVIRONMENT") or "practice"
    env = env.lower().strip()
    if env not in ("practice", "live"):
        env = "practice"
    return Identifiants(jeton=jeton, compte=compte, environnement=env)


def preparer_environnement(ident: Identifiants) -> None:
    """Prepare l'environnement que les 4 scanners consultent.

    Les programmes d'origine lisent leurs secrets eux-memes : on ne les
    remplace donc pas. Mais deux d'entre eux (GPS et CHoCH) lisent aussi
    OANDA_ENV dans l'environnement AVANT de regarder les secrets, et ce nom
    sert a choisir l'URL de l'API OANDA.

    Sans cette precaution, un OANDA_ENV laisse dans le shell pourrait
    orienter GPS et CHoCH vers le compte reel alors que vous visez
    "practice". On ecrit donc OANDA_ENV de facon ferme, a partir de la
    valeur unique que vous avez fournie dans les secrets.
    """
    os.environ["OANDA_ENV"] = ident.environnement
    os.environ["OANDA_ENVIRONMENT"] = ident.environnement


def installer_secrets_streamlit(ident: Identifiants) -> None:
    """Fournit les secrets aux programmes quand la plateforme ne le fait pas.

    Sur Streamlit Cloud, st.secrets est alimente par l'interface : les
    programmes lisent donc leurs secrets normalement, et cette fonction ne
    fait rien. Elle ne sert qu'en execution locale, quand aucun fichier
    secrets.toml n'est present.
    """
    import streamlit as st

    try:
        if st.secrets["OANDA_ACCESS_TOKEN"]:
            return
    except Exception:
        pass

    class _Secrets(dict):
        def __getitem__(self, cle):
            return self.get(cle)

        def get(self, cle, defaut=None):
            return dict.get(self, cle, defaut)

    donnees = {
        "OANDA_ACCESS_TOKEN": ident.jeton,
        "OANDA_ACCOUNT_ID": ident.compte,
        "OANDA_ENV": ident.environnement,
        "OANDA_ENVIRONMENT": ident.environnement,
    }
    try:
        st.secrets = _Secrets(donnees)
    except Exception as exc:
        logger.warning("secrets Streamlit non installes : %s", exc)


# ---------------------------------------------------------------------------
# Etape A - les 4 scanners
# ---------------------------------------------------------------------------
def _scanner_gps(ident, travail, journal):
    dossier = DOSSIER_GPS
    mod = _charger_module("bs_gps", dossier / "app.py", dossier)
    jeton = mod.SecretToken(ident.jeton)
    df, _erreurs, meta, _avertissements = mod.analyze_all_core(ident.compte, jeton)
    octets = mod.export_json(df, meta)
    if isinstance(octets, str):
        octets = octets.encode("utf-8")
    cible = travail / "gps.json"
    _ecrire_atomique(cible, octets)
    doc = json.loads(octets)
    signaux = doc.get("signals") or doc.get("assets") or []
    journal(str(len(signaux)) + " elements")
    return cible, "{0} elements - {1} octets".format(len(signaux), len(octets)), len(octets)


def _scanner_choch(ident, travail, journal):
    dossier = DOSSIER_CHOCH
    mod = _charger_module("bs_choch", dossier / "app.py", dossier)
    # OandaCredentials(token, env, source) : 3 arguments, pas de account_id.
    creds = mod.OandaCredentials(ident.jeton, ident.environnement, "OANDA_ACCESS_TOKEN")
    resultat = mod.run_scan(creds, 0)
    texte = mod.serialize_pipeline(
        resultat.payloads, resultat.scan_time, resultat.errors,
        resultat.coverage_counts, resultat.precision, ident.environnement,
    )
    octets = texte.encode("utf-8") if isinstance(texte, str) else texte
    cible = travail / "choch.json"
    _ecrire_atomique(cible, octets)
    doc = json.loads(octets)
    signaux = doc.get("signals") or []
    journal(str(len(signaux)) + " signaux")
    return cible, "{0} signaux - {1} octets".format(len(signaux), len(octets)), len(octets)


def _scanner_rsi(ident, travail, journal):
    dossier = DOSSIER_RSI
    mod = _charger_module("bs_rsi", dossier / "app.py", dossier)
    resultat = mod.run_scan()

    # [AUDIT] Un refus du scanner n'est PAS un scan vide. run_scan() peut rendre
    # None (verrou de scan actif) ou {"cooldown": s, "results": None} (refus
    # trop rapproche). Sans ce garde-fou, la chaine exportait un document a 0
    # instrument marque "ok", et l'echec ne reapparaissait qu'au merge sous la
    # forme trompeuse "3/4 scanners reconnus". On echoue ici, explicitement.
    if resultat is None:
        raise RuntimeError(
            "RSI: scan refuse par le verrou interne du scanner (une autre "
            "execution est en cours) - relancez dans quelques secondes"
        )
    if isinstance(resultat, dict) and "cooldown" in resultat:
        try:
            restant = float(resultat.get("cooldown") or 0.0)
        except (TypeError, ValueError):
            restant = 0.0
        raise RuntimeError(
            "RSI: cooldown actif ({0:.0f} s restantes) - le scanner refuse deux "
            "executions trop rapprochees. Attendez puis relancez.".format(restant)
        )

    bruts = resultat.get("results") if isinstance(resultat, dict) else resultat
    if not bruts:
        raise RuntimeError(
            "RSI: le scan n'a retourne aucun instrument - verifiez le jeton "
            "OANDA (autorisation) puis relancez"
        )
    stats = mod.compute_statistics(bruts)
    import datetime
    scan_ts = datetime.datetime.now(datetime.timezone.utc)
    octets = mod.create_json_export(
        bruts, stats, scan_ts,
        scan_id=str((resultat or {}).get("scan_id") or "") if isinstance(resultat, dict) else "",
        completeness=(resultat or {}).get("completeness") if isinstance(resultat, dict) else None,
    )
    if isinstance(octets, str):
        octets = octets.encode("utf-8")
    cible = travail / "rsi.json"
    _ecrire_atomique(cible, octets)
    doc = json.loads(octets)
    n = len(doc.get("instruments") or [])
    journal(str(n) + " instruments")
    return cible, "{0} instruments - {1} octets".format(n, len(octets)), len(octets)


def _scanner_sr(ident, travail, journal):
    dossier = DOSSIER_SR
    mod = _charger_module("bs_sr", dossier / "app.py", dossier)
    # ALL_SYMBOLS est re-exporte par app.py depuis bluestar_core.
    symboles = list(getattr(mod, "ALL_SYMBOLS", []) or [])
    if not symboles:
        raise RuntimeError("liste d'instruments S/R introuvable (ALL_SYMBOLS)")
    # run_institutional_scan est asynchrone : l'interface d'origine l'execute
    # via _run_async_isolated, qui cree sa propre boucle d'evenements.
    brut = mod._run_async_isolated(
        lambda: mod.run_institutional_scan(
            symboles, ident.jeton, ident.compte, 2, oanda_env=ident.environnement,
        )
    )
    agg = mod._accumulate_scan_results(brut, _BarreInerte())
    conf = mod._compute_all_confluences(symboles, agg, 0.8)
    resumes = mod._build_summaries(symboles, agg)
    octets = mod.create_json_export(resumes, conf, None, 0.0, ("Vierge", "Testee", "Role Reverse"))
    if isinstance(octets, str):
        octets = octets.encode("utf-8")
    cible = travail / "sr.json"
    _ecrire_atomique(cible, octets)
    doc = json.loads(octets)
    n = len(doc.get("assets") or doc.get("instruments") or [])
    journal(str(n) + " actifs")
    return cible, "{0} actifs - {1} octets".format(n, len(octets)), len(octets)


# ---------------------------------------------------------------------------
# Etape A bis - le calendrier (script d'origine, lance tel quel)
# ---------------------------------------------------------------------------
def _calendrier(travail, journal):
    """Ingere le calendrier en appelant directement le module d'origine.

    On n'utilise PAS de sous-processus : sur Streamlit Community Cloud, un
    ``subprocess`` herite d'un environnement ou l'interpreteur n'est pas
    forcement executable, et il bloque sans que personne ne le voie.

    On appelle donc ``run_once()`` du programme d'origine, qui est la
    fonction qui fait reellement le travail (le mode ``--once`` de la ligne
    de commande ne fait que l'appeler). Le code du calendrier n'est pas
    modifie : on se contente de l'invoquer autrement.
    """
    dossier = travail / "calendrier"
    dossier.mkdir(parents=True, exist_ok=True)

    mod = _charger_module("bs_calendrier", DOSSIER_CALENDRIER / "pipeline_calendar.py",
                          DOSSIER_CALENDRIER)

    # Le programme d'origine journalise sur sa propre sortie. On la capte
    # pour la restituer dans l'interface au lieu de la perdre.
    import io as _io
    import logging as _log
    tampon = _io.StringIO()
    manche = _log.StreamHandler(tampon)
    manche.setLevel(_log.INFO)
    racine = _log.getLogger()
    niveau_avant = racine.level
    racine.addHandler(manche)
    racine.setLevel(_log.INFO)
    try:
        charge = mod.run_once(Path(dossier))
    finally:
        racine.removeHandler(manche)
        racine.setLevel(niveau_avant)
    for ligne in tampon.getvalue().splitlines():
        if ligne.strip():
            journal(ligne.strip()[:150])



    cible = dossier / FICHIER_CALENDAR
    if not cible.is_file():
        # Rien de neuf publie. Le motif reel est dans health.json : 429
        # (mode=cooldown), plancher anti-429 (mode=floor), panne de source
        # (HTTP_ERROR / NO_DATA) ou ingestion desactivee (mode=reader). On ne
        # les confond plus : "limite de debit" n'est vrai que pour un 429.
        etat = ""
        erreur = ""
        mode = ""
        limite = None
        sante = dossier / "health.json"
        if sante.is_file():
            try:
                h = json.loads(sante.read_bytes())
                etat = str(h.get("status") or h.get("error") or "")
                erreur = str(h.get("error") or "")
                mode = str(h.get("mode") or "").lower()
                limite = h.get("rate_limit") if h.get("rate_limited") else None
            except Exception:
                etat = ""
        motif = (erreur or etat).upper()
        if mode == "cooldown" or "RATE_LIMIT" in motif or "COOLDOWN" in motif:
            precision = ""
            if isinstance(limite, dict):
                jusqua = (limite.get("rate_limited_until_utc")
                          or limite.get("blocked_until_utc")
                          or limite.get("until"))
                if jusqua:
                    try:
                        import datetime as _dt
                        t = _dt.datetime.fromisoformat(
                            str(jusqua).replace("Z", "+00:00"))
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=_dt.timezone.utc)
                        reste = (_dt.datetime.now(_dt.timezone.utc)
                                 - t).total_seconds()
                        if reste < 0:
                            precision = (
                                " - cooldown jusqu'a {0} UTC (environ {1:.0f} min)"
                                .format(t.strftime("%H:%M:%S"), -reste / 60.0)
                            )
                    except ValueError:
                        precision = ""
            raise RuntimeError(
                "calendrier indisponible : la source a limite le debit (HTTP 429)"
                + precision
                + ". Relancez dans quelques minutes : rien n'est casse."
            )
        if mode == "floor" or "FLOOR" in motif:
            raise RuntimeError(
                "calendrier indisponible : plancher anti-429 actif (la source "
                "vient d'etre interrogee). Relancez dans une a deux minutes."
            )
        if mode == "reader" or "INGEST_DISABLED" in motif:
            raise RuntimeError(
                "calendrier indisponible : ingestion desactivee "
                "(BLUESTAR_DISABLE_INGEST=1) - aucun fetch n'est tente."
            )
        if erreur:
            raise RuntimeError(
                "calendrier indisponible : echec de la source (" + erreur + ")"
                " - verifiez la connectivite du conteneur puis relancez."
            )
        raise RuntimeError("le calendrier n'a produit aucun fichier"
                           + (" (" + etat + ")" if etat else ""))

    doc = json.loads(cible.read_bytes())
    age = _age_heures(doc)
    n = len(doc.get("events") or [])

    if age is not None and age > SEUIL_PEREMPTION_CALENDRIER_H:
        raise RuntimeError(
            "artefact calendrier perime ({0:.1f} h > {1:.0f} h) - desk refuse".format(
                age, SEUIL_PEREMPTION_CALENDRIER_H)
        )
    if age is not None and age > SEUIL_FRAICHEUR_CALENDRIER_H:
        journal("{0} evenements - servi depuis le cache ({1:.1f} h)".format(n, age))
        return cible, "{0} evenements - cache {1:.1f} h".format(n, age), age

    journal(str(n) + " evenements")
    return cible, "{0} evenements".format(n), age


# ---------------------------------------------------------------------------
# Etape B - le merge
# ---------------------------------------------------------------------------
def _merge(quatre, travail, journal):
    dossier = DOSSIER_MERGE
    mod = _charger_module("bs_merge", dossier / "merge_app.py", dossier)
    fichiers = []
    for cle in ("gps", "rsi", "sr", "choch"):
        p = quatre[cle]
        fichiers.append(mod.IngestedFile(name=p.name, payload=json.loads(p.read_bytes())))
    registre = mod.ScannerRegistry(
        [mod.GPSAdapter(), mod.RSIAdapter(), mod.SRAdapter(), mod.CHoCHAdapter()],
        fallback=mod.HeuristicAdapter(),
    )
    pipeline = mod.MergePipeline(registry=registre)
    resultat = pipeline.run(fichiers)
    sortie = resultat.value

    detectes = list(getattr(sortie.meta, "scanners_detected", []) or [])
    if len(detectes) < 4:
        # [AUDIT] Nommer les scanners manquants. Un adaptateur ne reconnait un
        # scanner que si son export contient au moins un element : un export
        # VIDE (aucun signal ce jour, ou jeton OANDA refuse) fait echouer le
        # merge avec "3/4" sans jamais dire lequel manquait.
        reconnus = sorted({str(d).rsplit(":", 1)[-1].strip()
                           for d in detectes if str(d).strip()})
        manquants = sorted({"gps", "rsi", "sr", "choch"} - set(reconnus))
        raise RuntimeError(
            "merge incomplet : {0}/4 scanners reconnus (reconnus : {1} ; "
            "non reconnus : {2}). Un scanner non reconnu a produit un export "
            "vide : aucun signal/actif ce jour, ou jeton OANDA non autorise "
            "pour ce scanner.".format(
                len(detectes),
                ", ".join(reconnus) if reconnus else "aucun",
                ", ".join(manquants) if manquants else "?",
            )
        )
    actifs = getattr(sortie.meta, "assets_count", 0)
    if not actifs:
        raise RuntimeError("merge sans actif - desk refuse")

    texte = mod.export_json(sortie)
    octets = texte.encode("utf-8") if isinstance(texte, str) else texte
    cible = travail / "merged.json"
    _ecrire_atomique(cible, octets)
    signaux = getattr(sortie.meta, "signals_count", 0)
    journal("{0} actifs - {1} signaux".format(actifs, signaux))
    return cible, "{0} actifs - {1} signaux - {2}/4 scanners".format(actifs, signaux, len(detectes))


# ---------------------------------------------------------------------------
# Etape C - BLS V10 Pro
# ---------------------------------------------------------------------------
def _bls(merged, calendar, travail, journal):
    dossier = DOSSIER_BLS
    mod = _charger_module("bs_bls", dossier / "ENGINE.V10.py", dossier)
    cible = travail / "desk.html"
    html = mod.run_pipeline(
        merged_path=str(merged), calendar_json_path=str(calendar), output_path=str(cible),
    )
    octets = html.encode("utf-8") if isinstance(html, str) else html
    _ecrire_atomique(cible, octets)
    journal("desk {0} octets".format(len(octets)))
    return cible, "desk genere - {0} octets".format(len(octets))


# ---------------------------------------------------------------------------
# Deroulement complet
# ---------------------------------------------------------------------------
@dataclass
class Etape:
    cle: str
    libelle: str
    statut: str = "en_attente"
    message: str = ""
    duree_s: float | None = None


@dataclass
class Resultat:
    statut: str = "en_attente"
    etapes: list = field(default_factory=list)
    desk: Path | None = None
    desk_sha256: str = ""
    desk_octets: int = 0
    erreur: str = ""
    duree_s: float = 0.0
    dossier_travail: Path | None = None


ETAPES = [
    ("gps", "Scanner MTF GPS"),
    ("choch", "Scanner CHoCH"),
    ("rsi", "Scanner RSI"),
    ("sr", "Scanner Supports & Resistances"),
    ("calendrier", "Calendrier economique"),
    ("merge", "Merge"),
    ("bls", "BLS V10 Pro - desk"),
]


def executer_chaine(ident, progression=None, conserver_travail=False) -> Resultat:
    """Deroule la chaine entiere. Aucun desk partiel n'est renvoye."""
    depart = time.monotonic()
    res = Resultat(etapes=[Etape(cle=c, libelle=l) for c, l in ETAPES])
    index = {e.cle: e for e in res.etapes}

    def _maj(cle, statut, message="", duree=None):
        e = index[cle]
        e.statut = statut
        if message:
            e.message = message
        if duree is not None:
            e.duree_s = duree
        # On trace AUSSI dans les logs serveur : sans cela, un blocage sur
        # Streamlit Cloud ne laisse aucune trace exploitable.
        if statut in ("ok", "echec"):
            logger.info("[%s] %s - %s%s", cle, statut, message or "",
                        " ({0:.1f}s)".format(duree) if duree else "")
        elif message:
            logger.info("[%s] %s", cle, message)
        if progression:
            progression(cle, e)

    def _journal(cle):
        def _f(msg):
            _maj(cle, "en_cours", msg)
        return _f

    _configurer_journal()
    _taire_bruit_streamlit()
    preparer_environnement(ident)
    installer_secrets_streamlit(ident)
    logger.info("chaine demarree - environnement=%s", ident.environnement)

    travail = Path(tempfile.mkdtemp(prefix="bluestar_desk_"))
    res.dossier_travail = travail

    try:
        for cle in ("gps", "choch", "rsi", "sr"):
            _maj(cle, "en_cours", "demarrage...")

        taches = {"gps": _scanner_gps, "choch": _scanner_choch,
                  "rsi": _scanner_rsi, "sr": _scanner_sr}
        quatre = {}
        erreurs = {}

        with ThreadPoolExecutor(max_workers=4) as pool:
            futurs = {pool.submit(fn, ident, travail, _journal(cle)): cle
                      for cle, fn in taches.items()}
            for futur in as_completed(futurs, timeout=DELAI_SCANNER):
                cle = futurs[futur]
                debut = time.monotonic()
                try:
                    cible, message, _o = futur.result()
                    quatre[cle] = cible
                    _maj(cle, "ok", message, time.monotonic() - debut)
                except Exception as exc:
                    erreurs[cle] = type(exc).__name__ + ": " + str(exc)
                    _maj(cle, "echec", erreurs[cle], time.monotonic() - debut)

        if erreurs:
            details = " | ".join(c + ": " + m for c, m in erreurs.items())
            raise RuntimeError("scanner(s) en echec - " + details)

        _maj("calendrier", "en_cours", "ingestion...")
        debut = time.monotonic()
        try:
            cal_path, cal_msg, _age = _calendrier(travail, _journal("calendrier"))
            _maj("calendrier", "ok", cal_msg, time.monotonic() - debut)
        except Exception as exc:
            _maj("calendrier", "echec", str(exc), time.monotonic() - debut)
            raise RuntimeError("calendrier en echec - " + str(exc)) from exc

        _maj("merge", "en_cours", "fusion...")
        debut = time.monotonic()
        try:
            merged, merge_msg = _merge(quatre, travail, _journal("merge"))
            _maj("merge", "ok", merge_msg, time.monotonic() - debut)
        except Exception as exc:
            _maj("merge", "echec", str(exc), time.monotonic() - debut)
            raise RuntimeError("merge en echec - " + str(exc)) from exc

        _maj("bls", "en_cours", "rendu du desk...")
        debut = time.monotonic()
        try:
            desk, bls_msg = _bls(merged, cal_path, travail, _journal("bls"))
            res.desk = desk
            res.desk_sha256 = _sha256(desk)
            res.desk_octets = desk.stat().st_size
            _maj("bls", "ok", bls_msg, time.monotonic() - debut)
        except Exception as exc:
            _maj("bls", "echec", str(exc), time.monotonic() - debut)
            raise RuntimeError("BLS V10 Pro en echec - " + str(exc)) from exc

        res.statut = "ok"
        return res

    except Exception as exc:
        res.statut = "echec"
        res.erreur = str(exc)
        return res

    finally:
        res.duree_s = time.monotonic() - depart
        if not conserver_travail:
            if res.desk and res.desk.is_file():
                garde = Path(tempfile.gettempdir()) / ("bluestar_desk_" + res.desk_sha256[:8] + ".html")
                try:
                    shutil.copy2(res.desk, garde)
                    res.desk = garde
                except Exception:
                    pass
            shutil.rmtree(travail, ignore_errors=True)
            res.dossier_travail = None