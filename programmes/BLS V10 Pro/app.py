"""
BLUESTAR ENGINE — Interface Streamlit v10
==========================================

Interface cloud-safe pour ENGINE.V10.py.

Déploiement Streamlit :
- aucune dépendance système obligatoire ;
- WeasyPrint est optionnel ;
- le moteur produit toujours un rapport HTML autonome calibré A4 ;
- si WeasyPrint est absent, le PDF est obtenu via l’impression navigateur.

Le fichier ENGINE.V10.py doit se trouver dans le même répertoire.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import streamlit as st
from streamlit.components.v1 import html as st_html


# ════════════════════════════════════════════════════════════════════════════
# Configuration Streamlit
# Cette instruction doit rester la première commande Streamlit du fichier.
# ════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="BLUESTAR ENGINE v10",
    page_icon="🔵",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ════════════════════════════════════════════════════════════════════════════
# Constantes
# ════════════════════════════════════════════════════════════════════════════

APP_DIRECTORY = Path(__file__).resolve().parent
ENGINE_FILENAME = "ENGINE.V10.py"
ENGINE_MODULE_NAME = "bluestar_engine_v10"

# Doit rester aligné sur ENGINE.V10.MIN_MERGE_SCHEMA (contrat du merged JSON).
MIN_MERGE_SCHEMA = (3, 4, 0)

REQUIRED_ENGINE_CALLABLES = (
    "run_pipeline",
    "render_report",
    "render_pdf",
)


# ════════════════════════════════════════════════════════════════════════════
# Helpers généraux
# ════════════════════════════════════════════════════════════════════════════

def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return "unavailable"


def _parse_schema_version(value: Any) -> tuple[int, int, int] | None:
    """Convertit une version x.y.z en tuple comparable."""
    if value is None:
        return None

    try:
        raw_parts = str(value).strip().split(".")
        padded = (raw_parts + ["0", "0", "0"])[:3]
        return tuple(int(part) for part in padded)
    except (TypeError, ValueError):
        return None


def _decode_json_bytes(
    content: bytes,
    *,
    label: str,
) -> dict[str, Any]:
    """Décode et valide un document JSON racine de type objet."""
    if not content:
        raise ValueError(f"{label} est vide.")

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{label} n'est pas encodé en UTF-8."
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{label} contient un JSON invalide à la ligne "
            f"{exc.lineno}, colonne {exc.colno} : {exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"{label} doit contenir un objet JSON à la racine."
        )

    return data


def _input_fingerprint(
    merged_content: bytes | None,
    calendar_content: bytes | None,
) -> str | None:
    if merged_content is None:
        return None

    digest = hashlib.sha256()
    digest.update(b"BLUESTAR-MERGED\0")
    digest.update(merged_content)
    digest.update(b"\0BLUESTAR-CALENDAR\0")
    digest.update(calendar_content or b"")
    return digest.hexdigest()


def _report_date_from_merged(merged_data: dict[str, Any]) -> str:
    """YYYY.MM.DD depuis meta.generated_at, dans le fuseau du rapport
    (engine.REPORT_TZ — même référence que l'en-tête du document)."""
    generated_at = (merged_data.get("meta") or {}).get("generated_at")

    if generated_at:
        try:
            parsed = datetime.fromisoformat(
                str(generated_at).replace("Z", "+00:00")
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            report_tz = getattr(engine, "REPORT_TZ", timezone.utc)
            return parsed.astimezone(report_tz).strftime("%Y.%m.%d")
        except (TypeError, ValueError):
            pass

    return datetime.now(
        getattr(engine, "REPORT_TZ", timezone.utc)
    ).strftime("%Y.%m.%d")


def _clear_report_state() -> None:
    for key in (
        "report_html",
        "report_base_name",
        "report_pdf_bytes",
        "report_fingerprint",
        "report_generated_at",
        "report_source_label",
    ):
        st.session_state.pop(key, None)


# ════════════════════════════════════════════════════════════════════════════
# ND-011 (11/09/2026) — fraîcheur de l'artefact desk.
# Défaut corrigé : l'app était purement bouton-dépendante ; l'HTML affiché ou
# re-téléchargé pouvait porter l'heure d'une génération antérieure (le 10/09,
# un desk de 13:52 a circulé à 22:53) et l'artefact ne vivait que dans la
# session. ENGINE.V10.py reste INTACT : orchestration seule.
# ════════════════════════════════════════════════════════════════════════════

OUTPUT_DIR = APP_DIRECTORY / "output"


def _latest_disk_file(
    directory_text: str,
    patterns: tuple[str, ...],
) -> "tuple[Path, float] | None":
    """Fichier le plus récent (mtime) du dossier surveillé correspondant à l'un
    des motifs. None si dossier absent/vide. Le producteur amont dépose ses
    merged_pipeline_*.json ici ; l'app les lit sans upload manuel."""
    if not directory_text or not directory_text.strip():
        return None
    directory = Path(directory_text)
    if not directory.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for pattern in patterns:
        for candidate in directory.glob(pattern):
            if candidate.is_file():
                candidates.append((candidate.stat().st_mtime, candidate))
    if not candidates:
        return None
    mtime, path = max(candidates)
    return path, mtime


def _format_age(seconds: "float | None") -> str:
    if seconds is None:
        return "inconnu"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "< 1 min"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _write_artifact(
    html_text: str,
    base_name: str,
    resolved: dict,
) -> None:
    """Écrit le rapport COURANT sur disque (atomique) + sidecar méta. L'aval
    (comité) peut consommer output/latest_desk_report.html et vérifier l'âge
    réel via latest_desk_report.meta.json AVANT de l'utiliser. Best-effort :
    un souci de disque ne casse jamais l'UI."""
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        payload = html_text.encode("utf-8")
        tmp = OUTPUT_DIR / "latest_desk_report.html.tmp"
        tmp.write_bytes(payload)
        os.replace(tmp, OUTPUT_DIR / "latest_desk_report.html")

        source_age = resolved.get("source_age_seconds")
        sidecar = {
            "generated_at_utc": datetime.now(
                timezone.utc
            ).isoformat(timespec="seconds"),
            "report_base_name": base_name,
            "size_bytes": len(payload),
            "input_fingerprint": resolved.get("fingerprint"),
            "source": resolved.get("source_label"),
            "source_age_seconds": source_age,
            "stale": bool(
                source_age is not None
                and source_age > max_source_age_minutes * 60
            ),
        }
        tmp_meta = OUTPUT_DIR / "latest_desk_report.meta.json.tmp"
        tmp_meta.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(
            tmp_meta, OUTPUT_DIR / "latest_desk_report.meta.json"
        )
    except OSError:
        pass


# ════════════════════════════════════════════════════════════════════════════
# Chargement du moteur
# ════════════════════════════════════════════════════════════════════════════

@st.cache_resource(show_spinner=False)
def _load_engine(
    engine_path_string: str,
    source_hash: str,
) -> tuple[ModuleType | None, str | None]:
    """Charge ENGINE.V10.py dans un module isolé.

    source_hash fait partie de la clé du cache. Une modification du fichier
    invalide donc automatiquement la ressource.
    """
    del source_hash

    engine_path = Path(engine_path_string)

    if not engine_path.is_file():
        return None, (
            f"Fichier moteur introuvable : {engine_path.name}. "
            f"Il doit être placé à la racine du dépôt."
        )

    try:
        spec = importlib.util.spec_from_file_location(
            ENGINE_MODULE_NAME,
            engine_path,
        )

        if spec is None or spec.loader is None:
            return None, (
                f"Impossible de créer la spécification d'import pour "
                f"{engine_path.name}."
            )

        module = importlib.util.module_from_spec(spec)

        # Nécessaire pour certains mécanismes Python, notamment les dataclasses.
        sys.modules[ENGINE_MODULE_NAME] = module

        spec.loader.exec_module(module)

        module.__file_hash__ = _file_hash(engine_path)
        module.__loaded_at__ = datetime.now(timezone.utc)

        return module, None

    except Exception as exc:  # noqa: BLE001
        sys.modules.pop(ENGINE_MODULE_NAME, None)
        return None, f"{type(exc).__name__}: {exc}"


ENGINE_PATH = APP_DIRECTORY / ENGINE_FILENAME
ENGINE_HASH = _file_hash(ENGINE_PATH)

engine, engine_error = _load_engine(
    str(ENGINE_PATH),
    ENGINE_HASH,
)


# ════════════════════════════════════════════════════════════════════════════
# Validation de l’API moteur
# ════════════════════════════════════════════════════════════════════════════

engine_api_missing: list[str] = []

if engine is not None:
    engine_api_missing = [
        name
        for name in REQUIRED_ENGINE_CALLABLES
        if not callable(getattr(engine, name, None))
    ]

engine_version = (
    str(getattr(engine, "__version__", "inconnue"))
    if engine is not None
    else "indisponible"
)

has_native_pdf = bool(
    getattr(engine, "_HAS_WEASYPRINT", False)
) if engine is not None else False

native_pdf_error = (
    str(getattr(engine, "_WEASYPRINT_ERROR", "") or "")
    if engine is not None
    else ""
)


# ════════════════════════════════════════════════════════════════════════════
# Sidebar
# ════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## BLUESTAR SYSTEM")
    st.caption("FX Institutional Desk — Hybrid V4")

    if engine is None:
        st.error("Moteur non chargé")
    elif engine_api_missing:
        st.error("Moteur incomplet")
        st.code(
            "\n".join(engine_api_missing),
            language="text",
        )
    else:
        loaded_at = getattr(engine, "__loaded_at__", None)
        displayed_hash = getattr(
            engine,
            "__file_hash__",
            "unavailable",
        )

        st.success(f"Moteur : {ENGINE_FILENAME}")
        st.caption(f"Version : `{engine_version}`")
        st.caption(
            "Hash : `"
            + (
                displayed_hash[:12]
                if displayed_hash != "unavailable"
                else "inconnu"
            )
            + "`"
        )
        st.caption(
            "Chargé : `"
            + (
                loaded_at.strftime("%Y-%m-%d %H:%M UTC")
                if isinstance(loaded_at, datetime)
                else "inconnu"
            )
            + "`"
        )

    st.divider()
    st.markdown("### Rendu PDF")

    if has_native_pdf:
        st.success("Backend PDF natif actif")
    else:
        st.info(
            "Backend PDF natif indisponible.\n\n"
            "Le rapport HTML reste calibré A4. Télécharge-le, "
            "ouvre-le dans un navigateur, puis utilise "
            "**Imprimer → Enregistrer en PDF**."
        )

        if native_pdf_error:
            with st.expander("Détail backend PDF", expanded=False):
                st.code(native_pdf_error, language="text")

    st.divider()
    st.markdown("### Pipeline")
    st.markdown(
        "1. Validation du merged JSON\n"
        "2. Validation du calendrier\n"
        "3. Gates univers\n"
        "4. Facteurs F1 à F7\n"
        "5. Contradictions et conviction\n"
        "6. Preflight et diversification\n"
        "7. Rapport HTML/PDF"
    )

    st.divider()

    with st.expander("Environnement", expanded=False):
        st.caption(f"Python `{platform.python_version()}`")
        st.caption(f"Streamlit `{st.__version__}`")
        st.caption(f"Engine `{engine_version}`")
        st.caption(
            "PDF natif `"
            + ("actif" if has_native_pdf else "inactif")
            + "`"
        )

    st.divider()
    st.markdown("### Rafraîchissement (ND-011)")
    auto_refresh_minutes = st.number_input(
        "Veille auto (minutes, 0 = manuel)",
        min_value=0,
        max_value=240,
        value=5,
        step=1,
        key="auto_refresh_minutes",
        help="Tant qu'un onglet de l'application reste ouvert, les entrées "
             "sont re-vérifiées à cet intervalle et le rapport est régénéré "
             "UNIQUEMENT si les DONNEES ont change (empreinte). Jamais sur "
             "simple ecoulement du temps : l'heure du rapport doit toujours "
             "reflecher l'heure des donnees.",
    )
    data_dir_text = st.text_input(
        "Dossier surveillé (merged JSON)",
        value=os.environ.get("BLUESTAR_DESK_DATA_DIR", ""),
        key="data_dir_text",
        help="Chemin du dossier où le pipeline amont dépose "
             "merged_pipeline_*.json (et calendar*.json). L'upload manuel "
             "reste prioritaire. Env BLUESTAR_DESK_DATA_DIR par défaut.",
    )
    max_source_age_minutes = st.number_input(
        "Données jugées périmées au-delà (minutes)",
        min_value=5,
        max_value=1440,
        value=60,
        step=5,
        key="max_source_age_minutes",
        help="Au-delà de cet âge des données sources (mtime du merged), "
             "bandeau rouge « HEURE DÉPASSÉE » et champ stale=true dans le "
             "sidecar output/latest_desk_report.meta.json.",
    )

    if st.button(
        "Vider les caches",
        use_container_width=True,
    ):
        _clear_report_state()
        st.cache_resource.clear()
        st.cache_data.clear()
        st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# Header et erreurs moteur
# ════════════════════════════════════════════════════════════════════════════

st.title(
    f"BLUESTAR ENGINE v{engine_version}"
    if engine is not None
    else "BLUESTAR ENGINE"
)

st.caption(
    "FX Institutional Desk — "
    "Hybrid Absolute/Cross-Sectional V4"
)

if engine is None:
    st.error("Le moteur n’a pas pu être chargé.")

    if engine_error:
        st.code(engine_error, language="text")

    st.info(
        f"Vérifie que `{ENGINE_FILENAME}` est présent à la racine "
        "du dépôt et que toutes ses dépendances figurent dans "
        "`requirements.txt`."
    )

    st.stop()

if engine_api_missing:
    st.error(
        f"`{ENGINE_FILENAME}` est incomplet ou a été tronqué."
    )
    st.code(
        "Fonctions obligatoires absentes :\n"
        + "\n".join(
            f"- {name}"
            for name in engine_api_missing
        ),
        language="text",
    )
    st.info(
        "Le moteur doit contenir `run_pipeline`, `render_report` "
        "et `render_pdf`."
    )
    st.stop()

run_pipeline = getattr(engine, "run_pipeline")

# Journal de calibration v10 — le moteur append une ligne par actif a
# chaque run si V10_JOURNAL_CSV est defini. Chemin par defaut : a cote de
# l'app ; desactivable via "set V10_JOURNAL_CSV=" avant lancement.
os.environ.setdefault(
    "V10_JOURNAL_CSV",
    str(Path(__file__).resolve().parent / "v10_journal.csv"),
)


# ════════════════════════════════════════════════════════════════════════════
# Uploads
# ════════════════════════════════════════════════════════════════════════════

upload_column_1, upload_column_2 = st.columns(2)

with upload_column_1:
    merged_file = st.file_uploader(
        "Merged JSON",
        type=["json", "txt"],
        key="merged_json_upload",
        help=(
            "Fichier merged du pipeline "
            "(ex. merged_pipeline_*.json). "
            f"Schéma minimum requis : "
            f"{'.'.join(map(str, MIN_MERGE_SCHEMA))} — bloquant en dessous."
        ),
    )

with upload_column_2:
    calendar_file = st.file_uploader(
        "Calendar JSON — optionnel",
        type=["json", "txt"],
        key="calendar_json_upload",
        help=(
            "Calendrier économique wrapper ou CalendarData natif. "
            "En son absence, F7 MACRO fonctionne en fail-closed."
        ),
    )

merged_bytes = (
    merged_file.getvalue()
    if merged_file is not None
    else None
)

calendar_bytes = (
    calendar_file.getvalue()
    if calendar_file is not None
    else None
)


# ════════════════════════════════════════════════════════════════════════════
# Validation et aperçu des entrées
# ════════════════════════════════════════════════════════════════════════════

merged_data: dict[str, Any] | None = None
calendar_data: dict[str, Any] | None = None
input_errors: list[str] = []
input_warnings: list[str] = []

if merged_bytes is not None:
    try:
        merged_data = _decode_json_bytes(
            merged_bytes,
            label="Merged JSON",
        )

        meta = merged_data.get("meta")
        assets = merged_data.get("assets")
        signals = merged_data.get("signals")

        if not isinstance(meta, dict):
            input_errors.append(
                "Merged JSON : le champ `meta` est absent ou invalide."
            )
            meta = {}

        if not isinstance(assets, dict):
            input_errors.append(
                "Merged JSON : le champ `assets` doit être un objet."
            )
            assets = {}

        if not assets:
            input_errors.append(
                "Merged JSON : aucun actif n’est disponible."
            )

        if signals is None:
            signals = []
        elif not isinstance(signals, list):
            input_warnings.append(
                "Merged JSON : le champ `signals` n’est pas une liste."
            )
            signals = []

        schema_raw = meta.get("version")
        schema_version = _parse_schema_version(schema_raw)

        if schema_version is None:
            input_errors.append(
                "Merged JSON : `meta.version` est absent ou non parseable."
            )
        elif schema_version < MIN_MERGE_SCHEMA:
            input_errors.append(
                f"Merged JSON : schéma {schema_raw} inférieur au minimum "
                f"{'.'.join(map(str, MIN_MERGE_SCHEMA))}."
            )

        generated_at = meta.get("generated_at")
        if generated_at is None:
            input_warnings.append(
                "Merged JSON : `meta.generated_at` est absent."
            )

    except ValueError as exc:
        input_errors.append(str(exc))

if calendar_bytes is not None:
    try:
        calendar_data = _decode_json_bytes(
            calendar_bytes,
            label="Calendar JSON",
        )

        has_wrapper_events = isinstance(
            calendar_data.get("events_engine"),
            list,
        ) or isinstance(
            calendar_data.get("events"),
            list,
        )

        if not has_wrapper_events:
            input_warnings.append(
                "Calendar JSON : aucune liste `events_engine` ou `events` "
                "n’a été détectée. Le moteur tentera le format natif."
            )

    except ValueError as exc:
        input_errors.append(str(exc))


if merged_data is not None:
    with st.expander(
        "Aperçu et validation des données",
        expanded=True,
    ):
        meta = (
            merged_data.get("meta")
            if isinstance(merged_data.get("meta"), dict)
            else {}
        )
        assets = (
            merged_data.get("assets")
            if isinstance(merged_data.get("assets"), dict)
            else {}
        )
        signals = (
            merged_data.get("signals")
            if isinstance(merged_data.get("signals"), list)
            else []
        )

        scanners = meta.get("scanners_detected")
        if not isinstance(scanners, list):
            scanners = []

        metric_1, metric_2, metric_3, metric_4 = st.columns(4)

        metric_1.metric(
            "Schéma",
            str(meta.get("version") or "N/A"),
        )
        metric_2.metric(
            "Actifs",
            len(assets),
        )
        metric_3.metric(
            "Signaux",
            len(signals),
        )
        metric_4.metric(
            "Scanners",
            len(scanners),
        )

        if assets:
            st.markdown(
                "**Premiers actifs :** "
                + ", ".join(
                    list(map(str, assets.keys()))[:10]
                )
            )

        if calendar_data is not None:
            metadata = calendar_data.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}

            events = calendar_data.get("events_engine")
            if not isinstance(events, list):
                events = calendar_data.get("events")
            if not isinstance(events, list):
                events = []

            st.markdown(
                f"**Calendrier :** {len(events)} événement(s) détecté(s)"
            )

            generated_calendar = metadata.get("generated_at_utc")
            if generated_calendar:
                st.caption(
                    f"Généré : `{generated_calendar}`"
                )

for warning in input_warnings:
    st.warning(warning)

for error in input_errors:
    st.error(error)


# ════════════════════════════════════════════════════════════════════════════
# Session fraîcheur (ND-011) : entrées effectives, génération, preview
# ════════════════════════════════════════════════════════════════════════════

def _validate_disk_merged(data: dict) -> list:
    """Validation minimale du merged lu sur disque (mêmes contrats que
    l’upload : meta dict, assets non vide, schéma >= MIN_MERGE_SCHEMA)."""
    errors: list = []
    meta = data.get("meta")
    assets = data.get("assets")
    if not isinstance(meta, dict):
        return ["Merged JSON (disque) : `meta` absent ou invalide."]
    if not isinstance(assets, dict) or not assets:
        return ["Merged JSON (disque) : aucun actif disponible."]
    schema_version = _parse_schema_version(meta.get("version"))
    if schema_version is None:
        errors.append("Merged JSON (disque) : `meta.version` absente.")
    elif schema_version < MIN_MERGE_SCHEMA:
        errors.append(
            "Merged JSON (disque) : schéma "
            + str(meta.get("version"))
            + " < minimum "
            + ".".join(map(str, MIN_MERGE_SCHEMA))
            + "."
        )
    return errors


def _effective_inputs() -> dict:
    """Upload prioritaire ; sinon fichier le plus récent du dossier surveillé.
    La clé d’auto-raîchissement est l’empreinte des DONNÉES (contenu), pas
    l’horloge : un merged inchangé ne régénère jamais — l’heure affichée doit
    rester l’heure des données."""
    if merged_bytes is not None:
        return {
            "merged_bytes": merged_bytes,
            "merged_data": merged_data,
            "calendar_bytes": calendar_bytes,
            "fingerprint": _input_fingerprint(merged_bytes, calendar_bytes),
            "source_label": "upload « " + merged_file.name + " »",
            "source_age_seconds": None,
            "errors": list(input_errors),
        }

    found = _latest_disk_file(
        data_dir_text, ("merged*.json", "merged*.txt", "*merged*.json")
    )
    if found is None:
        return {
            "merged_bytes": None,
            "merged_data": None,
            "calendar_bytes": None,
            "fingerprint": None,
            "source_label": "aucune source",
            "source_age_seconds": None,
            "errors": [],
        }

    path, mtime = found
    source_age = (
        datetime.now(timezone.utc)
        - datetime.fromtimestamp(mtime, timezone.utc)
    ).total_seconds()

    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {
            "merged_bytes": None,
            "merged_data": None,
            "calendar_bytes": None,
            "fingerprint": None,
            "source_label": path.name,
            "source_age_seconds": source_age,
            "errors": ["Lecture du merged surveillé impossible : " + str(exc)],
        }

    try:
        data = _decode_json_bytes(raw, label="Merged JSON (disque)")
    except ValueError as exc:
        return {
            "merged_bytes": None,
            "merged_data": None,
            "calendar_bytes": None,
            "fingerprint": None,
            "source_label": path.name,
            "source_age_seconds": source_age,
            "errors": [str(exc)],
        }

    cal_bytes = None
    cal_found = _latest_disk_file(data_dir_text, ("calendar*.json",))
    if cal_found is not None:
        try:
            cal_bytes = cal_found[0].read_bytes()
        except OSError:
            cal_bytes = None

    return {
        "merged_bytes": raw,
        "merged_data": data,
        "calendar_bytes": cal_bytes,
        "fingerprint": _input_fingerprint(raw, cal_bytes),
        "source_label": path.name,
        "source_age_seconds": source_age,
        "errors": _validate_disk_merged(data),
    }


def _generate_report(resolved: dict):
    """Lance run_pipeline sur les entrées résolues ; met à jour la session et
    l’artefact output/. Retour None si OK, sinon le message d’erreur."""
    if resolved["merged_bytes"] is None or resolved["errors"]:
        return "aucune entrée exploitable"

    with tempfile.TemporaryDirectory(prefix="bluestar_") as temporary_directory:
        try:
            temporary_path = Path(temporary_directory)
            merged_path = temporary_path / "merged.json"
            calendar_path = temporary_path / "calendar.json"
            output_path = temporary_path / "report.html"
            pdf_path = temporary_path / "report.pdf"

            merged_path.write_bytes(resolved["merged_bytes"])

            pipeline_arguments = {
                "merged_path": str(merged_path),
                "output_path": str(output_path),
            }

            if has_native_pdf:
                pipeline_arguments["pdf_path"] = str(pdf_path)

            if resolved["calendar_bytes"] is not None:
                calendar_path.write_bytes(resolved["calendar_bytes"])
                pipeline_arguments["calendar_json_path"] = str(calendar_path)

            report_html = run_pipeline(**pipeline_arguments)

            if not isinstance(report_html, str) or not report_html.strip():
                raise RuntimeError("Le moteur n’a retourné aucun HTML.")

            normalized_html = report_html.lower()
            if (
                "<html" not in normalized_html
                or "</html>" not in normalized_html
            ):
                raise RuntimeError(
                    "Le résultat du moteur n’est pas un document HTML complet."
                )

            report_pdf_bytes = None
            if pdf_path.is_file() and pdf_path.stat().st_size > 0:
                report_pdf_bytes = pdf_path.read_bytes()
                if not report_pdf_bytes.startswith(b"%PDF-"):
                    report_pdf_bytes = None

            report_date = _report_date_from_merged(resolved["merged_data"])
            base_name = "BLUESTAR FX Desk_Signal Report_" + report_date

            st.session_state["report_base_name"] = base_name
            st.session_state["report_html"] = report_html
            st.session_state["report_pdf_bytes"] = report_pdf_bytes
            st.session_state["report_fingerprint"] = resolved["fingerprint"]
            st.session_state["report_generated_at"] = datetime.now(
                timezone.utc
            ).isoformat(timespec="seconds")
            st.session_state["report_source_label"] = resolved["source_label"]

            _write_artifact(report_html, base_name, resolved)
            return None

        except Exception as exc:  # noqa: BLE001
            _clear_report_state()
            return type(exc).__name__ + ": " + str(exc)


def _freshness_banner(resolved: dict) -> None:
    """Cœur de ND-011 : l’âge des DONNÉES est affiché en permanence — plus
    jamais un HTML à heure dépassée ne circule sans marqueur visible."""
    if not st.session_state.get("report_html"):
        st.info(
            "Aucun rapport dans cette session — « Générer le rapport », ou "
            "déposer un merged JSON dans le dossier surveillé (le "
            "rafraîchissement automatique le prendra en charge)."
        )
        return

    gen_txt = "inconnue"
    generated_at = st.session_state.get("report_generated_at")
    if isinstance(generated_at, str):
        try:
            gen_dt = datetime.fromisoformat(generated_at)
            gen_age = (
                datetime.now(timezone.utc) - gen_dt
            ).total_seconds()
            gen_txt = (
                gen_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                + " (âgé de " + _format_age(gen_age) + ")"
            )
        except ValueError:
            pass

    src_txt = "source : " + str(resolved["source_label"])
    if resolved["source_age_seconds"] is not None:
        src_txt += (
            ", données âgées de "
            + _format_age(resolved["source_age_seconds"])
        )

    stale = (
        resolved["source_age_seconds"] is not None
        and resolved["source_age_seconds"] > max_source_age_minutes * 60
    )
    if stale:
        st.error(
            "⛔ DONNÉES SOURCES PÉRIMÉES (seuil "
            + str(int(max_source_age_minutes))
            + " min) — ce rapport porte une HEURE DÉPASSÉE ; ne pas le "
            "transmettre au comité en l’état. " + src_txt
            + ". Généré : " + gen_txt + "."
        )
    else:
        veille = (
            " Veille auto toutes les "
            + str(int(auto_refresh_minutes))
            + " min."
            if auto_refresh_minutes
            else ""
        )
        st.success(
            "🟢 Rapport courant — " + src_txt + ". Généré : " + gen_txt + "."
            + veille
        )


@st.fragment(
    run_every=(
        int(auto_refresh_minutes) * 60 if auto_refresh_minutes else None
    )
)
def _desk_session() -> None:
    resolved = _effective_inputs()
    _freshness_banner(resolved)

    # En mode dossier surveillé, les erreurs de validation n'ont pas été
    # affichées au niveau racine (boucle réservée à l'upload) : les rendre ici.
    if merged_bytes is None:
        for error in resolved['errors']:
            st.error(error)

    stored_fingerprint = st.session_state.get("report_fingerprint")
    if (
        stored_fingerprint is not None
        and resolved["fingerprint"] is not None
        and stored_fingerprint != resolved["fingerprint"]
    ):
        _clear_report_state()

    # Auto-regeneration : UNIQUEMENT quand des donnees nouvelles (ou jamais
    # exploitees) sont presentes — jamais pour « rajeunir » l’horodatage sur
    # des donnees inchangees.
    auto_error = None
    if (
        resolved["merged_bytes"] is not None
        and not resolved["errors"]
        and st.session_state.get("report_fingerprint") is None
    ):
        auto_error = _generate_report(resolved)
        if auto_error is None:
            st.toast("Rapport régénéré automatiquement (données nouvelles).")

    generation_disabled = (
        resolved["merged_bytes"] is None or bool(resolved["errors"])
    )
    generate_clicked = st.button(
        "Générer le rapport",
        type="primary",
        use_container_width=True,
        disabled=generation_disabled,
    )

    if generate_clicked:
        with st.spinner(
            "Scoring, contrôles de risque et génération du rapport..."
        ):
            error = _generate_report(resolved)
        if error:
            st.error("Erreur pipeline : " + error)

    if auto_error:
        st.error("Régénération automatique échouée : " + str(auto_error))

    # ─── Aperçu (vit dans le fragment : il se rafraîchit à chaque veille) ─
    report_html_state = st.session_state.get("report_html")

    if isinstance(report_html_state, str) and report_html_state:
        preview_tab, source_tab = st.tabs(["Aperçu", "Diagnostic HTML"])

        with preview_tab:
            st_html(report_html_state, height=1800, scrolling=True)

        with source_tab:
            st.metric(
                "Taille HTML",
                str(len(report_html_state)) + " caractères",
            )
            st.code(
                report_html_state[:5000]
                + (
                    "\n\n<!-- aperçu tronqué -->"
                    if len(report_html_state) > 5000
                    else ""
                ),
                language="html",
            )

        st.caption(
            "Artefact courant : "
            + str(OUTPUT_DIR / "latest_desk_report.html")
            + " — mis à jour à chaque génération ; le sidecar "
            "latest_desk_report.meta.json porte l’horodatage exact et le "
            "flag stale (à vérifier par l’aval avant usage)."
        )
    elif resolved["merged_bytes"] is None:
        st.info(
            "Charge un merged JSON (upload ou dossier surveillé) pour lancer "
            "le pipeline."
        )
    elif resolved["errors"]:
        st.info(
            "Corrige les erreurs de validation avant de lancer le pipeline."
        )


_desk_session()

# ════════════════════════════════════════════════════════════════════════════
# Téléchargements — niveau racine. Les st.download_button restent VOLONTAI-
# REMENT hors du fragment (compatibilité stricte toutes versions Streamlit ;
# leur re-déclenchement complet n’est jamais requis). La source constante
# pour l’automatisation reste output/latest_desk_report.html (+ sidecar).
# ════════════════════════════════════════════════════════════════════════════

report_html_state = st.session_state.get("report_html")

if isinstance(report_html_state, str) and report_html_state:
    report_base_name = str(
        st.session_state.get(
            "report_base_name",
            "BLUESTAR FX Desk_Signal Report",
        )
    )
    report_pdf_state = st.session_state.get("report_pdf_bytes")

    st.divider()

    download_column_1, download_column_2, download_column_3 = st.columns(3)

    _journal_path = Path(__file__).resolve().parent / "v10_journal.csv"

    with download_column_3:
        st.download_button(
            label="Journal de calibration v10 (CSV)",
            data=(
                _journal_path.read_bytes()
                if _journal_path.exists()
                else b""
            ),
            file_name=(
                "v10_journal_"
                + format(datetime.now(timezone.utc), "%Y%m%d_%H%M%S")
                + "Z.csv"
            ),
            mime="text/csv",
            use_container_width=True,
            disabled=not _journal_path.exists(),
            help="Une ligne par actif et par scan : decisions completes du "
                 "moteur. A conserver : sert a etalonner age et calendrier "
                 "sur donnees reelles (30/60/90 j).",
        )

    with download_column_1:
        st.download_button(
            label="Télécharger le rapport HTML A4",
            data=report_html_state.encode("utf-8"),
            file_name=report_base_name + ".html",
            mime="text/html",
            use_container_width=True,
        )

    with download_column_2:
        if (
            isinstance(report_pdf_state, bytes)
            and report_pdf_state.startswith(b"%PDF-")
        ):
            st.download_button(
                label="Télécharger le rapport PDF",
                data=report_pdf_state,
                file_name=report_base_name + ".pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        else:
            st.info(
                "PDF natif indisponible sur cet environnement. "
                "Télécharge le HTML, ouvre-le dans un navigateur, "
                "puis utilise **Imprimer → Enregistrer en PDF**, "
                "format A4, échelle 100 %."
            )






