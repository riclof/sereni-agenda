#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convertit le programme mensuel des activités seniors (PDF Ville de Nice)
en JSON consommable par l'app Séréni (EventsRemoteService).

Usage :
    python3 nice_agenda_to_json.py --pdf programme.pdf --year 2026 --out agenda-nice.json
    python3 nice_agenda_to_json.py --pdf "https://www.nice.fr/.../programme.pdf" --year 2026

Dépendances : pypdf  (pip install pypdf)

⚠️ Le PDF étant non structuré, l'extraction est heuristique : VÉRIFIEZ le JSON
   produit avant publication. Les champs id/date/heure/lieu sont déduits du texte.
"""

import argparse, json, re, sys, unicodedata, urllib.request, tempfile, os
from datetime import datetime

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("Installez pypdf :  pip install pypdf")

# ── Mois français → numéro ────────────────────────────────────────────────
MOIS = {
    "JANVIER": 1, "FEVRIER": 2, "MARS": 3, "AVRIL": 4, "MAI": 5, "JUIN": 6,
    "JUILLET": 7, "AOUT": 8, "SEPTEMBRE": 9, "OCTOBRE": 10, "NOVEMBRE": 11, "DECEMBRE": 12,
}
JOURS = "LUNDI|MARDI|MERCREDI|JEUDI|VENDREDI|SAMEDI|DIMANCHE"

# Entête de jour : « LUNDI 1 JUIN »
RE_JOUR = re.compile(rf"^\s*({JOURS})\s+(\d{{1,2}})\s+([A-ZÉÛ]+)", re.IGNORECASE)
# Début d'activité : « > Titre [Pass 55+] »
RE_ACT = re.compile(r"^\s*>\s*(.+)")
# Heure de début : « 10h30 », « 9h », « 14h30-16h30 »
RE_HEURE = re.compile(r"(\d{1,2})\s*h\s*(\d{2})?")

# ── Catégories par mots-clés (ordre = priorité) ───────────────────────────
KEYWORDS = [
    ("sport",   ["danse", "disco", "country", "charleston", "capoeira", "gym",
                 "randonn", "marche", "aquagym", "équilibre", "tai chi", "tai-chi",
                 "pilates", "stretching", "vélo"]),
    ("sante",   ["santé", "prévention", "sophrolog", "relaxation", "yoga",
                 "nutrition", "mémoire", "bien-être", "bien être"]),
    ("atelier", ["atelier", "dessin", "aquarelle", "percussion", "peinture",
                 "informatique", "couture", "cuisine", "jardinage", "initiation",
                 "cours de"]),
    ("social",  ["bal", "rencontre", "réunion", "café", "goûter", "gouter",
                 "repas", "thé dansant", "loto", "jeux", "rencontres"]),
    ("culture", ["visite", "musée", "musee", "cinéma", "cinema", "observatoire",
                 "exposition", "conférence", "conference", "théâtre", "theatre",
                 "spectacle", "concert", "grotte", "film", "excursion", "sortie",
                 "patrimoine", "balade"]),
]

def strip_accents_lower(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()

def infer_category(text):
    t = text.lower()
    for cat, kws in KEYWORDS:
        for kw in kws:
            if kw in t:
                return cat
    return "culture"

def slugify(s, maxlen=24):
    s = strip_accents_lower(s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maxlen].strip("-") or "evt"

def clean(s):
    return re.sub(r"\s+", " ", s).strip()

def parse_pdf(path, year):
    reader = PdfReader(path)
    lines = []
    for page in reader.pages:
        for ln in (page.extract_text() or "").splitlines():
            ln = ln.rstrip()
            if ln.strip():
                lines.append(ln)

    events = []
    cur_day = None      # (day, month_num)
    months_seen = set()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # Entête de jour ?
        mj = RE_JOUR.match(line)
        if mj:
            day = int(mj.group(2))
            mois_txt = strip_accents_lower(mj.group(3)).upper()
            # normalise (AOUT/AOÛT…)
            mnum = None
            for k, v in MOIS.items():
                if strip_accents_lower(k).upper() == mois_txt:
                    mnum = v; break
            if mnum:
                cur_day = (day, mnum)
                months_seen.add(mnum)
            i += 1
            continue

        # Début d'activité ?
        ma = RE_ACT.match(line)
        if ma and cur_day:
            # Titre = texte après '>', sans le tag [..]
            title = clean(re.sub(r"\[.*?\]", "", ma.group(1)))
            # Agrège les lignes suivantes jusqu'à la prochaine activité / jour
            block = [ma.group(1)]
            j = i + 1
            while j < n and not RE_ACT.match(lines[j]) and not RE_JOUR.match(lines[j]):
                block.append(lines[j])
                j += 1
            blob = " ".join(block)

            # Heure : première occurrence "Hh(MM)"
            hh, mm = 9, 0
            mh = RE_HEURE.search(blob)
            if mh:
                hh = int(mh.group(1)); mm = int(mh.group(2) or 0)

            # Lieu : segment après « : » sur la ligne d'horaire, avant « Inscription »
            location = ""
            mloc = re.search(r"\d{1,2}\s*h\s*\d{0,2}[^\:]*:\s*([^\n]+)", blob)
            if mloc:
                loc = mloc.group(1)
                loc = re.split(r"Inscription|Inscriptions|Tarif|Participation|Matériel|Accès", loc)[0]
                location = clean(loc)[:120]

            # Description : 1re phrase utile du bloc (hors titre/heure)
            desc = clean(re.sub(r"\[.*?\]", "", blob))
            desc = re.sub(r"^\s*" + re.escape(ma.group(1)) + r"\s*", "", desc)
            desc = re.split(r"Inscription|Inscriptions", desc)[0]
            desc = clean(desc)[:240]

            day, mnum = cur_day
            try:
                date = datetime(year, mnum, day, hh, mm)
            except ValueError:
                i = j; continue

            events.append({
                "id": f"{slugify(title)}-{date:%Y-%m-%d}",
                "title": title[:90],
                "category": infer_category(title + " " + desc),
                "date": date.strftime("%Y-%m-%dT%H:%M:%S"),
                "location": location,
                "description": desc,
            })
            i = j
            continue

        i += 1

    # Mois dominant pour l'en-tête du flux
    month_num = max(months_seen, key=lambda m: sum(1 for e in events
                    if e["date"][5:7] == f"{m:02d}")) if months_seen else datetime.now().month
    return events, month_num

def main():
    ap = argparse.ArgumentParser(description="PDF agenda seniors Nice → JSON Séréni")
    ap.add_argument("--pdf", required=True, help="Chemin local ou URL du PDF")
    ap.add_argument("--year", type=int, default=datetime.now().year)
    ap.add_argument("--city", default="Nice")
    ap.add_argument("--out", default="agenda-nice.json")
    args = ap.parse_args()

    # Téléchargement si URL
    path = args.pdf
    tmp = None
    if path.startswith("http"):
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        print(f"Téléchargement {path} …")
        urllib.request.urlretrieve(path, tmp.name)
        path = tmp.name

    events, month_num = parse_pdf(path, args.year)
    events.sort(key=lambda e: e["date"])

    feed = {
        "city": args.city,
        "month": f"{args.year}-{month_num:02d}",
        "updatedAt": datetime.now().strftime("%Y-%m-%d"),
        "events": events,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    if tmp: os.unlink(tmp.name)
    print(f"✅ {len(events)} activités → {args.out}  (mois {feed['month']})")
    # Récap par catégorie
    from collections import Counter
    for cat, c in Counter(e["category"] for e in events).most_common():
        print(f"   {cat:8} : {c}")

if __name__ == "__main__":
    main()
