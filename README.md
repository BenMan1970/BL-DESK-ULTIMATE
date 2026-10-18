# BLUESTAR DESK

**Decision Intelligence** — un bouton, toute la chaine, le desk technique.

Cette application enchaine les 7 programmes BLUESTAR d'origine et produit le
desk technique au format HTML. Elle est faite pour tourner sur
**Streamlit Community Cloud**, gratuitement, sans serveur a administrer.

---

## Ce que fait l'application

```
        UN SEUL BOUTON
              |
    +---------+---------+
    |         |         |
  GPS      CHoCH      RSI      S/R          <- 4 scanners EN PARALLELE
    |         |         |        |
    +---------+---------+--------+
              |
        Calendrier economique
              |
           MERGE  ->  merged.json
              |
        BLS V10 Pro  ->  DESK HTML
```

Vous cliquez sur **Lancer le desk**. L'application :

1. lance les 4 scanners **en parallele** (environ 60 s au total) ;
2. ingere le calendrier economique ;
3. fusionne les 4 resultats via le merge d'origine ;
4. fait tourner BLS V10 Pro, qui produit le desk ;
5. affiche le desk et propose son telechargement
   (`Rapport desk technique.html`).

Si une etape echoue, **la chaine s'arrete** : aucun desk partiel n'est presente,
et le message nomme l'etape fautive et la cause reelle.

---

## Verification du bon environnement

Apres execution, chaque artefact declare l'environnement qu'il a interroge.
Vous pouvez le controler dans le detail des etapes ou dans les fichiers
produits :

```
choch.json   -> "environment": "practice"
gps.json     -> "env": "practice"
merged.json  -> "env": "practice"
```

Si vous lisez `live` alors que vous attendiez `practice`, arretez et corrigez
vos secrets avant d'utiliser le desk.

---

## Installation sur Streamlit Community Cloud

### 1. Creer le depot

Poussez ce dossier sur GitHub (public ou prive).

### 2. Deployer

1. Allez sur <https://share.streamlit.io>
2. **New app** -> choisissez votre depot
3. **Main file path** : `app.py`
4. **Deploy**

> **Important** : le dossier `programmes/` doit etre present dans le depot.
> C'est lui qui contient les 7 programmes d'origine.

### 3. Renseigner les secrets

Dans l'application : **Settings -> Secrets**. Collez :

```toml
OANDA_ACCESS_TOKEN = "votre_jeton_oanda_practice"
OANDA_ACCOUNT_ID   = "101-002-00000000-001"
OANDA_ENV = "practice"
```

Cliquez sur **Save**. L'application redemarre et les identifiants sont pris en
compte.

**Ce sont exactement les cles attendues par vos 4 scanners d'origine.** Vous les
saisissez une seule fois, et les quatre s'en servent :

| Programme | Secrets utilises |
|---|---|
| Scanner MTF GPS | `OANDA_ACCESS_TOKEN`, `OANDA_ACCOUNT_ID` |
| Scanner CHoCH | `OANDA_ACCESS_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENV` |
| Scanner RSI | `OANDA_ACCESS_TOKEN` (+ `OANDA_ENVIRONMENT`, facultatif) |
| Scanner S/R | `OANDA_ACCESS_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENV` |
| Merge | aucun |
| Calendrier | aucun |
| BLS V10 Pro | aucun |

Le **Calendrier** n'a besoin d'aucun secret : il interroge ses propres sources
publiques gratuites (calendrier Forex Factory). Le **Merge** et **BLS V10 Pro**
travaillent sur des fichiers, jamais sur le reseau.

> **Les identifiants ne sont jamais dans le depot.** Le fichier
> `.streamlit/secrets.toml` est dans `.gitignore`, et le modele fourni
> (`.streamlit/secrets.toml.example`) ne contient que des valeurs d'exemple.

**Laissez `OANDA_ENV = "practice"`** tant que vous n'etes pas certain.
Cette valeur pilote l'URL de l'API OANDA : `live` interroge le compte reel.

---

## Lancer en local (facultatif)

