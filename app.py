"""BLUESTAR DESK - Decision Intelligence.

Un seul bouton : la chaine complete s'execute et le desk s'affiche.

    Lancer le desk  ->  4 scanners + calendrier  ->  merge  ->  BLS V10 Pro

Les 7 programmes d'origine sont utilises tels quels, sans une seule
modification. Ce fichier ne fait que les enchainer.
"""

from __future__ import annotations

import datetime
import hashlib
import time
from pathlib import Path

import streamlit as st

from bluestar_chain import ETAPES, executer_chaine, lire_identifiants

st.set_page_config(
    page_title="BLUESTAR DESK - Decision Intelligence",
    page_icon="⭐",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Habillage
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
  .stApp { background: linear-gradient(180deg,#050B1A 0%,#0A1530 100%); }
  #MainMenu, footer, header { visibility: hidden; }

  .bl-hdr { display:flex; align-items:center; gap:18px;
            padding:22px 0 18px; border-bottom:1px solid rgba(90,140,255,.22); }
  /* Logo BLUESTAR, affiche par st.image et non par du HTML : une image
     ne traverse pas le moteur Markdown du navigateur, elle s'affiche donc
     toujours. C'est ce qui manquait au SVG dessine en HTML. */
  .bl-logo { width:52px; height:52px; border-radius:10px;
             overflow:hidden; flex-shrink:0;
             border:1px solid rgba(90,140,255,.30);
             background:rgba(255,255,255,.04); }
  .bl-logo img { width:100%; height:100%; object-fit:cover; display:block; }
  .bl-nom { font-size:25px; font-weight:700; color:#EAF1FF;
            letter-spacing:2.4px; line-height:1.15; }
  .bl-sous { font-size:12.5px; color:#6C86B8; letter-spacing:1.6px;
             text-transform:uppercase; margin-top:3px; }
  .bl-hash { margin-left:auto; text-align:right; font-size:11px;
             color:#4E63A8; }

  .bl-msg { margin:26px 0 0; font-size:15px; color:#9FB4D8; }
  .bl-msg b { color:#EAF1FF; }

  .bl-etape { display:flex; align-items:center; gap:11px; padding:9px 14px;
              margin-bottom:7px; border-radius:9px;
              background:rgba(255,255,255,.028);
              border:1px solid rgba(255,255,255,.05);
              font-size:13.5px; color:#B9C8E4; }
  .bl-etape.ok    { border-color:rgba(0,208,132,.42); background:rgba(0,208,132,.07); }
  .bl-etape.echec { border-color:rgba(255,86,86,.45);  background:rgba(255,86,86,.08); }
  .bl-pastille { width:9px; height:9px; border-radius:50%;
                 background:#3A4A68; flex-shrink:0; }
  .bl-etape.ok    .bl-pastille { background:#00D084; box-shadow:0 0 9px #00D084; }
  .bl-etape.echec .bl-pastille { background:#FF5656; }
  .bl-etape-msg { margin-left:auto; font-size:12px; color:#7C90B4;
                  font-variant-numeric:tabular-nums; }
  .bl-duree { float:right; font-size:12px; color:#7C90B4;
              font-variant-numeric:tabular-nums; }
</style>
""",
    unsafe_allow_html=True,
)

_col_logo, _col_titre = st.columns([1, 14], gap="small")

with _col_logo:
    # Ordre de preference : le .ico (264 Ko, 256x256), puis sa copie PNG,
    # puis l'original 1254x1254 (1,4 Mo). Les trois montrent le meme
    # visuel, verifie : ecart de 0,05 % entre le .ico et le PNG 256.
    # On prend le plus leger d'abord, car le poids est ce qui avait fait
    # echouer le televersement depuis le navigateur.
    _dossier = Path(__file__).resolve().parent
    _logo = None
    for _nom in ("logo.ico", "logo_256.png", "logo.png"):
        _candidat = _dossier / _nom
        if _candidat.is_file():
            _logo = _candidat
            break
    if _logo is not None:
        st.markdown('<div class="bl-logo">', unsafe_allow_html=True)
        st.image(str(_logo), width=52)
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="bl-logo" style="display:flex;align-items:center;'
            'justify-content:center;font-weight:800;color:#EAF1FF;">B</div>',
            unsafe_allow_html=True,
        )

with _col_titre:
    st.markdown(
        """
<div class="bl-hdr">
  <div>
    <div class="bl-nom">BLUESTAR</div>
    <div class="bl-sous">Decision Intelligence</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Verification des identifiants
# ---------------------------------------------------------------------------
try:
    ident = lire_identifiants()
except Exception as exc:
    st.error("Impossible de lire les secrets : " + str(exc))
    st.stop()

manquant = [n for n, v in (("OANDA_ACCESS_TOKEN", ident.jeton),
                           ("OANDA_ACCOUNT_ID", ident.compte)) if not v]

if manquant:
    st.warning(
        "Identifiants OANDA absents : **" + "**, **".join(manquant) + "**\n\n"
        "Renseignez-les dans **Settings -> Secrets** de Streamlit Cloud :"
    )
    st.code(
        'OANDA_ACCESS_TOKEN = "votre_jeton_oanda_practice"\n'
        'OANDA_ACCOUNT_ID   = "101-002-00000000-001"\n'
        'OANDA_ENV = "practice"',
        language="toml",
    )
    st.caption(
        "Ces cles sont celles attendues par vos 4 scanners d'origine (GPS, "
        "CHoCH, RSI, S/R). Le Calendrier, le Merge et BLS V10 Pro n'ont "
        "besoin d'aucun secret."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Etat
# ---------------------------------------------------------------------------
if "resultat" not in st.session_state:
    st.session_state.resultat = None
if "quand" not in st.session_state:
    st.session_state.quand = None

col_g, col_i = st.columns([1, 3])

with col_g:
    lancer = st.button("Lancer le desk", type="primary", use_container_width=True)

with col_i:
    if st.session_state.quand:
        st.caption("Derniere execution : " + st.session_state.quand)
    else:
        st.caption("Aucune execution depuis l'ouverture de cette page.")

if lancer:
    zone = st.container()
    with zone:
        st.markdown("### Execution en cours")
        barre = st.progress(0.0)
        texte = st.empty()
        liste = st.empty()
        chrono = st.empty()

        etats = {c: ("en_attente", "") for c, _ in ETAPES}
        debut = time.monotonic()

        def rendre():
            lignes = []
            for cle, libelle in ETAPES:
                statut, message = etats[cle]
                cls = "bl-etape" + (" ok" if statut == "ok" else
                                    " echec" if statut == "echec" else "")
                lignes.append(
                    '<div class="' + cls + '"><span class="bl-pastille"></span>'
                    "<span>" + libelle + "</span>"
                    '<span class="bl-etape-msg">' + message + "</span></div>"
                )
            liste.markdown("".join(lignes), unsafe_allow_html=True)

        def progression(cle, etape):
            etats[cle] = (etape.statut, etape.message or "")
            if etape.statut == "en_cours":
                n = sum(1 for c, _ in ETAPES if etats[c][0] == "ok")
                texte.markdown(
                    '<div class="bl-msg"><b>' + etape.libelle + "</b> - "
                    + (etape.message or "en cours...") + "</div>",
                    unsafe_allow_html=True,
                )
                barre.progress(min(0.95, (n + 0.4) / len(ETAPES)))
            rendre()

        rendre()
        resultat = executer_chaine(ident, progression=progression)
        st.session_state.resultat = resultat
        st.session_state.quand = datetime.datetime.now().strftime("%d/%m/%Y a %H:%M:%S")

        duree = time.monotonic() - debut
        if resultat.statut == "ok":
            barre.progress(1.0)
            texte.markdown(
                '<div class="bl-msg">Desk genere en <b>' + "{0:.1f}".format(duree)
                + " s</b> - " + "{0:,}".format(resultat.desk_octets).replace(",", " ")
                + " octets</div>",
                unsafe_allow_html=True,
            )
        else:
            texte.markdown(
                '<div class="bl-msg" style="color:#FF8A8A">Arret de la chaine : <b>'
                + resultat.erreur + "</b></div>",
                unsafe_allow_html=True,
            )

# ---------------------------------------------------------------------------
# Resultat
# ---------------------------------------------------------------------------
res = st.session_state.resultat
if res is None:
    st.markdown(
        '<div class="bl-msg">Cliquez sur <b>Lancer le desk</b> pour executer la '
        "chaine complete : 4 scanners en parallele, calendrier, fusion, puis le "
        "desk BLS V10 Pro.</div>",
        unsafe_allow_html=True,
    )
    st.stop()

st.divider()

if res.statut == "ok" and res.desk and Path(res.desk).is_file():
    octets = Path(res.desk).read_bytes()
    c1, c2, c3 = st.columns(3)
    c1.metric("Duree totale", "{0:.1f} s".format(res.duree_s))
    c2.metric("Taille du desk", "{0:,}".format(len(octets)).replace(",", " ") + " o")
    c3.metric("Empreinte SHA-256", res.desk_sha256[:12])

    st.download_button(
        "Telecharger le rapport technique",
        data=octets,
        file_name="Rapport desk technique.html",
        mime="text/html",
        use_container_width=False,
    )

    st.caption("Apercu du desk")

    # On affiche le desk dans une IFRAME et non par st.html.
    #
    # Pourquoi : st.html passe le contenu par DOMPurify en profil « html »
    # (USE_PROFILES: {html: true}), qui SUPPRIME les balises SVG. Le logo
    # en etoile bleue du desk disparaissait donc de l'apercu, alors qu'il
    # est intact dans le fichier telecharge - c'est exactement le cadre
    # blanc sans etoile observe.
    #
    # st.iframe depose le HTML tel quel dans une iframe : le desk s'y
    # affiche complet, avec son logo et ses styles.
    st.iframe(octets.decode("utf-8", errors="replace"), height=900)
else:
    st.error("Chaine interrompue - aucun desk n'a ete produit.")
    st.markdown("**Cause reelle :** " + (res.erreur or "inconnue"))
    st.info(
        "Aucun desk partiel n'est presente : une etape a echoue et la chaine "
        "s'est arretee. Corrigez la cause, puis relancez."
    )

with st.expander("Detail des etapes"):
    for e in res.etapes:
        duree = "" if e.duree_s is None else "  ({0:.1f} s)".format(e.duree_s)
        symbole = {"ok": "[OK]", "echec": "[ECHEC]"}.get(e.statut, "[--]")
        st.markdown(
            "`" + symbole + "` **" + e.libelle + "** - " + (e.message or e.statut) + duree
        )