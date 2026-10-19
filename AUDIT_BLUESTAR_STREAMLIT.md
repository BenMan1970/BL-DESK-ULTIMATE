# AUDIT — BLUESTAR DESK sur Streamlit Community Cloud
**Date :** 18 octobre 2026 — **Périmètre :** diagnostic en lecture seule. Aucune ligne de code modifiée (vérifié : `git status` vierge avant ET après l'audit, `verifier_integrite.py` = 10/10 fichiers conformes).

---

## 1. Verdict synthétique

Le dépôt n'a **aucun défaut structurel de déploiement** : versions installables, Python 3.12, fichiers complets, chaîne mécaniquement fonctionnelle sous Streamlit 1.64.0. Les erreurs au lancement viennent de **quatre causes opérationnelles**, dont trois reproduites en conditions réelles pendant l'audit :

| # | Cause reproduite | Symptôme exact dans l'app |
|---|---|---|
| 1 | Jeton/compte OANDA qui n'autorise pas `practice` | `scanner(s) en echec - sr: OandaAuthError: Auth OANDA echouee` |
| 2 | **Calendrier : HTTP 429 de Fair Economy (REPRODUIT)** | `calendrier en echec - calendrier indisponible (limite de debit atteinte) - relancez dans quelques minutes` |
| 3 | Scan CHoCH ou S/R sans aucun signal → merge 3/4 | `merge en echec - merge incomplet : 3/4 scanners reconnus` |
| 4 | Double-clic < 30 s (cooldown RSI) | export RSI vide → échec déplacé au merge |

---

## 2. Ce qui a été testé (et le résultat)

| Contrôle | Méthode | Résultat |
|---|---|---|
| Existence des 17 versions épinglées sur PyPI | API PyPI, une requête par version | **17/17 existent** |
| Roues cp312 manylinux x86_64 (Cloud = Linux/Py3.12) | API PyPI, inventaire des fichiers | **OK** pour numpy 2.5.3, pandas 3.0.6, scipy 1.18.1, pyarrow 25.0.1, matplotlib 3.11.2, aiohttp 3.14.5 ; purs pour le reste. `oandapyV20` se compile depuis sa source (pur Python, **installé et testé : fonctionne**) |
| Compilation Python 3.12.9 | `compileall` sur tout le dépôt | **0 erreur** |
| Intégrité des 7 programmes d'origine | `verifier_integrite.py` | **10/10 conformes** |
| Complétude du dépôt Git | `git ls-files` | 66 fichiers, arbre propre, `programmes/` complet, secrets non versionnés |
| Version Python | `runtime.txt` + `.python-version` | `python-3.12` (cohérent avec votre environnement) |
| Import des 4 scanners **dans les threads du pool** (conditions Cloud) | AppTest Streamlit 1.64.0 (contexte de session sur le thread principal uniquement) + chaîne réelle `executer_chaine` | **Aucun plantage.** Les commandes Streamlit au niveau module des apps RSI et S/R (`st.set_page_config`, `st.title`, `st.button`, `st.session_state`) se dégradent en avertissements `missing ScriptRunContext` — pas d'exception. ⚠️ Ce comportement dépend de la version de Streamlit : **ne descendez pas `streamlit` sous 1.64.0** |
| oandapyV20 0.7.2 + requests 2.32.4 | Appel réel à `api-fxpractice.oanda.com` | Réseau OK, erreur d'auth propre (`V20Error`) — la lib n'est pas cassée |
| Chaîne complète (calendrier → merge → BLS) | `executer_chaine` en conditions réelles | Mécanique validée ; exports GPS/RSI/CHoCH/SR produits, sérialisation OK |
| **Structured output du calendrier** | seed + fetch live → `CalendarPayload` → `to_legacy_payload` → `ENGINE.V10.load_calendar` | **FONCTIONNE — détail § 4** |
| Fetch calendrier live | `run_once()` sur répertoire neuf | 20 événements, VALID, score 1.00, fraîcheur 0 h |

---

## 3. Les 4 causes de panne, diagnostic et conduite à tenir

### Cause 1 — les secrets OANDA n'autorisent pas `practice` (la plus probable si « les secrets sont dans Streamlit »)

Reproduit avec un jeton factice :

```
[sr]  echec — OandaAuthError: Auth OANDA echouee
[choch] ok — 0 signaux            (auth_aborted=true, 132 requêtes avortées)
[gps] ok — 33 elements            (exporte des lignes d'erreur comme des données)
[rsi] ok — 33 instruments         (idem, completion 0.0%)
```

Le S/R est le seul scanner qui **échoue net** (il teste l'auth explicitement). Les trois autres exportent des documents techniquement valides mais vides d'information — la chaîne s'arrête alors sur S/R, pas sur eux.

**À vérifier, dans l'ordre :**
1. Le jeton vient-il bien du **compte de démonstration (practice)** ? (OANDA → demo account → *Manage API Access* → *Generate token*)
2. Le compte ID a-t-il la forme d'un compte **practice** : `101-002-...` ? (Les comptes live sont `001-...` / `101-001-...`)
3. `OANDA_ENV = "practice"` est bien présent dans les secrets (si absent, le repli est `practice` de toute façon).
4. Un jeton live interrogé sur `practice` (ou l'inverse) donne exactement cette erreur.

**Où lire la vraie cause :** Streamlit Cloud → *Manage app* → **Logs**. La chaîne journalise chaque étape (`[sr] echec - ...`). C'est le diagnostic le plus rapide — copiez-moi la ligne fautive.

### Cause 2 — le calendrier est rate-limité (429) par Fair Economy — **REPRODUIT PENDANT L'AUDIT**

Après deux fetches espacés de quelques minutes, la source a répondu 429 :

```
WARNING | pipeline_calendar | rate-limited — cooldown 600s until ...
```

→ `run_once()` rend `None` → `_calendrier()` lève :

```
calendrier en echec - calendrier indisponible (limite de debit atteinte) - relancez dans quelques minutes
```

Sur Community Cloud, l'IP est partagée : ce 429 est ** Intermittent**, pas systématique (le même test a réussi 3 minutes avant). **Conduite : attendre 5–10 min et relancer.** Ce n'est pas une panne du code. Chaque exécution Streamlit repart d'un répertoire de travail neuf : le cooldown de 600 s n'est donc jamais « collant » entre deux relances.

### Cause 3 — un scanner sans aucun signal fait échouer le merge (piège structurel)

Le fixateur d'adaptateurs du merge exige des listes **non vides** :

- `CHoCHAdapter.detect()` : `if not sigs: return "no signals list"` → si CHoCH ne trouve **aucune** structure (semaine calme, légitime), le merge ne compte que 3/4 → `merge en echec - merge incomplet : 3/4 scanners reconnus` → aucun desk.
- `SRAdapter.detect()` : même mécanisme sur `assets` (aucune zone trouvée).

Reproduit : avec des exports réels mais vides côté CHoCH/SR → `merge incomplet : 2/4 scanners reconnus`.

**Conduite :** relancer ; si CHoCH rend systématiquement 0 signal en semaine, le problème est réel (mais le corriger suppose de toucher au code — hors du périmètre que vous avez fixé).

### Cause 4 — double-clic ou relance < 30 s : RSI vide silencieux

`SERVER_COOLDOWN = 30.0` dans le RSI. Une seconde exécution dans la foulée rend `{"cooldown": s, "results": None}` → la chaîne exporte un document RSI « valide » à 0 instrument (`rsi : ok — 0 instruments`) → l'échec réapparaît plus tard, au merge (`3/4 scanners reconnus`). **Conduite : attendre 30 s minimum entre deux lancements.**

---

## 4. Structured output du calendrier — VERDICT : il fonctionne

Testé dans les deux sens, producteur et consommateur, sur le seed du dépôt **et** sur un fetch live réel.

**Contrat de sortie (produit par `to_legacy_payload`, fichier `calendar.json`) :**

```
metadata   : 80 champs (schema_version "legacy-1.2.0+core-2.4.2", generated_at_utc,
             content_hash, quality_status, filters_applied{currencies,impact_levels},
             data_coverage_*, feed_*, upcoming_count, high_impact_count, ...)
events     : [ {occurrence_id, event_type_id, release_group_id, currency, event_name,
               datetime_utc, impact, forecast/previous/actual (+_value/_status),
               hours_until, priority, is_upcoming, time_proximity, status, session,
               pairs_affected, ...} ]
events_engine : liste identique (consommée en priorité par BLS)
summary_by_day, events_upcoming (additif)
```

**Vérifications effectuées :**

| Test | Résultat |
|---|---|
| `CalendarPayload.model_validate(seed)` (producteur, pydantic 2.14.1) | OK — 22 événements, quality VALID, score 1.0 |
| `to_legacy_payload()` | 22 rows, 80 champs metadata, hash cohérent |
| Chaque row → `ENGINE.V10.CalendarEvent.model_validate()` (**ce que fait `load_calendar()`**) | **22/22 OK, 0 échec** — idem sur le fetch live (20/20) |
| `load_calendar()` puis `bucket()` | 22 événements chargés, 6 devises couvertes, blackout/proximity/watch/watch_ccy calculés |
| `audit_calendar_time_consistency()` (P0-A fuseaux) | offset −748 h sur le **seed** (artefact du 17/09, périmé) → fail-closed correctement déclenché ; offset nul sur le flux live |
| `run_once()` live (comme la chaîne) | **20 événements publiés, VALID, serving_mode=live, âge 0,00 h** → passe les seuils de la chaîne (12 h frais / 48 h péremption) |
| Seuils de fraîcheur de la chaîne (`_age_heures`) | 0,00 h < 12 h → statut OK |

**Conclusion :** le structured output du calendrier est **structurellement sain et compatible bout-en-bout** (calendar_core 2.4.2 → ENGINE.V10 10.6.2). Le seul mode de panne du calendrier est le **429 réseau** (cause 2), pas le format.

**Note :** le seed présent dans votre copie de travail (`programmes/Calendrier/data/seed/calendar.latest.seed.json`) date du **17/09/2026** (748 h). Il est dans `.gitignore` : Streamlit Cloud ne l'a donc pas, et la chaîne ne l'utilise jamais (elle fait un fetch live à chaque exécution). Aucun impact sur le desk — mais n'utilisez pas ce seed comme source de secours manuelle.

---

## 5. Fragilités latentes (à connaître, hors périmètre « ne rien changer »)

1. **Fail-open RSI/GPS** : en cas d'auth invalide, ces scanners exportent des lignes d'erreur comme des données et passent `ok`. Si seul S/R est temporairement tolérant, un desk peut être produit sur des données vides. Le merge/BLS ne détectent pas ce cas comme une erreur.
2. **`os.chdir` global** dans `_charger_module` exécuté depuis 4 threads simultanés : racé mais sans conséquence ici (tous les programmes utilisent des chemins absolus).
3. **`preparer_environnement` écrit dans `os.environ` process-global** : en déploiement multi-utilisateurs, deux sessions simultanées pourraient se croiser (une seule valeur OANDA_ENV pour toutes).
4. **La pin `streamlit==1.64.0` est load-bearing** : les imports de modules à effets Streamlit dans les threads ne passent que sur cette génération de Streamlit. Toute « simplification » de requirements casse la chaîne.
5. Le `params_final.json` absent du GPS (repli usine silencieux) — comportement documenté du programme, sans impact.

---

## 6. Diagnostic en 2 minutes sur votre déploiement

1. Ouvrez **Manage app → Logs** (pas l'interface).
2. Cliquez « Lancer le desk », puis lisez la **première** ligne `[xxx] echec` :
   - `sr: OandaAuthError` → **cause 1** (jeton/compte practice).
   - `calendrier en echec - limite de debit` → **cause 2** (attendre 10 min, relancer).
   - `merge en echec - merge incomplet : 3/4` → **cause 3** (un scanner à 0 signal).
   - `rsi : ok — 0 instruments` + merge 3/4 → **cause 4** (relance trop rapide).
3. Si tout est `ok` mais l'aperçu n'affiche rien : `st.html` existe bien en 1.64.0 (vérifié), le problème est alors dans le contenu du desk — remontez-moi la ligne `[bls]`.

---

*Audit réalisé sans modification du dépôt. Tests exécutés dans un venv isolé avec la pile épinglée (streamlit 1.64.0, pandas 3.0.6, pydantic 2.14.1, matplotlib 3.11.2, requests 2.32.4, oandapyV20 0.7.2, numpy 2.5.4, scipy 1.18.1).*

---

## 7. Correctifs appliqués (addendum post-audit)

**Déclencheur :** accord explicite de l'utilisateur (« Applique direct »).
**Périmètre :** `bluestar_chain.py` **uniquement**. Les 7 programmes d'origine n'ont pas été touchés — `verifier_integrite.py` reste **10/10 conformes** après patch.

`md5` 122e29925f6e075bec72887973e81c23 → **319348526383e416e5431c00ff8e5853**
28 869 octets · 742 lignes · **+92 / −6**

### Patch 1 — `_scanner_rsi` : un refus n'est pas un scan vide

`run_scan()` peut rendre `None` (verrou de scan actif), `{"cooldown": s, "results": None}` (refus trop rapproché) ou un `results` vide. Ces trois cas produisaient un export « ok » trompeur. Ils lèvent désormais une `RuntimeError` explicite (verrou / cooldown avec secondes restantes / jeton OANDA). Le chemin succès est inchangé.

### Patch 2 — `_merge` : nommer les scanners manquants

Le message `3/4 scanners reconnus` ne disait pas *lesquels*. Il liste maintenant les reconnus et les non reconnus, et explique la cause : un scanner non reconnu a produit un export vide (aucun signal ce jour, ou jeton non autorisé).

### Patch 3 — `_calendrier` : distinguer la vraie cause

Lecture de `health.json` (`status` / `mode` / `error` / `rate_limit`) au lieu d'un message unique « aucun fichier ». Quatre cas distincts :
- **HTTP 429** → cooldown avec l'heure de fin UTC et la durée restante ;
- **`mode=floor`** → plancher anti-429, relancer dans 1–2 min ;
- **`mode=reader`** → ingestion désactivée (`BLUESTAR_DISABLE_INGEST=1`) ;
- **erreur source** → message d'erreur réel (jamais présenté comme un 429).

### Vérifications

| Contrôle | Résultat |
|---|---|
| Batterie de non-régression (A1–A4, B1–B3, C1–C4) | **11 / 11** |
| AppTest chaîne complète avant / après patch | **identique** (gps ok · choch ok 0 signal · rsi ok 33 instruments · sr échec OandaAuthError) |
| Calendrier, chemin succès après patch | **20 événements · VALID · score 1.00 · 80 clés metadata · 5 jours** |
| `CalendarEvent.model_validate` (consommateur) | **20 / 20** |
| `CalendarData.bucket(now)` | **fonctionnel** (blackout 0 · proximity 3 CAD CPI · watch 2) |
| `audit_calendar_time_consistency` | **20/20 concordants**, offset −0.000067 h → cohérent |
| `verifier_integrite.py` | **10 / 10** |
| `py_compile` Python 3.12 | **OK** |
| `git status` | `M bluestar_chain.py` + `?? .workbuddy-ai/` — aucun `__pycache__` |

**Validation en conditions réelles :** pendant la campagne de tests, la source a de nouveau répondu 429 et le nouveau message s'est affiché tel quel — `calendrier indisponible : la source a limite le debit (HTTP 429) - cooldown jusqu'a 23:23:47 UTC (environ 10 min). Relancez dans quelques minutes : rien n'est casse.`

### Correction d'une hypothèse de l'audit

Le **cooldown RSI de 30 s n'est pas atteignable depuis la chaîne** : `_ScanState._last_scan_at` est au niveau *instance* et `_charger_module` ré-exécute le module à chaque appel (donc à chaque run de la chaîne, l'état repart à zéro). Le garde ajouté est de la **défense en profondeur** pour le cas `None` / `results` vide — ce n'est pas une cause racine. Les causes racines restent : **jeton OANDA non autorisé**, **429 Fair Economy**, et **adaptateurs qui refusent les exports vides**.

*Sauvegarde de l'original : `.workbuddy-ai/backup/bluestar_chain.py.orig`*
