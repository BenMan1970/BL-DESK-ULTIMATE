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

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("bluestar.chain")

# ---------------------------------------------------------------------------
# Emplacements - tout est relatif au repo, aucun chemin absolu.
# ---------------------------------------------------------------------------
RACINE = Path(__file__).resolve().parent
PROGRAMMES = RACINE / "programmes"



# Un calendrier plus vieux que ce seuil est refuse : le desk serait trompeur.
SEUIL_FRAICHEUR_CALENDRIER_H = 12.0
SEUIL_PEREMPTION_CALENDRIER_H = 48.0

DELAI_SCANNER = 420.0
DELAI_SR = 420.0
DELAI_CALENDRIER = 180.0
DELAI_MERGE = 300.0
DELAI_BLS = 300.0
ORDRE_MERGE = ("gps", "rsi", "sr", "choch")
_MSG_DELAI_SCANNER = (
    "delai depasse ({delai:.0f} s) - le scanner n'a pas rendu la main. "
    "La source a probablement limite le debit (HTTP 429) ; relancez dans "
    "quelques minutes."
)


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------
def _configurer_journal() -> None:
    """Fait apparaitre nos messages dans les logs du serveur.

    Sans cela, sur Streamlit Cloud, un blocage ne laisse AUCUNE trace
    exploitable : on voit seulement le bruit des threads de Streamlit.

    On installe un handler DEDIE sur notre propre logger, sans passer par
    le logger racine. C'est indispensable : deux programmes d'origine
    manipulent le logger racine pendant leur execution (le GPS le met au
    niveau WARNING, le S/R rappelle basicConfig). Sans handler dedie, nos
    messages INFO disparaissent des le demarrage des scanners, et un
    blocage redevient invisible.
    """
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S"
    )
    notre = logging.getLogger("bluestar.chain")
    notre.setLevel(logging.INFO)
    notre.propagate = False
    if not any(getattr(h, "_bluestar", False) for h in notre.handlers):
        manche = logging.StreamHandler()
        manche.setLevel(logging.INFO)
        manche.setFormatter(fmt)
        manche._bluestar = True
        notre.addHandler(manche)
    # Filet de securite : si le logger racine n'a aucun handler, les
    # bibliotheques tierces ecriraient dans le vide.
    racine = logging.getLogger()
    if not racine.handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")


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


