#!/usr/bin/env python3
"""
Bedriftspriser — norske banker
Scraper DNB + SpareBank 1 SMN + SpareBank 1 Østlandet

Krav:
    pip install playwright beautifulsoup4
    playwright install chromium

Kjør:
    python3 bankpriser.py
    python3 bankpriser.py --debug
"""

import asyncio
import json
import os
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

DEBUG = "--debug" in sys.argv

SOURCES = [
    {"bank": "DNB", "url": "https://www.dnb.no/bedrift/dagligbank/konto/prisliste", "js_required": False},
    {"bank": "DNB", "url": "https://www.dnb.no/bedrift/dagligbank/prisliste", "js_required": False},
    {"bank": "DNB", "url": "https://www.dnb.no/bedrift/dagligbank/kort/prisliste", "js_required": False},
    {"bank": "DNB", "url": "https://www.dnb.no/bedrift/dagligbank/betaling/prisliste", "js_required": False},
    {"bank": "SB1 SMN", "url": "https://www.sparebank1.no/nb/smn/bedrift/kundeservice/bestill/prisliste.html", "js_required": True, "wait_selector": ".accordion"},
    {"bank": "SB1 Østlandet", "url": "https://www.sparebank1.no/nb/ostlandet/bedrift/kundeservice/bestill/prisliste.html", "js_required": True, "wait_selector": ".accordion"},
]

OUTPUT_FILE   = Path("bankpriser.html")
SNAPSHOT_FILE = Path("bankpriser_snapshot.json")
DEBUG_FILE    = Path("debug.txt")