```bash
python -m venv venv
source venv/bin/activate          # Windows : venv\Scripts\activate
pip install -r requirements.txt

# identifiants en local, deux possibilites :
#  a) copier .streamlit/secrets.toml.example en .streamlit/secrets.toml
#  b) exporter les variables d'environnement
streamlit run app.py
```

---

## Structure du depot

```
app.py                     l'application Streamlit (interface + un bouton)
bluestar_chain.py          l'enchainement des 7 programmes
requirements.txt           les dependances
.streamlit/
  secrets.toml.example     modele de secrets (a ne pas remplir dans le depot)
programmes/                LES 7 PROGRAMMES D'ORIGINE, INTACTS
  Scanner  MTF GPS/
  Scanner CHoCH/
  Scanner RSI/
  Scanner Supports & Resistances/
  Merge/
  Calendrier/
  BLS V10 Pro/
```

---

## Les 7 programmes sont intacts

`bluestar_chain.py` et `app.py` sont les **seuls** fichiers ecrits pour ce
depot. Les 7 programmes d'origine sont repris **tels quels** : aucune ligne
modifiee, aucun renommage, aucune correction.

Integrite verifiee par empreinte SHA-256 :

| Programme | Fichier |
|---|---|
| Scanner MTF GPS | `app.py` |
| Scanner RSI | `app.py` |
| Scanner CHoCH | `app.py` |
| Scanner S/R | `app.py` + `bluestar_core.py` |
| Merge | `merge_app.py` |
| BLS V10 Pro | `app.py` + `ENGINE.V10.py` |
| Calendrier | `app.py` + `pipeline_calendar.py` |

Les empreintes attendues sont listees dans `INTEGRITE.md`. Pour verifier :

```bash
python verifier_integrite.py
```

---

## Points a connaitre

### Les 4 scanners ne lisent pas tous le meme nom de variable

| Programme | Nom lu |
|---|---|
| GPS | `OANDA_ENV` |
| CHoCH | `OANDA_ENV` (environnement d'abord, secrets ensuite) |
| RSI | `OANDA_ENVIRONMENT`, avec `practice` par defaut |
| S/R | `OANDA_ENV`, puis `OANDA_ENVIRONMENT` |

**Cela ne vous demande rien de particulier** : vous ne fournissez qu'une seule
valeur, `OANDA_ENV = "practice"`, et les quatre scanners s'y conforment. RSI
retombe de lui-meme sur `practice` quand `OANDA_ENVIRONMENT` est absent.

`bluestar_chain.py` pose simplement `OANDA_ENV` et `OANDA_ENVIRONMENT` dans
l'environnement a partir de votre valeur, pour que GPS et CHoCH — qui lisent
l'environnement avant les secrets — ne puissent pas partir sur le compte reel
a cause d'une variable restee dans le shell.

### Le calendrier

Le calendrier est lance comme un **script separe** (`pipeline_calendar.py`),
exactement comme dans la version d'origine, avec les arguments `--once` et
`--data-dir`.

Il applique un **plancher anti-limite de debit** : si vous relancez la chaine
trop vite, il ressert le dernier artefact connu. L'application l'indique alors
clairement (`servi depuis le cache`). Au-dela de 48 h, la chaine **refuse** de
produire le desk plutot que d'afficher des donnees perimees.

### Le week-end

Les scanners signalent `price_is_stale` quand le marche est ferme : le dernier
prix connu est celui de vendredi. C'est normal. Relancez le desk en semaine
pour des prix frais.

### F7 CAL est un facteur temporel

Le facteur calendaire du desk (`F7 CAL`) est recalcule a partir du delai
restant jusqu'aux evenements. Deux executions a une heure d'intervalle donnent
donc des valeurs legerement differentes — **avec exactement les memes donnees
d'entree**. Ce n'est pas une instabilite : c'est la nature du calcul.

---

## Licence et diffusion

Le desk produit est un document confidentiel : il contient des niveaux, des
biais et des zones issus de vos scanners. Ne partagez pas l'URL de l'application
publiquement.

Sur Streamlit Community Cloud, une application publique est accessible a qui
possede l'URL. Pour restreindre l'acces, passez le depot en **prive** et
utilisez les fonctions de partage de la plateforme.