def _sha256(chemin: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


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
#
# CHAQUE SCANNER TOURNE DANS SON PROPRE PROCESSUS.
#
# Pourquoi : un scanner bloque sur une requete reseau ne peut pas etre
# arrete depuis un thread Python (un thread ne se tue pas). Sur Streamlit
# Cloud, cela se traduisait par une application qui tourne sans fin, sans
# rien afficher. Dans un processus separe, on peut terminer de force au
# bout du delai : l'application reprend TOUJOURS la main.
#
# Les 7 programmes d'origine ne sont pas modifies : bluestar_workers.py
# les importe et appelle leurs fonctions exactement comme le faisait leur
# interface.
# ---------------------------------------------------------------------------

def _lancer(cle, fonction, args, delai, journal):
    """Execute un travailleur dans un processus separe et rend son bilan."""
    import bluestar_process

    journal("demarrage...")
    t0 = time.monotonic()
    try:
        bilan = bluestar_process.lancer_isole(
            "bluestar_workers", fonction, args, delai,
            dossier=RACINE,
            fichier_retour=Path(tempfile.gettempdir())
            / "bluestar_retour_{0}_{1}.json".format(os.getpid(), cle),
        )
    except TimeoutError:
        raise RuntimeError(_MSG_DELAI_SCANNER.format(delai=delai)) from None
    except Exception as exc:                            # noqa: BLE001
        message = str(exc)
        # Le travailleur prefixe deja ses erreurs par le type ; on evite
        # d'afficher "RuntimeError: RuntimeError: ...".
        if not message.startswith(type(exc).__name__):
            message = "{0}: {1}".format(type(exc).__name__, message)
        raise RuntimeError(message) from exc

    duree = time.monotonic() - t0
    if not isinstance(bilan, dict):
        raise RuntimeError("le processus n'a pas rendu de bilan exploitable")
    for ligne in bilan.get("journal") or []:
        journal(str(ligne)[:160])
    return bilan, duree


def _calendrier(travail, journal):
    bilan, duree = _lancer(
        "calendrier", "etape_calendrier",
        (str(travail), SEUIL_FRAICHEUR_CALENDRIER_H, SEUIL_PEREMPTION_CALENDRIER_H),
        DELAI_CALENDRIER, journal,
    )
    return Path(bilan["cible"]), bilan["message"], bilan.get("age_h"), duree


def _merge(quatre, travail, journal):
    ordre = [c for c in ORDRE_MERGE if c in quatre]
    bilan, duree = _lancer(
        "merge", "etape_merge", (str(travail), ordre), DELAI_MERGE, journal,
    )
    return Path(bilan["cible"]), bilan["message"], duree


def _bls(merged, calendar, travail, journal):
    sortie = Path(travail) / "desk.html"
    bilan, duree = _lancer(
        "bls", "etape_bls", (str(merged), str(calendar), str(sortie)),
        DELAI_BLS, journal,
    )
    return Path(bilan["cible"]), bilan["message"], duree


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

        quatre = {}
        erreurs = {}

        # LES 4 SCANNERS TOURNENT EN PARALLELE, CHACUN DANS SON PROCESSUS.
        #
        # Pourquoi des processus et non des threads : un thread Python ne
        # peut pas etre tue. Si un scanner reste bloque sur une requete
        # reseau (OANDA renvoie parfois 429 ou 504), il tournait sans fin
        # et l'application ne rendait jamais la main. Un processus, lui,
        # se termine de force : l'application reprend TOUJOURS la main.
        import bluestar_process

        contexte = bluestar_process.contexte()
        processus = {}
        retours = {}

        for cle, nom in (("gps", "scanner_gps"), ("choch", "scanner_choch"),
                         ("rsi", "scanner_rsi"), ("sr", "scanner_sr")):
            retours[cle] = Path(tempfile.gettempdir()) / (
                "bluestar_{0}_{1}.json".format(os.getpid(), cle))
            try:
                retours[cle].unlink()
            except OSError:
                pass

        for cle, nom in (("gps", "scanner_gps"), ("choch", "scanner_choch"),
                         ("rsi", "scanner_rsi"), ("sr", "scanner_sr")):
            args = {
                "gps": (str(travail), ident.jeton, ident.compte),
                "choch": (str(travail), ident.jeton, ident.environnement),
                "rsi": (str(travail), ident.jeton, ident.compte,
                        ident.environnement),
                "sr": (str(travail), ident.jeton, ident.compte,
                       ident.environnement),
            }[cle]
            delai = DELAI_SR if cle == "sr" else DELAI_SCANNER
            _maj(cle, "en_cours", "demarrage...")
            processus[cle] = {
                "proc": bluestar_process.demarrer(
                    contexte, nom, args, retours[cle], RACINE, delai),
                "retour": retours[cle],
                "delai": delai,
                "debut": time.monotonic(),
            }

        # On attend les scanners EN PARALLELE : chacun a son propre compte a
        # rebours, et le premier qui rend la main est traite aussitot.
        restants = set(processus)
        while restants:
            for cle in sorted(restants):
                p = processus[cle]
                ecoule = time.monotonic() - p["debut"]
                reste = max(0.0, p["delai"] - ecoule)
                p["proc"].join(min(reste, 1.0))
                if p["proc"].is_alive() and ecoule < p["delai"]:
                    continue
                bilan, erreur = bluestar_process.attendre(
                    p["proc"], p["retour"], 0 if not p["proc"].is_alive() else 1
                )
                restants.discard(cle)
                if erreur:
                    erreurs[cle] = erreur
                    _maj(cle, "echec", erreur, time.monotonic() - p["debut"])
                else:
                    quatre[cle] = Path(bilan["cible"])
                    _maj(cle, "ok", bilan.get("message", ""),
                         round(time.monotonic() - p["debut"], 1))
            if restants:
                time.sleep(0.5)

        if erreurs:
            details = " | ".join(c + ": " + m for c, m in erreurs.items())
            raise RuntimeError("scanner(s) en echec - " + details)

        _maj("calendrier", "en_cours", "ingestion...")
        debut = time.monotonic()
        try:
            cal_path, cal_msg, _age, _d = _calendrier(travail, _journal("calendrier"))
            _maj("calendrier", "ok", cal_msg, time.monotonic() - debut)
        except Exception as exc:
            _maj("calendrier", "echec", str(exc), time.monotonic() - debut)
            raise RuntimeError("calendrier en echec - " + str(exc)) from exc

        _maj("merge", "en_cours", "fusion...")
        debut = time.monotonic()
        try:
            merged, merge_msg, _d = _merge(quatre, travail, _journal("merge"))
            _maj("merge", "ok", merge_msg, time.monotonic() - debut)
        except Exception as exc:
            _maj("merge", "echec", str(exc), time.monotonic() - debut)
            raise RuntimeError("merge en echec - " + str(exc)) from exc

        _maj("bls", "en_cours", "rendu du desk...")
        debut = time.monotonic()
        try:
            desk, bls_msg, _d = _bls(merged, cal_path, travail, _journal("bls"))
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