# Minimumsantall rader vi forventer fra hver bank.
# Hvis siden returnerer færre, er noe sannsynligvis galt med URL eller sidestruktur.
MINIMUM_RADER = {"DNB": 30, "SB1 SMN": 50, "SB1 Østlandet": 50}

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def rens(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()

def normaliser_verdi(v):
    """Fjern enhet-tekst: '1,75 kr', '2,-', 'kr 80' -> '1,75', '2', '80'.
    Beholder interne mellomrom slik at '4,65 % per ar' ikke smelter sammen."""
    if not v:
        return v
    v = v.strip()
    v = re.sub(r'(?i)^kr\s*', '', v)           # "kr 80" -> "80"
    v = re.sub(r'(?i)\s+kr(\s.*)?$', '', v)    # "1,75 kr" / "1,75 kr per kjop" -> "1,75"
    v = re.sub(r',-$', '', v)                   # "2,-" -> "2"
    return v.strip()


def ekstraher_prisrader(html):
    soup = BeautifulSoup(html, "html.parser")
    rader = []
    heading_tags = {"h1","h2","h3","h4","h5","h6","dt","button","summary","th"}

    for tabell in soup.find_all("table"):
        if tabell.find("table"):
            continue
        seksjon = ""
        for forgjenger in tabell.find_all_previous():
            if forgjenger.name in heading_tags:
                kandidat = rens(forgjenger.get_text())
                if len(kandidat) > 2 and kandidat.lower() not in {"vis alle","lukk alle",""}:
                    seksjon = kandidat
                    break
        for tr in tabell.find_all("tr"):
            celler = [rens(c.get_text()) for c in tr.find_all(["td","th"])]
            celler = [c for c in celler if c]
            if len(celler) >= 2 and celler[0] != celler[-1]:
                rader.append((seksjon, celler[0], celler[-1]))

    return rader

def fp(rader, seksjon_ord, label_ord=None, maks=25):
    for seksjon, label, verdi in rader:
        if seksjon_ord:
            if not any(o.lower() in seksjon.lower() for o in seksjon_ord):
                continue
        if label_ord:
            if not any(o.lower() in label.lower() for o in label_ord):
                continue
        if verdi and len(verdi) <= maks:
            return verdi
    return None

# ---------------------------------------------------------------------------
# Henting
# ---------------------------------------------------------------------------

async def hent_html(kilde, browser):
    side = await browser.new_page()
    await side.set_extra_http_headers({
        "Accept-Language": "nb-NO,nb;q=0.9",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    })
    print(f"  Henter {kilde['bank']} — {kilde['url']}")
    await side.goto(kilde["url"], wait_until="domcontentloaded", timeout=30_000)
    if kilde.get("js_required"):
        try:
            await side.wait_for_selector(kilde.get("wait_selector","table"), timeout=15_000)
        except Exception:
            pass
        await side.wait_for_timeout(2_500)
    html = await side.content()
    await side.close()
    return html

# ---------------------------------------------------------------------------
# Prisstruktur
# ---------------------------------------------------------------------------

def parse_priser(bank, rader):
    r = rader
    def n(v):
        return normaliser_verdi(v) if v else v
    return {
        "bank": bank,
        "Driftskonto rente": (
            fp(r, ["driftskonto"], ["bedriftskonto", "rente"]) or
            fp(r, ["driftskonto næringsliv"], ["rente"])
        ),
        "Klientkonto rente": fp(r, ["klientkonto"], ["klientkonto", "rente"]),
        "Plasseringskonto (standard)": (
            fp(r, None, ["rente fra første krone"]) or
            fp(r, ["plasseringskonto bedrift"], ["rente kr 750"]) or
            fp(r, ["plasseringskonto"], ["rente"], maks=10)
        ),
        "Plasseringskonto+ (1M+)": (
            fp(r, ["plasseringskonto pluss"], ["rente 1 mill"]) or
            fp(r, ["plasseringskonto+"], ["rente"])
        ),
        "Depositumskonto rente": fp(r, ["depositumskonto", "depositumkonto"], ["rente"]),
        "Fastrente 6 mnd": (
            fp(r, ["binding i 3 måneder"], ["innskudd fra 25.000 til 10"]) or
            fp(r, ["fastrenteinnskudd"], ["6 måneders innskudd", "rente ved 6"])
        ),
        "Fastrente 12 mnd": (
            fp(r, ["binding i 6 måneder"], ["innskudd fra 25 000 til 100"]) or
            fp(r, ["fastrenteinnskudd"], ["12 måneders innskudd", "rente ved 12"])
        ),
        "Utbetaling m/KID (kr)": (
            fp(r, ["elektroniske utbetalinger via bedriftsnettbank",
                   "utbetalinger i norge"], ["med kid"]) or
            fp(r, ["nettbank bedrift"], ["betaling med kid"]) or
            fp(r, ["utbetalinger"], ["betaling med kid"])
        ),
        "Lønnskjøring (kr)": fp(r, ["utbetalinger i norge","nettbank bedrift","utbetalinger"], ["lønnsutbetaling","lønnsoverførs"]),
        "Internasjonal betaling (kr)": (
            fp(r, ["betale til utlandet","utenlands betalingsformidling"], ["øvrige betalinger","øvrige ordinære"]) or
            fp(r, ["betaling til utlandet"], ["norske kroner"])
        ),
        "eFaktura per faktura (kr)": (
            fp(r, ["registrere og motta efaktura", "registrere og motta eFaktura"], ["registrere efaktura, per faktura"]) or
            fp(r, ["efaktura b2c"], ["efaktura betalingskrav"]) or
            fp(r, ["fakturatjenesten"], ["pr faktura utsendelse"])
        ),

        # --- Kontohold og daglige tjenester ---
        # DNB: to separate produkter:
        #   Standalone uten ERP: 0 kr/mnd (tilgangsgebyr gratis, betaler per transaksjon)
        #   Bundle: 69 kr/mnd (inkl. 3 brukere, 30 KID-utbet., 30 lonn, Visa-kortavgift)
        #   ERP-integrasjon: 150 kr/mnd (eget produkt, legges til enten 0 eller 69)
        # SB1 SMN: uten integrasjon 0 kr, med integrasjon 300 kr
        # SB1 Ostlandet: uten integrasjon 120 kr, med integrasjon 300 kr
        "Nettbank uten integrasjon (kr/mnd)": (
            # DNB: section "Etablering av bedriftsnettbank" er headingen parser finner
            # (off-by-one) for abonnementstabellen uten integrasjon
            fp(r, ["etablering av bedriftsnettbank"], ["inkludert første bruker"]) or
            fp(r, ["nettbank bedrift"], ["uten integrasjon", "uten filover"])
        ),

        "Nettbank bundle (kr/mnd)": (
            fp(r, ["bank og regnskap i ett", "banking and accounting in one"], ["banktjenester", "banking services"])
        ),

        "ERP-integrasjon (kr/mnd)": (
            # DNB: section "Abonnementspris bedriftsnettbank Per mån" (off-by-one) + label "Første bruker"
            fp(r, ["abonnementspris bedriftsnettbank per mån", "abonnementspris bedriftsnettbank per man"], ["første bruker"]) or
            fp(r, ["nettbank bedrift"], ["integrasjon mot økonomisystem", "med filoverf"])
        ),

        # Debitkort (Visa) årspris:
        # DNB: seksjon "Visa Business" / "Corporate card with Visa", label "Annual fee" → 295
        # SB1 SMN: seksjon "Grunnpakke", label "Årspris SpareBank 1 Visa bankkort" → 0 kr (inkl i pakke)
        # SB1 Østlandet: seksjon "Visa bedrift", label "Årspris pr kort" → 295,-
        "Debitkort Visa årspris": (
            fp(r, ["bedriftskort med visa"], ["årsavgift"]) or    # DNB: seksjonnavn fra debug
            fp(r, ["bankkort"], ["årspris"]) or                   # SB1 SMN: standalone 200 kr
            fp(r, ["visa bedrift"], ["årspris pr kort"])           # SB1 Østlandet
        ),

        "Kortgebyr per kjøp": (
            fp(r, ["bedriftskort med visa"], ["varekjøp i norge"]) or  # DNB: 2,50 kr
            fp(r, ["bankkort"], ["varekjøp"]) or                        # SB1 SMN: 4 kr
            fp(r, ["visa bedrift"], ["varekjøp i norge"])                # SB1 Østlandet: 2,50
        ),
    }

# ---------------------------------------------------------------------------
# Varsling — sammenlign med forrige snapshot
# ---------------------------------------------------------------------------

def last_snapshot():
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    return {}

def lagre_snapshot(alle_priser):
    data = {p["bank"]: {k: v for k, v in p.items() if k != "bank"} for p in alle_priser}
    SNAPSHOT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def send_slack(melding):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    data = json.dumps({"text": melding}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)

def finn_advarsler(alle_priser, snapshot):
    advarsler = []
    endringer = []
    for p in alle_priser:
        bank = p["bank"]
        forrige = snapshot.get(bank, {})
        for felt, verdi in p.items():
            if felt == "bank":
                continue
            gammel = forrige.get(felt)
            if gammel and not verdi:
                advarsler.append((bank, felt, gammel))
            elif gammel and verdi and normaliser_verdi(gammel) != normaliser_verdi(verdi):
                endringer.append((bank, felt, gammel, verdi))
    return advarsler, endringer

# ---------------------------------------------------------------------------
# Markdown-output
# ---------------------------------------------------------------------------

KATEGORIER = [
    ("Kontohold og daglig bruk", [
        "Nettbank uten integrasjon (kr/mnd)",
        "Nettbank bundle (kr/mnd)",
        "ERP-integrasjon (kr/mnd)",
        "Debitkort Visa årspris",
        "Kortgebyr per kjøp",
    ]),
    ("Betalingstjenester — kr per transaksjon", [
        "Utbetaling m/KID (kr)",
        "Lønnskjøring (kr)",
        "eFaktura per faktura (kr)",
    ]),
    ("Rentebetingelser", [
        "Driftskonto rente",
        "Klientkonto rente",
        "Plasseringskonto (standard)",
        "Plasseringskonto+ (1M+)",
        "Depositumskonto rente",
        "Fastrente 6 mnd",
        "Fastrente 12 mnd",
    ]),
]

FELT_DISPLAY = {
    "ERP-integrasjon (kr/mnd)": "Integrasjon (kr/mnd)",
}

BANK_DISPLAY = {
    "SB1 Østlandet": "SB1 Øst",
}

IKONER = {
    "person": (
        '<svg viewBox="0 0 22 22" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M11 10C13.2091 10 15 8.20914 15 6C15 3.79086 13.2091 2 11 2C8.79086 2 7 3.79086 7 6C7 8.20914 8.79086 10 11 10Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M14.08 13L12.414 14.6228C11.6301 15.3863 10.3781 15.3781 9.60427 14.6043L8 13H7.92C6.88035 13 5.88328 13.413 5.14814 14.1481C4.413 14.8833 4 15.8804 4 16.92V18C4 19.1046 4.89543 20 6 20H16C17.1046 20 18 19.1046 18 18V16.92C18 15.8804 17.587 14.8833 16.8519 14.1481C16.1167 13.413 15.1196 13 14.08 13Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    ),
    "briefcase": (
        '<svg viewBox="0 0 22 22" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="2" y="5.99998" width="18" height="6" rx="2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M3 12H19V17C19 18.1045 18.1046 19 17 19H5C3.89543 19 3 18.1045 3 17V12Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M13.9866 3.29785C13.6939 2.51721 12.9477 2.00004 12.114 2.00004H9.88605C9.05233 2.00004 8.30608 2.51721 8.01337 3.29785L7 6.0004H15L13.9866 3.29785Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M11 10.0003V14.0003" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    ),
    "building": (
        '<svg viewBox="0 0 22 22" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M12.0001 20H3.00012C2.44784 20 2.00012 19.5523 2.00012 19V3C2.00012 2.44772 2.44784 2 3.00012 2H14.0001C14.5524 2 15.0001 2.44772 15.0001 3V8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M18 8H12C11.4477 8 11 8.44772 11 9V19C11 19.5523 11.4477 20 12 20H18C18.5523 20 19 19.5523 19 19V9C19 8.44772 18.5523 8 18 8Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M14 12H16" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>'
        '<path d="M14 16H16" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>'
        '<path d="M5.00012 6H7.00012" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>'
        '<path d="M5.00012 10H7.00012" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>'
        '<path d="M5.00012 14H7.00012" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>'
        '</svg>'
    ),
    "piggybank": (
        '<svg viewBox="0 0 22 22" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M10.6257 4.25097L9.8174 4.83977C10.0677 5.18336 10.5078 5.33116 10.9148 5.20827L10.6257 4.25097ZM5.99996 2.49999L5.75742 1.52985C5.49995 1.59422 5.27863 1.75829 5.1422 1.98593C5.00577 2.21358 4.96543 2.48611 5.03006 2.74352L5.99996 2.49999ZM13.289 3.97064L13.4168 2.97884V2.97884L13.289 3.97064ZM19.8006 9.08591L20.7771 8.87033V8.87033L19.8006 9.08591ZM16.7195 16.9542L16.201 16.0991C15.902 16.2804 15.7195 16.6046 15.7195 16.9542H16.7195ZM13.6406 18H14.6406C14.6406 17.4477 14.1929 17 13.6406 17V18ZM10.6748 18V17C10.1225 17 9.67477 17.4477 9.67477 18H10.6748ZM7.70893 18.9999H6.70893V18.9999H7.70893ZM7.70893 16.9542H8.70893C8.70893 16.5094 8.41511 16.118 7.98796 15.9939L7.70893 16.9542ZM4.01233 13L4.94998 12.6524C4.80462 12.2603 4.43056 12 4.01233 12V13ZM4.32993 9V10C4.72334 10 5.08019 9.76933 5.24173 9.41062L4.32993 9ZM6.84617 5.87023L7.38367 6.71349C7.74672 6.48208 7.92091 6.04428 7.81606 5.62671L6.84617 5.87023ZM10.6257 4.25097L11.434 3.66217C10.3422 2.1634 8.91233 1.62112 7.77935 1.46279C7.21924 1.38452 6.72863 1.39861 6.37589 1.43223C6.19858 1.44913 6.05333 1.47123 5.94819 1.49015C5.89553 1.49962 5.85268 1.50834 5.82062 1.51528C5.80458 1.51875 5.79121 1.52179 5.78065 1.52426C5.77536 1.52549 5.77078 1.52658 5.7669 1.52752C5.76496 1.52799 5.7632 1.52842 5.76162 1.52881C5.76083 1.529 5.76009 1.52919 5.75939 1.52936C5.75904 1.52945 5.75854 1.52957 5.75837 1.52961C5.75789 1.52973 5.75742 1.52985 5.99996 2.49999C6.24249 3.47014 6.24205 3.47025 6.24161 3.47036C6.24148 3.47039 6.24105 3.47049 6.24079 3.47056C6.24026 3.47069 6.23977 3.47081 6.23933 3.47092C6.23843 3.47114 6.2377 3.47132 6.23714 3.47146C6.236 3.47173 6.2355 3.47185 6.23564 3.47181C6.2359 3.47175 6.23867 3.47111 6.24383 3.46999C6.25416 3.46775 6.27394 3.46365 6.30228 3.45855C6.35912 3.44833 6.44929 3.4343 6.56567 3.4232C6.8003 3.40084 7.13019 3.39151 7.50255 3.44355C8.23453 3.54583 9.11756 3.87905 9.8174 4.83977L10.6257 4.25097ZM13.289 3.97064L13.4168 2.97884C12.3207 2.83762 11.2895 3.00591 10.3366 3.29367L10.6257 4.25097L10.9148 5.20827C11.6946 4.97276 12.4303 4.86828 13.1612 4.96245L13.289 3.97064ZM19.8006 9.08591L20.7771 8.87033C19.9799 5.2596 16.5413 3.38142 13.4168 2.97884L13.289 3.97064L13.1612 4.96245C15.8512 5.30904 18.2829 6.85019 18.8241 9.3015L19.8006 9.08591ZM16.7195 16.9542L17.238 17.8093C18.993 16.7451 21.8189 13.5894 20.7771 8.87033L19.8006 9.08591L18.8241 9.3015C19.6288 12.9462 17.4564 15.3378 16.201 16.0991L16.7195 16.9542ZM16.7195 18.9999H17.7195V16.9542H16.7195H15.7195V18.9999H16.7195ZM15.7195 19.9999V20.9999C16.824 20.9999 17.7195 20.1045 17.7195 18.9999H16.7195H15.7195V19.9999ZM14.6406 19.9999V20.9999H15.7195V19.9999V18.9999H14.6406V19.9999ZM13.6406 18.9999H12.6406C12.6406 20.1045 13.536 20.9999 14.6406 20.9999V19.9999V18.9999H14.6406H13.6406ZM13.6406 18H12.6406V18.9999H13.6406H14.6406V18H13.6406ZM10.6748 18V19H13.6406V18V17H10.6748V18ZM10.6748 18.9999H11.6748V18H10.6748H9.67477V18.9999H10.6748ZM9.67477 19.9999V20.9999C10.7793 20.9999 11.6748 20.1045 11.6748 18.9999H10.6748H9.67477H9.67477V19.9999ZM8.70893 19.9999V20.9999H9.67477V19.9999V18.9999H8.70893V19.9999ZM7.70893 18.9999H6.70893C6.70893 20.1045 7.60436 20.9999 8.70893 20.9999V19.9999V18.9999H8.70893H7.70893ZM7.70893 16.9542H6.70893V18.9999H7.70893H8.70893V16.9542H7.70893ZM4.01233 13L3.07467 13.3476C3.43566 14.3214 4.04976 15.3077 4.78372 16.1163C5.50767 16.9139 6.42524 17.6225 7.4299 17.9145L7.70893 16.9542L7.98796 15.9939C7.46963 15.8433 6.85177 15.4189 6.26461 14.7721C5.68746 14.1362 5.21476 13.3667 4.94998 12.6524L4.01233 13ZM4.01233 13V12H3V13V14H4.01233V13ZM3 13V12H2H1C1 13.1046 1.89543 14 3 14V13ZM2 12H3V10H2H1V12H2ZM2 10H3V9V8C1.89543 8 1 8.89543 1 10H2ZM3 9V10H4.32993V9V8H3V9ZM6.84617 5.87023L6.30867 5.02697C5.14985 5.76561 4.0754 7.12986 3.41812 8.58937L4.32993 9L5.24173 9.41062C5.78199 8.21097 6.63158 7.19288 7.38367 6.71349L6.84617 5.87023ZM5.99996 2.49999L5.03006 2.74352L5.87627 6.11376L6.84617 5.87023L7.81606 5.62671L6.96985 2.25647L5.99996 2.49999Z" fill="currentColor"/>'
        '<circle cx="8" cy="9.99998" r="0.5" stroke="currentColor"/>'
        '</svg>'
    ),
}

SCENARIOER = [
    {
        "id": "a",
        "tittel": "Mikro uten integrasjon",
        "icon": "person",
        "beskrivelse": "ENK eller liten AS med minimal aktivitet. Ingen KID-fakturering, ingen integrasjon. Tre utbetalinger per måned, ett bedriftskort med ti varekjøp.",
        "rader": [
            ("Månedspris/nettbank",    ["89",    "0",    "0",    "120"]),
            ("Integrasjon",            ["—",     "—",    "—",    "—"]),
            ("Kort (årsavgift÷12)",    ["inkl.", "25",   "17",   "25"]),
            ("Utbetalinger (3 stk)",   ["inkl.", "5",    "5",    "5"]),
            ("Kortbruk (10 kjøp)",     ["inkl.", "25",   "40",   "25"]),
        ],
        "totaler": ["89", "55", "62", "175"],
    },
    {
        "id": "b",
        "tittel": "Typisk driftskunde",
        "icon": "briefcase",
        "beskrivelse": "Aktiv AS som bruker eksternt regnskapssystem (Tripletex, Fiken e.l.). Seks utbetalinger per måned, ett kort med tjue varekjøp og integrasjon.",
        "rader": [
            ("Månedspris/nettbank",    ["159",   "0",    "0",    "300"]),
            ("Integrasjon",            ["inkl.", "150",  "300",  "inkl. i 300"]),
            ("Kort (årsavgift÷12)",    ["inkl.", "25",   "17",   "25"]),
            ("Utbetalinger (6 stk)",   ["inkl.", "11",   "9",    "11"]),
            ("Kortbruk (20 kjøp)",     ["inkl.", "50",   "80",   "50"]),
        ],
        "totaler": ["159", "236", "406", "386"],
    },
    {
        "id": "c",
        "tittel": "Vekstkunde",
        "icon": "building",
        "beskrivelse": "Mer aktiv bedrift. Bruker KID-fakturering og integrasjon. Tjue utbetalinger og fem KID-innbetalinger per måned, to bedriftskort med femti varekjøp, og én ekstra nettbankbruker.",
        "rader": [
            ("Månedspris/nettbank",    ["359",   "0",    "0",    "300"]),
            ("Integrasjon",            ["inkl.", "150",  "300",  "inkl. i 300"]),
            ("Ekstra bruker",          ["inkl.", "45",   "50",   "40"]),
            ("2 kort (årsavgift÷12)",  ["inkl.", "50",   "33",   "50"]),
            ("1 ekstra kort (Folio)",  ["89",    "—",    "—",    "—"]),
            ("Utbetalinger (20 stk)",  ["inkl.", "0",    "30",   "35"]),
            ("KID-abonnement",         ["—",     "90",   "95",   "95"]),
            ("KID-innbet. (5 stk)",    ["10",    "11",   "10",   "10"]),
            ("Kortbruk (50 kjøp)",     ["inkl.", "125",  "200",  "125"]),
        ],
        "totaler": ["458", "471", "718", "655"],
    },
    {
        "id": "d",
        "tittel": "Inaktiv holding",
        "icon": "piggybank",
        "beskrivelse": "Ingen kortbruk og null transaksjoner. Viser minimumskostnaden for å holde en konto åpen.",
        "rader": [
            ("Månedspris/nettbank",    ["89",    "0",    "0",    "120"]),
        ],
        "totaler": ["89", "0", "0", "120"],
    },
]

FORUTSETNINGER = [
    ("<strong>DNB bundle vs standalone:</strong> Bundle (69 kr/mnd) inkluderer 3 brukere, 30 utbet./mnd med KID, "
     "30 lønnsutbet. og Visa Business-kortavgift. Brukes kun der det er billigere enn standalone. "
     "Integrasjon mot regnskapssystem (150 kr/mnd) er alltid et separat tillegg."),
    ("<strong>SB1 Østlandet nettbank + integrasjon:</strong> 300 kr/mnd er totalpris med filoverføring/integrasjon — "
     "ikke 120 + 300. Du velger enten 120 kr (uten) eller 300 kr (med)."),
    ("<strong>KID-abonnement:</strong> DNB og SB1 tar 90–95 kr/mnd i abonnementsavgift for KID-innbetalingsmottak, "
     "i tillegg til per-transaksjonsprisen. Folio har ingen slik abonnementsavgift."),
    ("<strong>Kortavgift:</strong> DNB og SB1 tar 200–295 kr/år per bedriftskort (÷12 i beregningene). "
     "Folio inkluderer ett kort; ekstra kort koster 89 kr/mnd kun ved bruk."),
    ("<strong>Varekjøp med kort:</strong> DNB 2,50 kr, SB1 SMN 4 kr, SB1 Østlandet 2,50 kr, Folio 0 kr."),
    ("<strong>Ikke inkludert:</strong> Renter på innskudd (se pristabell nedenfor), internasjonale betalinger, "
     "AvtaleGiro, terminaler og andre spesialtjenester."),
]

_HTML_CSS = """
  :root {
    --ground:    #f8f7f4;
    --surface:   #ffffff;
    --ink:       #1c1c1a;
    --ink-mid:   #4a4a46;
    --ink-light: #6e6e68;
    --rule:      #dedad4;
    --rule-dark: #b8b4ac;
    --detail-bg: #fbfaf8;
    --radius:    3px;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--ground);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
    font-size: 13.5px;
    line-height: 1.5;
  }
  .page { max-width: 820px; margin: 0 auto; padding: 3rem 1.5rem 4rem; }
  .masthead { border-top: 2px solid var(--ink); padding-top: .75rem; margin-bottom: 2.5rem; }
  .masthead h1 {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1.75rem; font-weight: 700; letter-spacing: -.02em;
    color: var(--ink); text-wrap: balance; margin-bottom: .2rem;
  }
  .masthead .dateline { font-size: .72rem; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-light); }
  .section-label {
    font-size: .65rem; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
    color: var(--ink-light); margin: 2.5rem 0 .9rem; padding-bottom: .4rem; border-bottom: 1px solid var(--rule);
  }
  .summary-wrap { overflow-x: auto; }
  table.summary {
    width: 100%; border-collapse: collapse; font-size: 0.97rem;
    font-variant-numeric: tabular-nums; table-layout: fixed;
  }
  table.summary col.col-name { width: 40%; }
  table.summary col.col-bank { width: 15%; }
  table.summary thead th {
    text-align: right; font-size: .65rem; font-weight: 700; letter-spacing: .05em;
    text-transform: uppercase; color: var(--ink-light);
    padding: .5rem .6rem .5rem 0; border-bottom: 1px solid var(--rule-dark); white-space: nowrap;
  }
  table.summary thead th:first-child { text-align: left; padding-left: 0; }
  table.summary thead th:not(:first-child) { color: var(--ink); }
  tr.summary-row { cursor: pointer; }
  tr.summary-row td {
    padding: .7rem .6rem .7rem 0; border-bottom: 1px solid var(--rule);
    vertical-align: middle; text-align: right; color: var(--ink-mid); transition: background .1s;
  }
  tr.summary-row td:first-child { text-align: left; color: var(--ink); padding-left: .75rem; }
  tr.summary-row:hover td { background: #f0ede8; }
  tr.summary-row.open td { border-bottom: none; background: var(--surface); }
  .scenario-name {
    font-family: Georgia, "Times New Roman", serif; font-weight: 600;
    color: var(--ink); display: inline-flex; align-items: center; gap: .5rem;
  }
  .scenario-name svg { flex-shrink: 0; width: 16px; height: 16px; position: relative; top: -1px; }
  .toggle-icon {
    display: inline-block; font-size: .935rem; color: var(--ink-mid); margin-left: .4rem;
    transition: transform .2s; vertical-align: middle; line-height: 1; position: relative; top: -2px;
  }
  tr.summary-row.open .toggle-icon { transform: rotate(180deg); }
  tr.detail-row { display: none; }
  tr.detail-row.open { display: table-row; }
  tr.detail-row td {
    padding: .45rem .6rem .45rem 0; text-align: right; color: var(--ink-mid);
    border-bottom: 1px solid var(--rule); background: var(--detail-bg);
    white-space: nowrap; font-size: .8rem;
  }
  tr.detail-row td:first-child { text-align: left; color: var(--ink); white-space: normal; padding-left: .75rem; }
  .dash { color: var(--rule-dark); }
  .prisseksjon {
    background: var(--surface); border: 1px solid var(--rule);
    border-radius: var(--radius); overflow: hidden; margin-bottom: .85rem;
  }
  .prisseksjon-header {
    padding: .5rem .75rem; border-bottom: 1px solid var(--rule);
    font-size: .72rem; font-weight: 700; letter-spacing: .03em;
    color: var(--ink-mid); background: var(--ground);
  }
  .prisseksjon .table-wrap { overflow-x: auto; }
  .prisseksjon table {
    width: 100%; border-collapse: collapse; font-size: .8rem; font-variant-numeric: tabular-nums;
  }
  .prisseksjon thead th {
    background: var(--surface); border-bottom: 1px solid var(--rule); padding: .35rem .75rem;
    text-align: right; font-size: .65rem; font-weight: 700; letter-spacing: .05em;
    text-transform: uppercase; color: var(--ink-mid); white-space: nowrap;
  }
  .prisseksjon thead th:first-child { text-align: left; }
  .prisseksjon td { padding: .37rem .75rem; border-bottom: 1px solid var(--rule); text-align: right; color: var(--ink-mid); }
  .prisseksjon td:first-child { text-align: left; color: var(--ink); }
  .prisseksjon tr:last-child td { border-bottom: none; }
  .forutsetninger { margin-top: .5rem; }
  .forutsetninger ul { list-style: none; display: flex; flex-direction: column; gap: .5rem; }
  .forutsetninger li strong { font-weight: 700; color: var(--ink); }
  .forutsetninger li {
    font-size: .78rem; font-weight: 300; color: var(--ink-light);
    padding-left: 1rem; position: relative; line-height: 1.55;
  }
  .forutsetninger li::before { content: "–"; position: absolute; left: 0; color: var(--ink-light); }
  .to-kolonner { display: grid; grid-template-columns: 1fr 1fr; gap: 2.5rem; align-items: start; }
  @media (max-width: 560px) { .to-kolonner { grid-template-columns: 1fr; } }
  .scenario-beskrivelser dl { display: flex; flex-direction: column; gap: .75rem; }
  .sc-def { display: block; }
  .sc-def dt { font-weight: 600; font-size: .8rem; color: var(--ink); margin-bottom: .1rem; }
  .sc-def dd { font-size: .8rem; font-weight: 300; color: var(--ink-light); line-height: 1.55; margin-left: 0; }
  .footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--rule); display: flex; flex-direction: column; gap: .5rem; }
  .footer .disclaimer { font-size: .72rem; color: var(--ink-light); line-height: 1.55; }
  .footer .kilder { font-size: .7rem; color: var(--ink-light); }
  .footer .kilder ul { list-style: none; display: flex; flex-wrap: wrap; gap: .15rem .75rem; margin-top: .25rem; }
  .footer .kilder a { color: var(--ink-light); text-underline-offset: 2px; }
  .footer .kilder a:hover { color: var(--ink); }
  .advarsel {
    border: 1px solid #f97316; background: #fff7ed;
    border-radius: 3px; padding: .75rem 1rem; margin-bottom: 1.5rem; font-size: .875rem;
  }
"""

_HTML_JS = """
  document.querySelectorAll('tr.summary-row').forEach(function(row) {
    row.addEventListener('click', function() {
      var prefix = row.dataset.target;
      var isOpen = row.classList.contains('open');
      if (isOpen) {
        row.classList.remove('open');
        document.querySelectorAll('tr.detail-row[id^="' + prefix + '-"]').forEach(function(d) {
          d.classList.remove('open');
        });
      } else {
        row.classList.add('open');
        document.querySelectorAll('tr.detail-row[id^="' + prefix + '-"]').forEach(function(d) {
          d.classList.add('open');
        });
      }
    });
  });
"""


def _td_val(v):
    if v == "—":
        return '<td class="dash">—</td>'
    return f"<td>{v}</td>"


def lag_html(alle_priser, advarsler, rad_advarsler):
    banker = [p["bank"] for p in alle_priser]
    alle_banker = ["Folio"] + banker
    alle_banker_display = [BANK_DISPLAY.get(b, b) for b in alle_banker]

    folio_priser = {
        "bank": "Folio",
        "Nettbank uten integrasjon (kr/mnd)": "89–359",
        "Nettbank bundle (kr/mnd)": "—",
        "ERP-integrasjon (kr/mnd)": "inkl.",
        "Debitkort Visa årspris": "inkl.",
        "Kortgebyr per kjøp": "0",
        "Utbetaling m/KID (kr)": "inkl.",
        "Lønnskjøring (kr)": "inkl.",
        "eFaktura per faktura (kr)": "2",
        "Driftskonto rente": "—",
        "Klientkonto rente": "—",
        "Plasseringskonto (standard)": "—",
        "Plasseringskonto+ (1M+)": "—",
        "Depositumskonto rente": "—",
        "Fastrente 6 mnd": "—",
        "Fastrente 12 mnd": "—",
    }
    alle_priser_med_folio = [folio_priser] + alle_priser

    advarsel_html = ""
    if advarsler or rad_advarsler:
        items = ""
        for bank, felt, gammel in advarsler:
            items += f"<li><strong>{bank}</strong> — felt <code>{felt}</code> ikke funnet (forrige verdi: <code>{gammel}</code>)</li>"
        for bank, antall, minimum in rad_advarsler:
            items += f"<li><strong>{bank}</strong> — bare {antall} rader lest (forventet minst {minimum}). URL eller sidestruktur kan ha endret seg.</li>"
        advarsel_html = f'<div class="advarsel"><strong>⚠️ Les-feil — sjekk disse manuelt</strong><ul>{items}</ul></div>'

    # Accordion summary table
    accordion_rader = ""
    for s in SCENARIOER:
        icon = IKONER[s["icon"]]
        totaler_html = "".join(f"<td>{t}</td>" for t in s["totaler"])
        accordion_rader += (
            f'<tr class="summary-row" data-target="detail-{s["id"]}">'
            f'<td><span class="scenario-name">{icon}{s["tittel"]}</span>'
            f'<span class="toggle-icon">▾</span></td>'
            f'{totaler_html}</tr>\n'
        )
        for i, (label, verdier) in enumerate(s["rader"], 1):
            celler = "".join(_td_val(v) for v in verdier)
            accordion_rader += (
                f'<tr class="detail-row" id="detail-{s["id"]}-{i}">'
                f"<td>{label}</td>{celler}</tr>\n"
            )

    bank_header_cols = "".join(f"<col class=\"col-bank\">" for _ in alle_banker)
    bank_header_ths = "".join(f"<th>{b}</th>" for b in alle_banker_display)

    # Om scenariene
    om_scenariene = ""
    for s in SCENARIOER:
        om_scenariene += (
            f'<div class="sc-def">'
            f'<dt>{s["tittel"]}</dt>'
            f'<dd>{s["beskrivelse"]}</dd>'
            f'</div>\n'
        )

    # Forutsetninger
    forutsetninger_html = "".join(f"<li>{f}</li>" for f in FORUTSETNINGER)

    # Listepriser
    pris_seksjoner = ""
    for tittel, felt_liste in KATEGORIER:
        header_ths = "<th>Tjeneste</th>" + "".join(f"<th>{b}</th>" for b in alle_banker_display)
        rader_html = ""
        for felt in felt_liste:
            display = FELT_DISPLAY.get(felt, felt)
            celler = f"<td>{display}</td>"
            for p in alle_priser_med_folio:
                v = normaliser_verdi(p.get(felt)) if p.get(felt) else "—"
                celler += _td_val(v)
            rader_html += f"<tr>{celler}</tr>\n"
        pris_seksjoner += (
            f'<div class="prisseksjon">'
            f'<div class="prisseksjon-header">{tittel}</div>'
            f'<div class="table-wrap"><table>'
            f'<thead><tr>{header_ths}</tr></thead>'
            f'<tbody>{rader_html}</tbody>'
            f'</table></div></div>\n'
        )

    # Kilder
    kilder_items = "".join(
        f'<li><a href="{s["url"]}" target="_blank">{s["bank"]}</a></li>'
        for s in SOURCES
    )

    today = date.today().strftime("%-d. %B %Y").replace(
        "January", "januar").replace("February", "februar").replace(
        "March", "mars").replace("April", "april").replace(
        "May", "mai").replace("June", "juni").replace(
        "July", "juli").replace("August", "august").replace(
        "September", "september").replace("October", "oktober").replace(
        "November", "november").replace("December", "desember")

    return f"""<!DOCTYPE html>
<html lang="nb">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Priser bedriftsbank</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<div class="page">

  <header class="masthead">
    <h1>Priser bedriftsbank</h1>
    <p class="dateline">Oppdatert {today}</p>
  </header>

  {advarsel_html}

  <div class="summary-wrap">
    <table class="summary">
      <colgroup>
        <col class="col-name">
        {bank_header_cols}
      </colgroup>
      <thead>
        <tr>
          <th>Eksempelkunde</th>
          {bank_header_ths}
        </tr>
      </thead>
      <tbody>
        {accordion_rader}
      </tbody>
    </table>
  </div>

  <div class="to-kolonner">
    <div>
      <p class="section-label">Om scenariene</p>
      <div class="scenario-beskrivelser">
        <dl>{om_scenariene}</dl>
      </div>
    </div>
    <div>
      <p class="section-label">Forutsetninger</p>
      <div class="forutsetninger">
        <ul>{forutsetninger_html}</ul>
      </div>
    </div>
  </div>

  <p class="section-label">Listepriser</p>
  {pris_seksjoner}

  <footer class="footer">
    <p class="disclaimer">Kun offentlig tilgjengelige listepriser. Lån og finansiering fastsettes individuelt og er ikke inkludert. Plasseringskonto+ krever typisk minimum 1 million i innskudd.</p>
    <div class="kilder">
      <strong>Kilder</strong>
      <ul>{kilder_items}</ul>
    </div>
  </footer>

</div>
<script>{_HTML_JS}</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Hoved
# ---------------------------------------------------------------------------

async def main():
    print("Starter bankpris-scraper...\n")
    alle_priser = []
    alle_rader  = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        grupper = {}
        for kilde in SOURCES:
            grupper.setdefault(kilde["bank"], []).append(kilde)

        for bank, kilder in grupper.items():
            print(f"\n[{bank}]")
            kombinert_html = ""
            for kilde in kilder:
                kombinert_html += await hent_html(kilde, browser)
            rader = ekstraher_prisrader(kombinert_html)
            alle_rader[bank] = rader

            min_rader = MINIMUM_RADER.get(bank, 10)
            if len(rader) < min_rader:
                print(f"  ⚠️  Bare {len(rader)} rader funnet (forventet minst {min_rader}) — sjekk URL og sidestruktur")

            alle_priser.append(parse_priser(bank, rader))

        await browser.close()

    # Varsling
    snapshot = last_snapshot()
    advarsler, endringer = finn_advarsler(alle_priser, snapshot)

    # Legg til rad-advarsler fra parsing
    rad_advarsler = []
    for p in alle_priser:
        bank = p["bank"]
        antall = len(alle_rader[bank])
        min_rader = MINIMUM_RADER.get(bank, 10)
        if antall < min_rader:
            rad_advarsler.append((bank, antall, min_rader))

    url = "https://ingridesigner.github.io/bankpriser/"

    if endringer:
        print("\n📊  PRISENDRINGER:")
        for bank, felt, gammel, ny in endringer:
            print(f"   {bank} / {felt}: {gammel} → {ny}")

    if advarsler or rad_advarsler or endringer:
        print("\n⚠️  VARSLER:" if advarsler or rad_advarsler else "")
        slack_linjer = []
        for bank, felt, gammel, ny in endringer:
            slack_linjer.append(f"• {bank}: *{felt}* endret fra {gammel} til *{ny}*")
        for bank, felt, gammel in advarsler:
            print(f"   Felt forsvant: {bank} / {felt}  (forrige: {gammel})")
            slack_linjer.append(f"• {bank}: *{felt}* ikke funnet (forrige: {gammel})")
        for bank, antall, minimum in rad_advarsler:
            print(f"   For få rader: {bank} — {antall} funnet, forventet minst {minimum}")
            slack_linjer.append(f"• {bank}: bare {antall} rader lest (forventet minst {minimum})")
        slack_linjer.append(f"<{url}|Se oppdatert prisoversikt>")
        send_slack("🏦 *Endringer oppdaget*\n" + "\n".join(slack_linjer))
    else:
        print("\n✅ Ingen endringer oppdaget.")
        send_slack(f"✅ Ingen endringer denne uken. <{url}|Se prisoversikt>")

    html = lag_html(alle_priser, advarsler, rad_advarsler)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    lagre_snapshot(alle_priser)

    print(f"\n✅ Ferdig! Lagret til {OUTPUT_FILE.resolve()}")
    print("\nFunnet priser:")
    for p in alle_priser:
        print(f"\n  {p['bank']}")
        for k, v in p.items():
            if k != "bank":
                print(f"    {k}: {normaliser_verdi(v) if v else '—'}")

    if DEBUG:
        linjer = []
        for bank, rader in alle_rader.items():
            linjer.append(f"\n{'='*60}\n{bank}\n{'='*60}")
            for seksjon, label, verdi in rader:
                linjer.append(f"  [{seksjon[:40]}]  {label[:40]}  →  {verdi[:30]}")
        DEBUG_FILE.write_text("\n".join(linjer), encoding="utf-8")
        print(f"\n🔍 Debug-rader skrevet til {DEBUG_FILE.resolve()}")

if __name__ == "__main__":
    asyncio.run(main())
