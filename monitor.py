#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Moto Monitor — tracciamento quotidiano di annunci di moto d'epoca e ricambi.

Il programma interroga i portali configurati per ogni "ricerca" definita in
config.yaml, riconosce gli annunci nuovi rispetto all'esecuzione precedente
(state.json) e produce un riepilogo (report.html / report.md) che puo' essere
inviato via e-mail o Telegram.

Oltre ai nuovi annunci segnala anche i ribassi di prezzo e gli annunci rimossi,
e tiene traccia della salute dei portali (un portale che non risponde viene
riportato nel riepilogo e non fa "sparire" gli annunci gia' noti).

Uso:
    python monitor.py                 esecuzione completa (fetch + report + invio)
    python monitor.py --dry-run       nessun invio, salva solo i report
    python monitor.py --no-state      ignora lo stato (utile per il primo test)
    python monitor.py --config X.yaml usa un file di configurazione diverso
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import random
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus

import requests
import yaml
from bs4 import BeautifulSoup

# Motore HTTP: se disponibile usa curl_cffi, che si presenta come un vero
# Chrome anche a livello TLS. Molti portali (Subito, eBay, Mobile.de) bloccano
# con 403 l'impronta della libreria "requests" pura, anche da IP domestico.
try:
    from curl_cffi import requests as chrome_requests  # pip install curl-cffi
except ImportError:
    chrome_requests = None

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
SEARCHES_PATH = ROOT / "searches.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
PORTAL_LABELS = {
    "subito": "Subito",
    "ebay": "eBay",
    "ebay_de": "eBay.de",
    "mobile_de": "Mobile.de",
    "kleinanzeigen": "Kleinanzeigen",
}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

log = logging.getLogger("moto")


def _setup_logging() -> None:
    """Logging leggibile su stdout. Idempotente: sicuro da richiamare piu' volte."""
    # Console Windows redirette = cp1252: mai piu' crash per un carattere
    # non rappresentabile (viene sostituito con '?').
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname).1s %(message)s",
            datefmt="%H:%M:%S",
        )


# Tentativi extra su errori di rete / 429 / 5xx (sovrascrivibile con http_retries in config.yaml)
HTTP_RETRIES = 2

# ---- Salute dei portali nell'esecuzione corrente ----
# portale -> {"ok": richieste utilizzabili, "fail": fallimenti, "detail": primo errore}
# Serve a distinguere "oggi non c'e' nessun annuncio" da "il portale non ha
# risposto" (blocco IP, pagina di verifica): nel secondo caso il report lo
# segnala e gli annunci gia' noti di quel portale NON vengono contati assenti.
FETCH_STATS: dict[str, dict] = {}
_CURRENT_PORTAL = "?"


def _reset_fetch_stats() -> None:
    FETCH_STATS.clear()


def _stats_bucket() -> dict:
    return FETCH_STATS.setdefault(_CURRENT_PORTAL, {"ok": 0, "fail": 0, "detail": ""})


def _note_fetch_ok() -> None:
    _stats_bucket()["ok"] += 1


def _note_fetch_fail(detail: str) -> None:
    s = _stats_bucket()
    s["fail"] += 1
    if not s["detail"]:
        s["detail"] = detail


def _note_parse_fail(detail: str) -> None:
    """La richiesta HTTP era ok ma la pagina non conteneva i dati attesi
    (tipico delle pagine di verifica anti-bot): riclassifica come fallimento."""
    s = _stats_bucket()
    s["ok"] = max(0, s["ok"] - 1)
    s["fail"] += 1
    if not s["detail"]:
        s["detail"] = detail


def collect_portal_issues() -> list[dict]:
    """Riassume i problemi per portale dell'ultimo giro (usato da report e web app)."""
    issues = []
    for pid, s in FETCH_STATS.items():
        if s["fail"] <= 0 or pid == "?":
            continue
        issues.append(
            {
                "portal": pid,
                "label": PORTAL_LABELS.get(pid, pid),
                "level": "down" if s["ok"] == 0 else "partial",
                "detail": s["detail"],
                "ok": s["ok"],
                "fail": s["fail"],
            }
        )
    return sorted(issues, key=lambda i: (i["level"] != "down", i["label"]))


_BLOCK_MARKERS = (
    "captcha",
    "are you a robot",
    "access denied",
    "unusual traffic",
    "überprüfung",
    "verifica di sicurezza",
)


def _looks_blocked(page_text: str) -> bool:
    """Euristica: pagina 200 ma con contenuti tipici delle verifiche anti-bot."""
    low = page_text[:20000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


# Mappa prefisso degli id annuncio -> id portale (per capire da quale portale
# proviene una voce dello stato, es. "ebayde:123..." -> "ebay_de").
_ID_PREFIX_TO_PORTAL = {
    "subito": "subito",
    "ebay": "ebay",
    "ebayde": "ebay_de",
    "mobilede": "mobile_de",
    "kleinanzeigen": "kleinanzeigen",
}


def _portal_id_of(item_id: str) -> str:
    return _ID_PREFIX_TO_PORTAL.get(str(item_id).split(":", 1)[0], "")


# --------------------------------------------------------------------------- #
# Ingegnerizzazione delle ricerche
# --------------------------------------------------------------------------- #
def expand_queries(keywords: list[str], synonyms: list[str] | None = None) -> list[str]:
    """Genera varianti di una ricerca a partire dalle parole chiave.

    Per ogni parola chiave crea permutazioni ragionevoli (inversione
    marca/modello/cilindrata) e, se richiesto, la combina con termini di
    contesto tipici degli annunci d'epoca. L'espansione e' volutamente
    contenuta per non generare rumore.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", q).strip().lower()
        if q and q not in seen:
            seen.add(q)
            out.append(q)

    for kw in keywords:
        add(kw)
        tokens = kw.split()
        # inversione di due token adiacenti (es. "350 s2" <-> "s2 350")
        if len(tokens) >= 2:
            for i in range(len(tokens) - 1):
                swap = tokens[:i] + [tokens[i + 1], tokens[i]] + tokens[i + 2:]
                add(" ".join(swap))
        # variante senza "cc"
        add(kw.replace(" cc", "").replace("cc", ""))

    # I sinonimi ampliano solo la prima parola chiave (la piu' rappresentativa)
    if synonyms and keywords:
        base = keywords[0]
        for s in synonyms:
            add(f"{base} {s}")

    return out


def is_relevant(title: str, keywords: list[str]) -> bool:
    """Filtro di pertinenza: il titolo deve contenere i token distintivi di
    almeno una parola chiave. Evita di raccogliere annunci correlati o banner.
    """
    t = title.lower()
    for kw in keywords:
        tokens = [tok for tok in re.split(r"\s+", kw.lower()) if len(tok) >= 2]
        if tokens and all(tok in t for tok in tokens):
            return True
    return False


# --------------------------------------------------------------------------- #
# Adapter dei portali
# --------------------------------------------------------------------------- #
def _http_get(url: str, params: dict | None = None, retries: int | None = None) -> requests.Response | None:
    """GET con tentativi ripetuti e pausa crescente su errori di rete, 429 e 5xx.
    Su 403/404 non insiste (ritentare non aiuta e infastidisce il portale).
    Registra l'esito per portale in FETCH_STATS."""
    headers = {
        "User-Agent": UA,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8,de;q=0.7",
        "Accept": "text/html,application/json,*/*",
    }
    attempts = (HTTP_RETRIES if retries is None else retries) + 1
    last_err = "errore sconosciuto"
    for attempt in range(1, attempts + 1):
        r = None
        try:
            if chrome_requests is not None:
                r = chrome_requests.get(
                    url, params=params, headers=headers, timeout=25, impersonate="chrome"
                )
            else:
                r = requests.get(url, params=params, headers=headers, timeout=25)
        except Exception as exc:  # noqa: BLE001 — curl_cffi ha eccezioni proprie
            last_err = f"errore di rete ({exc.__class__.__name__})"
            log.warning("    [http] %s su %s (tentativo %d/%d)", exc.__class__.__name__, url, attempt, attempts)
        if r is not None:
            if r.status_code == 200:
                _note_fetch_ok()
                return r
            last_err = f"HTTP {r.status_code}"
            log.warning("    [http] %s -> HTTP %s (tentativo %d/%d)", url, r.status_code, attempt, attempts)
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        if attempt < attempts:
            time.sleep(2 * attempt + random.uniform(0, 1.5))
    _note_fetch_fail(last_err)
    return None


def _walk_json(node, found: list[dict]) -> None:
    """Percorre ricorsivamente una struttura JSON e raccoglie i dizionari che
    somigliano a un annuncio (hanno un titolo e un identificativo). Robusto ai
    cambi di percorso interni al JSON del sito.
    """
    if isinstance(node, dict):
        has_title = any(k in node for k in ("subject", "title"))
        has_id = any(k in node for k in ("urn", "list_id", "item_id", "id"))
        if has_title and has_id:
            found.append(node)
        for v in node.values():
            _walk_json(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, found)


def _dig_price(node: dict) -> str:
    """Estrae un prezzo leggibile da un annuncio Subito, cercando in piu' punti."""
    if isinstance(node.get("price"), (str, int, float)):
        return str(node["price"])
    # Subito espone il prezzo tra le "features"
    for feat in node.get("features", []) or []:
        try:
            label = (feat.get("label") or "").lower()
            uri = (feat.get("uri") or "").lower()
            if "prezzo" in label or "price" in uri:
                vals = feat.get("values") or []
                if vals:
                    return str(vals[0].get("value") or vals[0].get("key") or "")
        except AttributeError:
            continue
    # fallback: qualsiasi valore che contenga il simbolo dell'euro
    for v in node.values():
        if isinstance(v, str) and "€" in v:
            return v.strip()
    return "n.d."


def _dig_location(node: dict) -> str:
    geo = node.get("geo") or {}
    for key in ("town", "city", "region"):
        val = geo.get(key)
        if isinstance(val, dict) and val.get("value"):
            return val["value"]
        if isinstance(val, str) and val:
            return val
    return ""


def _dig_url(node: dict) -> str:
    urls = node.get("urls")
    if isinstance(urls, dict):
        for key in ("default", "mobile", "desktop"):
            if urls.get(key):
                return urls[key]
    if isinstance(node.get("url"), str):
        return node["url"]
    return ""


IMG_RE = re.compile(r"https?://[^\s\"'\\]+\.(?:jpe?g|png|webp)", re.IGNORECASE)
# CDN immagini di Subito: URL spesso con query (?rule=...) o senza estensione.
# Va riconosciuto per intero, query compresa, altrimenti la foto resta vuota.
IMG_HOST_RE = re.compile(
    r"https?://[^\s\"'\\]*(?:images\.sbito\.it|sbt-ads-images|images\.subito\.it)[^\s\"'\\]*",
    re.IGNORECASE,
)


def _find_image_url(node) -> str | None:
    """Cerca ricorsivamente il primo URL di immagine in un sottoalbero JSON,
    dando priorita' alle chiavi che tipicamente contengono le foto.
    """
    if isinstance(node, str):
        m = IMG_HOST_RE.search(node)
        if m:
            return m.group(0)
        m = IMG_RE.search(node)
        return m.group(0) if m else None
    if isinstance(node, dict):
        for key in ("scale_variants", "images", "image", "uri", "url", "src", "secondary_uri"):
            if key in node:
                found = _find_image_url(node[key])
                if found:
                    return found
        for value in node.values():
            found = _find_image_url(value)
            if found:
                return found
    if isinstance(node, list):
        for value in node:
            found = _find_image_url(value)
            if found:
                return found
    return None


def _dig_image(node: dict) -> str:
    """Estrae la foto principale di un annuncio. Prima cerca nei rami 'immagini'
    (per non prendere l'avatar del venditore); se non trova nulla, come ripiego
    cerca in tutto il nodo un URL del CDN annunci di Subito, che e' comunque
    una foto dell'annuncio e non del profilo.
    """
    for key in ("images", "image", "thumbnail", "pictures", "gallery"):
        if key in node:
            found = _find_image_url(node[key])
            if found:
                return found
    for value in node.values():
        if isinstance(value, (dict, list, str)):
            found = _find_image_url(value)
            if found and IMG_HOST_RE.search(found):
                return found
    return ""


def _subito_items_from_nodes(raw: list[dict]) -> list[dict]:
    """Converte i nodi-annuncio del JSON di Subito nel formato interno.
    Percorso unico per entrambe le fonti (endpoint JSON e __NEXT_DATA__)."""
    results: list[dict] = []
    seen_ids: set[str] = set()
    for ad in raw:
        title = str(ad.get("subject") or ad.get("title") or "").strip()
        ident = str(
            ad.get("urn") or ad.get("list_id") or ad.get("item_id") or ad.get("id") or ""
        )
        link = _dig_url(ad)
        if not title or not link or ident in seen_ids:
            continue
        seen_ids.add(ident)
        results.append(
            {
                "id": f"subito:{ident}",
                "portal": "Subito",
                "title": title,
                "price": _dig_price(ad),
                "location": _dig_location(ad),
                "url": link,
                "image": _dig_image(ad),
            }
        )
    return results


# Endpoint JSON pubblico usato dal sito stesso di Subito: piu' leggero e stabile
# del parsing HTML. Se non risponde o cambia forma, si ripiega in automatico
# sulla pagina dei risultati. Id di categoria noti; categoria assente = HTML.
SUBITO_API_URL = "https://hades.subito.it/v1/search/items"
SUBITO_CATEGORY_IDS = {"moto-e-scooter": "36", "accessori-moto": "42"}
_SUBITO_API_STATE = {"fails": 0}  # circuit breaker: dopo 2 errori stop per il giro


def _fetch_subito_api(query: str, category: str) -> list[dict] | None:
    """Interroga l'endpoint JSON di Subito. Ritorna None quando il percorso API
    non e' utilizzabile (categoria ignota, errori, risposta anomala): il
    chiamante ripiega sull'HTML. Uno "zero risultati" dell'API viene comunque
    ricontrollato sull'HTML per prudenza (es. id categoria cambiato)."""
    cat = SUBITO_CATEGORY_IDS.get(category)
    if cat is None or _SUBITO_API_STATE["fails"] >= 2:
        return None
    params = {"q": query, "c": cat, "lim": "30", "sort": "datedesc"}
    headers = {"User-Agent": UA, "Accept": "application/json"}
    try:
        if chrome_requests is not None:
            r = chrome_requests.get(
                SUBITO_API_URL, params=params, headers=headers, timeout=25, impersonate="chrome"
            )
        else:
            r = requests.get(SUBITO_API_URL, params=params, headers=headers, timeout=25)
    except Exception as exc:  # noqa: BLE001 — curl_cffi ha eccezioni proprie
        log.info("    [subito-api] non raggiungibile (%s): uso la pagina HTML", exc.__class__.__name__)
        _SUBITO_API_STATE["fails"] += 1
        return None
    if r.status_code != 200:
        log.info("    [subito-api] HTTP %s: uso la pagina HTML", r.status_code)
        _SUBITO_API_STATE["fails"] += 1
        return None
    try:
        ads = r.json().get("ads")
    except Exception:  # noqa: BLE001
        _SUBITO_API_STATE["fails"] += 1
        return None
    if not isinstance(ads, list):
        _SUBITO_API_STATE["fails"] += 1
        return None
    _SUBITO_API_STATE["fails"] = 0
    raw: list[dict] = []
    _walk_json(ads, raw)
    return raw


def fetch_subito(query: str, category: str = "moto-e-scooter", region: str = "") -> list[dict]:
    """Adapter Subito.it. Prova prima l'endpoint JSON del sito (niente HTML da
    interpretare, meno esposto alle pagine di verifica); se non e' utilizzabile
    estrae il blob __NEXT_DATA__ dalla pagina pubblica dei risultati.
    """
    if not region:
        raw = _fetch_subito_api(query, category)
        if raw:
            results = _subito_items_from_nodes(raw)
            if results:
                _note_fetch_ok()
                return results

    area = region if region else "italia"
    url = f"https://www.subito.it/annunci-{area}/vendita/{category}/"
    r = _http_get(url, params={"q": query})
    if r is None:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        log.warning("    [subito] __NEXT_DATA__ non trovato (pagina di verifica o struttura cambiata)")
        _note_parse_fail("__NEXT_DATA__ assente (possibile pagina di verifica)")
        return []

    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        log.warning("    [subito] JSON non valido")
        _note_parse_fail("JSON __NEXT_DATA__ non valido")
        return []

    raw = []
    _walk_json(data, raw)
    return _subito_items_from_nodes(raw)


# ---- eBay via API ufficiale (Browse API) --------------------------------- #
# Se sono configurate le chiavi developer (gratuite: developer.ebay.com),
# eBay.it ed eBay.de vengono interrogati tramite l'API ufficiale: niente
# scraping, niente blocchi 403. Le chiavi si passano come variabili d'ambiente
# EBAY_CLIENT_ID / EBAY_CLIENT_SECRET (o nei campi di config.yaml).
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_CLIENT_ID = ""
EBAY_CLIENT_SECRET = ""
_EBAY_TOKEN = {"value": "", "expires": 0.0}


def _ebay_api_enabled() -> bool:
    return bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)


def _ebay_api_token() -> str | None:
    """Token applicativo OAuth (client credentials), con cache fino a scadenza."""
    if _EBAY_TOKEN["value"] and time.time() < _EBAY_TOKEN["expires"] - 60:
        return _EBAY_TOKEN["value"]
    try:
        r = requests.post(
            EBAY_TOKEN_URL,
            auth=(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET),
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        log.warning("[ebay-api] errore di rete sul token: %s", exc.__class__.__name__)
        return None
    if r.status_code != 200:
        log.warning("[ebay-api] token rifiutato: HTTP %s %s", r.status_code, r.text[:200])
        return None
    data = r.json()
    _EBAY_TOKEN["value"] = data.get("access_token", "")
    _EBAY_TOKEN["expires"] = time.time() + float(data.get("expires_in", 7200))
    return _EBAY_TOKEN["value"] or None


def _fmt_api_price(value, currency) -> str:
    """Prezzo API in formato europeo leggibile dai parser ("1200,50 EUR")."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n.d."
    s = str(int(round(v))) if abs(v - round(v)) < 0.005 else f"{v:.2f}".replace(".", ",")
    return f"{s} {currency or ''}".strip()


def fetch_ebay_api(query: str, marketplace: str, portal_label: str, id_prefix: str) -> list[dict]:
    token = _ebay_api_token()
    if token is None:
        _note_fetch_fail("token API eBay non ottenuto")
        return []
    try:
        r = requests.get(
            EBAY_BROWSE_URL,
            params={"q": query, "limit": "50", "sort": "newlyListed"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": marketplace,
                "Accept-Language": "de-DE" if marketplace == "EBAY_DE" else "it-IT",
            },
            timeout=25,
        )
    except requests.RequestException as exc:
        _note_fetch_fail(f"errore di rete API eBay ({exc.__class__.__name__})")
        return []
    if r.status_code != 200:
        _note_fetch_fail(f"API eBay HTTP {r.status_code}")
        log.warning("    [ebay-api] HTTP %s: %s", r.status_code, r.text[:200])
        return []
    _note_fetch_ok()
    results: list[dict] = []
    for it in r.json().get("itemSummaries") or []:
        item_id = str(it.get("itemId") or "").strip()
        title = (it.get("title") or "").strip()
        url = it.get("itemWebUrl") or ""
        if not item_id or not title or not url:
            continue
        price = it.get("price") or {}
        image = (it.get("image") or {}).get("imageUrl", "")
        if not image:
            thumbs = it.get("thumbnailImages") or []
            if thumbs:
                image = thumbs[0].get("imageUrl", "")
        loc = it.get("itemLocation") or {}
        location = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
        # itemId Browse: "v1|1234567890|0" -> stesso id numerico dello scraping,
        # cosi' lo storico esistente non si duplica.
        m = re.search(r"\|(\d+)\|", item_id)
        ident = m.group(1) if m else item_id
        results.append(
            {
                "id": f"{id_prefix}:{ident}",
                "portal": portal_label,
                "title": title[:160],
                "price": _fmt_api_price(price.get("value"), price.get("currency")),
                "location": location,
                "url": url.split("?")[0],
                "image": image,
            }
        )
    return results


def fetch_ebay(query: str, domain: str = "www.ebay.it", portal_label: str = "eBay", id_prefix: str = "ebay") -> list[dict]:
    """Adapter eBay. Con le chiavi developer configurate usa l'API ufficiale
    (Browse API): niente blocchi. Senza chiavi, best-effort sull'HTML della
    pagina risultati. Riutilizzabile su piu' domini eBay (es. eBay.de).
    """
    if _ebay_api_enabled():
        marketplace = "EBAY_DE" if domain.endswith(".de") else "EBAY_IT"
        return fetch_ebay_api(query, marketplace, portal_label, id_prefix)
    url = f"https://{domain}/sch/i.html"
    r = _http_get(url, params={"_nkw": query, "_sop": "10"})  # 10 = time: newly listed
    if r is None:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results: list[dict] = []
    for li in soup.select("li.s-item, li.s-card"):
        a = li.select_one("a.s-item__link, a[href*='/itm/']")
        title_el = li.select_one(".s-item__title, .s-card__title")
        price_el = li.select_one(".s-item__price, .s-card__price")
        if not a or not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        # Rimuove le diciture di servizio che eBay annega nel titolo
        for junk in (
            "viene aperta una nuova finestra o scheda",
            "wird in neuem Fenster oder Tab geöffnet",
            "opens in a new window or tab",
        ):
            title = title.replace(junk, "")
        title = re.sub(r"^(nuova inserzione|neues angebot|new listing)\s*", "", title, flags=re.IGNORECASE).strip()
        if not title or title.lower() == "shop on ebay":
            continue
        link = a.get("href", "").split("?")[0]
        m = re.search(r"/itm/(\d+)", link)
        ident = m.group(1) if m else link
        img_el = li.select_one("img")
        image = ""
        if img_el:
            image = img_el.get("src") or img_el.get("data-src") or ""
            if image and not image.lower().startswith("http"):
                image = ""
        results.append(
            {
                "id": f"{id_prefix}:{ident}",
                "portal": portal_label,
                "title": title,
                "price": price_el.get_text(strip=True) if price_el else "n.d.",
                "location": "",
                "url": link,
                "image": image,
            }
        )
    if not results and _looks_blocked(r.text):
        _note_parse_fail("possibile pagina di verifica anti-bot")
    return results


def fetch_ebay_de(query: str) -> list[dict]:
    """eBay Germania: ottimo per i ricambi Kawasaki S2 (mercato tedesco)."""
    return fetch_ebay(query, domain="www.ebay.de", portal_label="eBay.de", id_prefix="ebayde")


# --------------------------------------------------------------------------- #
# Helper JSON-LD (schema.org) — usato dai portali che espongono dati strutturati
# --------------------------------------------------------------------------- #
def _jsonld_nodes(soup: BeautifulSoup) -> list[dict]:
    nodes: list[dict] = []
    for sc in soup.find_all("script", type="application/ld+json"):
        raw = sc.string or sc.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            types = node.get("@type", "")
            types = types if isinstance(types, list) else [types]
            if "ItemList" in types:
                for el in node.get("itemListElement", []) or []:
                    item = el.get("item") if isinstance(el, dict) else None
                    if isinstance(item, dict):
                        nodes.append(item)
            elif any(t in ("Product", "Vehicle", "Car", "Motorcycle", "Offer") for t in types):
                nodes.append(node)
    return nodes


def _jsonld_to_item(node: dict, portal_label: str, id_prefix: str) -> dict | None:
    name = (node.get("name") or node.get("headline") or "").strip()
    url = node.get("url") or ""
    if isinstance(url, dict):
        url = url.get("@id") or url.get("url") or ""
    image = node.get("image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or image.get("@id") or ""
    image = image if isinstance(image, str) else ""
    offers = node.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = ""
    if isinstance(offers, dict) and offers.get("price"):
        price = f"{offers.get('price')} {offers.get('priceCurrency', '')}".strip()
    if not name or not isinstance(url, str) or not url:
        return None
    m = re.search(r"(\d{5,})", url)
    ident = m.group(1) if m else re.sub(r"[^a-z0-9]+", "-", name.lower())[:40]
    return {
        "id": f"{id_prefix}:{ident}",
        "portal": portal_label,
        "title": name[:160],
        "price": price or "n.d.",
        "location": "",
        "url": url,
        "image": image,
    }


def fetch_mobilede(query: str) -> list[dict]:
    """Adapter Mobile.de (Germania) — best-effort. Prima prova a leggere i dati
    strutturati JSON-LD, poi ripiega sui link agli annunci.

    ATTENZIONE: Mobile.de ha una protezione anti-bot elevata. Da un IP di
    datacenter viene quasi sempre bloccato; da rete domestica puo' funzionare.
    Per un uso intenso servirebbe l'API ufficiale (con credenziali) o un browser
    reale. I parametri di ricerca potrebbero richiedere un ritocco.
    """
    url = "https://suchen.mobile.de/fahrzeuge/search.html"
    params = {
        "q": query,
        "vehicleCategory": "Motorbike",
        "isSearchRequest": "true",
        "sortOption.sortBy": "creationTime",
    }
    r = _http_get(url, params=params)
    if r is None:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results: list[dict] = []
    seen: set[str] = set()

    for node in _jsonld_nodes(soup):
        it = _jsonld_to_item(node, "Mobile.de", "mobilede")
        if it and it["id"] not in seen:
            seen.add(it["id"])
            results.append(it)
    if results:
        return results

    # Fallback: link ai dettagli annuncio
    for a in soup.select("a[href*='/fahrzeuge/details.html']"):
        href = a.get("href", "")
        m = re.search(r"id=(\d+)", href)
        ident = m.group(1) if m else href
        if not ident or ident in seen:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        seen.add(ident)
        link = href if href.startswith("http") else "https://suchen.mobile.de" + href
        results.append(
            {
                "id": f"mobilede:{ident}",
                "portal": "Mobile.de",
                "title": title[:160],
                "price": "n.d.",
                "location": "",
                "url": link,
                "image": "",
            }
        )
    if not results and _looks_blocked(r.text):
        _note_parse_fail("possibile pagina di verifica anti-bot")
    return results


def fetch_kleinanzeigen(query: str) -> list[dict]:
    """Adapter Kleinanzeigen.de (ex eBay Kleinanzeigen, Germania) — best-effort.
    Ricerca full-text su tutte le categorie: utile sia per le moto sia per i
    ricambi. Legge la lista annunci dall'HTML.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "moto"
    url = f"https://www.kleinanzeigen.de/s-{slug}/k0"
    r = _http_get(url)
    if r is None:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    for art in soup.select("article.aditem, li.aditem, [data-adid]"):
        adid = art.get("data-adid") or ""
        a = art.select_one("a.ellipsis, h2 a, .text-module-begin a, a[href*='/s-anzeige/']")
        href = (a.get("href", "") if a else art.get("data-href", "")) or ""
        if not href:
            continue
        title = (a.get_text(" ", strip=True) if a else art.get_text(" ", strip=True))
        link = href if href.startswith("http") else "https://www.kleinanzeigen.de" + href
        m = re.search(r"/(\d{6,})-", link) or re.search(r"(\d{6,})", adid or link)
        ident = adid or (m.group(1) if m else link)
        if not title or ident in seen:
            continue
        price_el = art.select_one(
            ".aditem-main--middle--price-shipping--price, .aditem-details .price, .price-shipping--price"
        )
        price = price_el.get_text(" ", strip=True) if price_el else "n.d."
        loc_el = art.select_one(".aditem-main--top--left, .aditem-addon")
        location = loc_el.get_text(" ", strip=True) if loc_el else ""
        img_el = art.select_one(".imagebox img, .aditem-image img, img")
        image = ""
        if img_el:
            image = img_el.get("src") or img_el.get("data-imgsrc") or img_el.get("data-src") or ""
            if image and not image.lower().startswith("http"):
                image = ""
        seen.add(ident)
        results.append(
            {
                "id": f"kleinanzeigen:{ident}",
                "portal": "Kleinanzeigen",
                "title": title[:160],
                "price": price,
                "location": location,
                "url": link,
                "image": image,
            }
        )
    if not results and _looks_blocked(r.text):
        _note_parse_fail("possibile pagina di verifica anti-bot")
    return results


ADAPTERS = {
    "subito": fetch_subito,
    "ebay": fetch_ebay,
    "ebay_de": fetch_ebay_de,
    "mobile_de": fetch_mobilede,
    "kleinanzeigen": fetch_kleinanzeigen,
}


# --------------------------------------------------------------------------- #
# Stato
# --------------------------------------------------------------------------- #
def _atomic_write(path: Path, text: str) -> None:
    """Scrittura atomica: prima su file temporaneo, poi sostituzione.
    Evita file troncati se il processo viene interrotto a meta' scrittura."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_state(use_state: bool) -> dict:
    if not use_state or not STATE_PATH.exists():
        return {"seen": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Non buttare via lo storico in silenzio: metti da parte il file rotto.
        backup = STATE_PATH.with_suffix(".json.bad")
        try:
            STATE_PATH.replace(backup)
            log.warning("[state] state.json corrotto: copiato in %s, riparto da zero", backup.name)
        except OSError:
            log.warning("[state] state.json corrotto (backup non riuscito), riparto da zero")
        return {"seen": {}}
    # Migrazione dal formato antico (lista di id) a quello a dizionario
    seen = state.get("seen", {})
    now = dt.datetime.now().isoformat(timespec="seconds")
    for name, val in list(seen.items()):
        if isinstance(val, list):
            seen[name] = {i: now for i in val}
    state["seen"] = seen
    return state


def save_state(state: dict) -> None:
    _atomic_write(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Ricerche (modello modificabile, condiviso con il server web)
# --------------------------------------------------------------------------- #
def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "ricerca"


def default_searches_from_config(config: dict) -> dict:
    """Converte i 'watches' di config.yaml nel nuovo modello a stringhe
    etichettate. Usato solo per creare searches.json la prima volta.
    """
    searches = []
    enabled_portals = [p for p, on in config.get("portals", {}).items() if on] or ["subito"]
    for w in config.get("watches", []):
        texts = expand_queries(w.get("keywords", []), w.get("synonyms", []))
        queries = [{"text": t, "label": "auto", "enabled": True} for t in texts]
        searches.append(
            {
                "id": _slug(w["name"]),
                "name": w["name"],
                "portals": list(enabled_portals),
                "price_max": w.get("price_max"),
                "subito_category": w.get("subito_category", "moto-e-scooter"),
                "relevance_filter": w.get("relevance_filter", True),
                "queries": queries,
            }
        )
    return {"searches": searches}


def load_searches(config: dict) -> list[dict]:
    if SEARCHES_PATH.exists():
        try:
            data = json.loads(SEARCHES_PATH.read_text(encoding="utf-8"))
            return data.get("searches", [])
        except json.JSONDecodeError:
            log.warning("[searches] searches.json non valido, uso config.yaml")
    # primo avvio: genera searches.json dal config e salvalo
    data = default_searches_from_config(config)
    save_searches(data)
    return data["searches"]


def save_searches(data: dict) -> None:
    _atomic_write(SEARCHES_PATH, json.dumps(data, ensure_ascii=False, indent=2))


def load_blacklist() -> dict:
    """Carica la blacklist (annunci da non riproporre mai piu'). Ritorna insiemi
    di URL e id per il confronto rapido. Il file viene alimentato dalla web app
    tramite il server locale, o si puo' editare a mano."""
    if not BLACKLIST_PATH.exists():
        return {"urls": set(), "ids": set()}
    try:
        data = json.loads(BLACKLIST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("[blacklist] blacklist.json non valido, ignorato")
        return {"urls": set(), "ids": set()}
    urls: set[str] = set()
    ids: set[str] = set()
    for entry in data.get("items", []):
        if isinstance(entry, str):
            urls.add(entry)
        elif isinstance(entry, dict):
            if entry.get("url"):
                urls.add(entry["url"])
            if entry.get("id"):
                ids.add(entry["id"])
    return {"urls": urls, "ids": ids}


def save_blacklist(data: dict) -> None:
    _atomic_write(BLACKLIST_PATH, json.dumps(data, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Raccolta
# --------------------------------------------------------------------------- #
def run_searches(config: dict, state: dict, searches: list[dict]) -> list[dict]:
    global _CURRENT_PORTAL
    _reset_fetch_stats()
    global_portals = config.get("portals", {})
    delay = float(config.get("request_delay_seconds", 2))
    keep_days = max(1.0, float(config.get("keep_days", 30)))
    drop_min_pct = float(config.get("price_drop_min_pct", 1.0))
    blacklist = load_blacklist()
    seen = state.setdefault("seen", {})
    blocks: list[dict] = []
    now_iso = dt.datetime.now().isoformat(timespec="seconds")

    for search in searches:
        name = search["name"]
        skey = search.get("id") or _slug(name)
        # Migrazione: lo stato era indicizzato per nome ricerca, ora per id stabile
        # (cosi' rinominare una ricerca non fa ripartire tutto da "nuovo").
        if skey not in seen and name in seen:
            seen[skey] = seen.pop(name)
        # Residuo della versione precedente (stato indicizzato per nome accanto
        # a quello per id, creato dai giri col codice vecchio): se il bucket per
        # nome e' vuoto va semplicemente eliminato.
        if name != skey and not seen.get(name):
            seen.pop(name, None)
        known = seen.setdefault(skey, {})
        # Archivio dei rimossi: niente viene mai cancellato davvero. Se un
        # annuncio "rimosso" ricompare (es. era solo finito in seconda pagina),
        # viene ripescato da qui con storico prezzi e primo avvistamento intatti.
        archive = state.setdefault("archive", {}).setdefault(skey, {})
        # Migrazione voci: formato "id -> data" -> dizionario ricco per voce.
        for iid, val in list(known.items()):
            if isinstance(val, str):
                known[iid] = {"first_seen": val, "last_seen": val, "misses": 0}
            elif isinstance(val, dict):
                val.setdefault("first_seen", now_iso)
                val.setdefault("misses", 0)
            else:
                del known[iid]
        # Blacklist: voci gia' note che l'utente ha eliminato spariscono dallo
        # stato in silenzio (niente sezione "rimossi" per queste).
        if blacklist["urls"] or blacklist["ids"]:
            for bucket in (known, archive):
                for iid, entry in list(bucket.items()):
                    if iid in blacklist["ids"] or (isinstance(entry, dict) and entry.get("url") in blacklist["urls"]):
                        del bucket[iid]

        # portali: intersezione tra quelli scelti per la ricerca e quelli attivi globalmente
        chosen = search.get("portals") or [p for p, on in global_portals.items() if on]
        portals = [p for p in chosen if global_portals.get(p, False) and p in ADAPTERS]
        queries = [q for q in search.get("queries", []) if q.get("enabled", True)]
        query_texts = [q["text"] for q in queries]
        log.info("== %s ==  portali=%s  stringhe attive=%d", name, portals, len(queries))

        collected: dict[str, dict] = {}
        for portal in portals:
            adapter = ADAPTERS[portal]
            plabel = PORTAL_LABELS.get(portal, portal)
            _CURRENT_PORTAL = portal
            for q in queries:
                # Fail-fast: se oggi il portale ha solo rifiutato (3+ errori e
                # zero risposte utili), inutile insistere: si salta al prossimo.
                pstats = FETCH_STATS.get(portal, {})
                if pstats.get("ok", 0) == 0 and pstats.get("fail", 0) >= 3:
                    log.info("  - [%s] senza risposta oggi: salto le stringhe rimanenti", plabel)
                    break
                text = q["text"]
                tag = q.get("label") or text
                log.info("  - [%s] '%s'  (etichetta: %s)", plabel, text, tag)
                try:
                    if portal == "subito":
                        items = adapter(
                            text,
                            category=search.get("subito_category", "moto-e-scooter"),
                            region=search.get("subito_region", ""),
                        )
                    elif portal == "ebay":
                        items = adapter(text, domain=config.get("ebay_domain", "www.ebay.it"))
                    else:
                        items = adapter(text)
                except Exception as exc:  # noqa: BLE001 — un adapter rotto non deve fermare il giro
                    log.error("    [%s] adapter in errore: %s", plabel, exc)
                    _note_fetch_fail(f"errore interno adapter ({exc.__class__.__name__})")
                    items = []

                for it in items:
                    if it["url"] in blacklist["urls"] or it["id"] in blacklist["ids"]:
                        continue  # eliminato dall'utente: mai piu' riproposto
                    if search.get("relevance_filter", True) and not is_relevant(it["title"], query_texts):
                        continue
                    # Filtro prezzo per ricerca: min esclude i ricambi/gadget da
                    # pochi euro quando si cerca una moto intera, max i fuori budget.
                    # Gli annunci senza prezzo leggibile non vengono mai esclusi.
                    pmax = search.get("price_max")
                    pmin = search.get("price_min")
                    if pmax or pmin:
                        pnum = _numeric_price(it["price"])
                        if pnum is not None and ((pmax and pnum > pmax) or (pmin and pnum < pmin)):
                            continue
                    it.setdefault("via_label", tag)
                    it.setdefault("via_portal", plabel)
                    it.setdefault("via", f"{tag} · {plabel}")
                    collected.setdefault(it["id"], it)
                time.sleep(delay)
        _CURRENT_PORTAL = "?"

        current = list(collected.values())
        new_items: list[dict] = []
        price_drops: list[dict] = []
        for it in current:
            entry = known.get(it["id"])
            if entry is None and it["id"] in archive:
                # Ricomparso dopo essere stato dato per rimosso: recupera lo
                # storico dall'archivio e segnalalo di nuovo tra i nuovi.
                entry = archive.pop(it["id"])
                entry.pop("archived_at", None)
                entry["misses"] = 0
                known[it["id"]] = entry
                it["is_new"] = True
                new_items.append(it)
            else:
                it["is_new"] = entry is None
                if entry is None:
                    entry = {"first_seen": now_iso, "misses": 0}
                    known[it["id"]] = entry
                    new_items.append(it)
            entry["last_seen"] = now_iso
            entry["misses"] = 0
            # Prezzi: confronto con l'ultimo prezzo noto e storico compatto.
            new_num = _numeric_price(it["price"])
            old_num = entry.get("price_num")
            if new_num is not None:
                if old_num is not None and old_num > 0 and new_num < old_num * (1 - drop_min_pct / 100.0):
                    pct = round((old_num - new_num) / old_num * 100)
                    it["price_drop"] = {"old": entry.get("price", ""), "old_num": old_num, "pct": pct}
                    price_drops.append(it)
                if old_num is None or abs(new_num - old_num) > 0.01:
                    hist = entry.setdefault("price_history", [])
                    hist.append({"date": now_iso, "price": it["price"]})
                    del hist[:-10]  # tieni solo le ultime 10 variazioni
                entry["price_num"] = new_num
            entry["price"] = it["price"]
            entry["title"] = it["title"]
            entry["url"] = it["url"]
            entry["portal"] = it["portal"]
            entry["location"] = it.get("location", "")
            entry["image"] = it.get("image", "")
            entry["via"] = it.get("via", "")
            it["first_seen"] = entry["first_seen"]

        # Modello ADDITIVO: gli annunci gia' noti restano in elenco anche se
        # oggi non sono ricomparsi (i portali mostrano solo la prima pagina di
        # risultati: l'assenza non significa venduto). Escono solo dopo
        # keep_days giorni senza essere piu' stati rivisti, oppure eliminati a
        # mano dalla pagina (blacklist).
        do_expire = bool(queries and portals)
        cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
        removed: list[dict] = []
        carried: list[dict] = []

        def _verifiable_today(pid: str) -> bool:
            """Vero solo se oggi il portale e' stato interrogato per questa
            ricerca e ha risposto almeno una volta: solo allora l'assenza di un
            annuncio e' un segnale. Un portale bloccato (403), irraggiungibile o
            disattivato non puo' "smentire" nulla: i suoi annunci non maturano
            anzianita' verso la scadenza e vengono contrassegnati 'stale'."""
            if pid not in portals:
                return False
            return (FETCH_STATS.get(pid) or {}).get("ok", 0) > 0

        for iid, entry in list(known.items()):
            if iid in collected:
                continue
            pid = _portal_id_of(iid)
            verifiable = _verifiable_today(pid) if pid else False
            last = entry.get("last_seen") or entry.get("first_seen") or now_iso
            too_old = False
            if do_expire and verifiable:
                try:
                    too_old = dt.datetime.fromisoformat(last) < cutoff
                except ValueError:
                    too_old = False
            if too_old:
                removed.append(
                    {
                        "id": iid,
                        "title": entry.get("title", iid),
                        "price": entry.get("price", ""),
                        "url": entry.get("url", ""),
                        "portal": entry.get("portal", PORTAL_LABELS.get(pid, pid)),
                        "first_seen": entry.get("first_seen", ""),
                    }
                )
                entry["archived_at"] = now_iso
                archive[iid] = entry
                del known[iid]
                continue
            carried.append(
                {
                    "id": iid,
                    "portal": entry.get("portal", PORTAL_LABELS.get(pid, pid)),
                    "title": entry.get("title", iid),
                    "price": entry.get("price", "n.d."),
                    "location": entry.get("location", ""),
                    "url": entry.get("url", ""),
                    "image": entry.get("image", ""),
                    "via": entry.get("via", ""),
                    "first_seen": entry.get("first_seen", ""),
                    "last_seen": entry.get("last_seen", ""),
                    "is_new": False,
                    "carried": True,
                    "stale": not verifiable,
                }
            )
        current = current + carried

        # L'archivio non cresce all'infinito: tieni le ultime 400 voci per ricerca
        if len(archive) > 400:
            for old_iid in sorted(archive, key=lambda k: archive[k].get("archived_at", ""))[: len(archive) - 400]:
                del archive[old_iid]

        log.info(
            "  => %d in elenco (%d visti oggi, %d riportati), %d nuovi, %d ribassi, %d usciti",
            len(current),
            len(collected),
            len(carried),
            len(new_items),
            len(price_drops),
            len(removed),
        )
        blocks.append(
            {
                "name": name,
                "new": sorted(new_items, key=lambda x: x["title"].lower()),
                "current": sorted(current, key=lambda x: x["title"].lower()),
                "price_drops": sorted(price_drops, key=lambda x: x["title"].lower()),
                "removed": sorted(removed, key=lambda x: x["title"].lower()),
            }
        )
    return blocks


def _numeric_price(price: str) -> float | None:
    m = re.search(r"(\d[\d.\s]*\d|\d)", str(price).replace(".", "").replace(",", "."))
    if not m:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", m.group(0)))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def build_reports(blocks: list[dict], portal_issues: list[dict] | None = None, max_items: int = 60) -> tuple[str, str, dict]:
    today = dt.date.today().strftime("%d/%m/%Y")
    issues = portal_issues or []
    max_items = max(1, int(max_items))
    totals = {
        "new": sum(len(b["new"]) for b in blocks),
        "drops": sum(len(b.get("price_drops", [])) for b in blocks),
        "removed": sum(len(b.get("removed", [])) for b in blocks),
    }

    def drop_text(it: dict) -> str:
        d = it.get("price_drop") or {}
        return f"{d.get('old') or 'n.d.'} → {it['price']} (−{d.get('pct', '?')}%)"

    # ---- Markdown ----
    md = [f"# Riepilogo annunci moto — {today}", ""]
    if any(totals.values()):
        md.append(f"**Nuovi: {totals['new']} · Ribassi: {totals['drops']} · Rimossi: {totals['removed']}**")
    else:
        md.append("*Nessuna novità oggi.*")
    md.append("")
    for i in issues:
        stato = "nessuna risposta" if i["level"] == "down" else "risposte parziali"
        md.append(f"> ⚠ {i['label']}: {stato} ({i['detail']}) — l'elenco potrebbe essere incompleto.")
    if issues:
        md.append("")
    for b in blocks:
        md.append(f"## {b['name']}")
        if b["new"]:
            md.append(f"### Nuovi ({len(b['new'])})")
            for it in b["new"]:
                md.append(f"- **{it['title']}** — {it['price']} · {it['location']} · {it['portal']}\n  {it['url']}")
        if b.get("price_drops"):
            md.append(f"### Prezzi calati ({len(b['price_drops'])})")
            for it in b["price_drops"]:
                md.append(f"- **{it['title']}** — {drop_text(it)} · {it['portal']}\n  {it['url']}")
        if b.get("removed"):
            md.append(f"### Usciti dall'elenco ({len(b['removed'])})")
            for it in b["removed"]:
                md.append(f"- {it['title']} — era {it['price'] or 'n.d.'} · {it['portal']}")
        md.append(f"### Tutti gli annunci in elenco ({len(b['current'])})")
        if not b["current"]:
            md.append("_Nessun risultato._")
        for it in b["current"][:max_items]:
            flag = ("🆕 " if it.get("is_new") else "") + ("📉 " if it.get("price_drop") else "")
            md.append(f"- {flag}{it['title']} — {it['price']} · {it['location']} · {it['portal']}\n  {it['url']}")
        if len(b["current"]) > max_items:
            md.append(f"_…e altri {len(b['current']) - max_items} annunci nella web app._")
        md.append("")
    md_text = "\n".join(md)

    # ---- HTML ----
    def esc(s: str) -> str:
        return html.escape(str(s))

    def item_row(it: dict) -> str:
        seen_date = ""
        try:
            seen_date = dt.datetime.fromisoformat(it.get("first_seen", "")).strftime("%d/%m")
        except (ValueError, TypeError):
            pass
        badges = []
        if it.get("is_new"):
            badges.append(
                "<span style=\"background:#d2681e;color:#fff;font-size:11px;font-weight:bold;"
                "padding:2px 6px;border-radius:3px;margin-right:6px\">NUOVO</span>"
            )
        if it.get("price_drop"):
            badges.append(
                "<span style=\"background:#2c6e6a;color:#fff;font-size:11px;font-weight:bold;"
                "padding:2px 6px;border-radius:3px;margin-right:6px\">PREZZO ↓</span>"
            )
        badge = "".join(badges)
        if it.get("image"):
            img = (
                f"<img src=\"{esc(it['image'])}\" width=\"120\" alt=\"\" "
                "style=\"width:120px;height:90px;object-fit:cover;border-radius:6px;border:1px solid #ddd\">"
            )
        else:
            img = (
                "<div style=\"width:120px;height:90px;border:1px solid #ddd;border-radius:6px;"
                "background:#f4f1ea;color:#b7ad97;font-size:11px;text-align:center;line-height:90px\">"
                "senza foto</div>"
            )
        if it.get("price_drop"):
            d = it["price_drop"]
            price_html = (
                f"<s style=\"color:#999\">{esc(d.get('old') or 'n.d.')}</s> "
                f"<b style=\"color:#2c6e6a\">{esc(it['price'])}</b> (−{esc(d.get('pct', '?'))}%)"
            )
        else:
            price_html = esc(it["price"])
        meta = " · ".join(
            filter(
                None,
                [
                    price_html,
                    esc(it["location"]),
                    esc(it["portal"]),
                    f"dal {seen_date}" if seen_date else "",
                    "<i>non verificato oggi</i>" if it.get("stale") else "",
                ],
            )
        )
        via = f"<div style=\"color:#8a7f68;font-size:11px;margin-top:2px\">trovato con: {esc(it.get('via',''))}</div>" if it.get("via") else ""
        return (
            "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
            "style=\"margin:8px 0;border-collapse:collapse\"><tr>"
            f"<td valign=\"top\" style=\"padding-right:12px\">{img}</td>"
            f"<td valign=\"top\" style=\"font-size:14px\">{badge}"
            f"<a href=\"{esc(it['url'])}\" style=\"color:#15302f;font-weight:bold;text-decoration:none\">{esc(it['title'])}</a>"
            f"<div style=\"color:#555;font-size:13px;margin-top:3px\">{meta}</div>{via}</td>"
            "</tr></table>"
        )

    def removed_row(it: dict) -> str:
        title = esc(it.get("title", ""))
        if it.get("url"):
            title = f"<a href=\"{esc(it['url'])}\" style=\"color:#666\">{title}</a>"
        return (
            f"<p style=\"margin:4px 0;font-size:13px;color:#666\">{title}"
            f" — era {esc(it.get('price') or 'n.d.')} · {esc(it.get('portal', ''))}</p>"
        )

    parts = [
        "<div style=\"font-family:Arial,Helvetica,sans-serif;max-width:680px;margin:auto;color:#222\">",
        f"<h2 style=\"margin-bottom:4px\">Riepilogo annunci moto — {today}</h2>",
    ]
    if any(totals.values()):
        parts.append(
            "<p style=\"font-weight:bold\">"
            f"<span style=\"color:#0a7d29\">Nuovi: {totals['new']}</span>"
            f" · <span style=\"color:#2c6e6a\">Ribassi: {totals['drops']}</span>"
            f" · <span style=\"color:#888\">Rimossi: {totals['removed']}</span></p>"
        )
    else:
        parts.append("<p style=\"color:#777\">Nessuna novità oggi.</p>")
    if issues:
        rows = "<br>".join(
            f"<b>{esc(i['label'])}</b>: "
            + ("nessuna risposta" if i["level"] == "down" else "risposte parziali")
            + f" ({esc(i['detail'])})"
            for i in issues
        )
        parts.append(
            "<div style=\"background:#fdf0e7;border:1px solid #d2681e;color:#8a4a1a;"
            "padding:10px 12px;border-radius:8px;font-size:13px\">"
            f"⚠ Portali con problemi oggi — l'elenco potrebbe essere incompleto.<br>{rows}</div>"
        )

    for b in blocks:
        parts.append(f"<h3 style=\"border-bottom:2px solid #eee;padding-bottom:4px;margin-top:22px\">{esc(b['name'])}</h3>")
        if b["new"]:
            parts.append(f"<p style=\"font-weight:bold;color:#0a7d29;margin-bottom:2px\">Nuovi ({len(b['new'])})</p>")
            for it in b["new"]:
                parts.append(item_row(it))
        if b.get("price_drops"):
            parts.append(f"<p style=\"font-weight:bold;color:#2c6e6a;margin-bottom:2px;margin-top:14px\">Prezzi calati ({len(b['price_drops'])})</p>")
            for it in b["price_drops"]:
                parts.append(item_row(it))
        if b.get("removed"):
            parts.append(f"<p style=\"font-weight:bold;color:#888;margin-bottom:2px;margin-top:14px\">Usciti dall'elenco ({len(b['removed'])})</p>")
            for it in b["removed"]:
                parts.append(removed_row(it))
        parts.append(f"<p style=\"color:#555;margin-bottom:2px;margin-top:14px\">Tutti gli annunci in elenco ({len(b['current'])})</p>")
        if not b["current"]:
            parts.append("<p style=\"color:#999\">Nessun risultato.</p>")
        for it in b["current"][:max_items]:
            parts.append(item_row(it))
        if len(b["current"]) > max_items:
            parts.append(
                f"<p style=\"color:#999;font-size:13px\">…e altri {len(b['current']) - max_items} annunci nella web app.</p>"
            )
    parts.append("<hr><p style=\"font-size:12px;color:#999\">Generato automaticamente da Moto Monitor.</p></div>")
    html_text = "\n".join(parts)

    (ROOT / "report.md").write_text(md_text, encoding="utf-8")
    (ROOT / "report.html").write_text(html_text, encoding="utf-8")
    return md_text, html_text, totals


def write_webapp(blocks: list[dict], config: dict, portal_issues: list[dict] | None = None) -> None:
    """Rigenera la web app statica (docs/index.html) iniettando i dati aggregati,
    foto comprese, nel template. Nessun backend: un unico file da aprire o da
    pubblicare (es. GitHub Pages).
    """
    template_path = ROOT / "webapp" / "template.html"
    if not template_path.exists():
        log.warning("[webapp] template.html non trovato, generazione saltata.")
        return

    def clean(it: dict) -> dict:
        drop = it.get("price_drop") or {}
        return {
            "title": it.get("title", ""),
            "price": it.get("price", ""),
            "location": it.get("location", ""),
            "url": it.get("url", ""),
            "portal": it.get("portal", ""),
            "image": it.get("image", ""),
            "first_seen": it.get("first_seen", ""),
            "is_new": bool(it.get("is_new")),
            "via": it.get("via", ""),
            "price_drop_old": drop.get("old", ""),
            "price_drop_pct": drop.get("pct"),
            "stale": bool(it.get("stale")),
            "carried": bool(it.get("carried")),
            "last_seen": it.get("last_seen", ""),
        }

    data = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "title": config.get("webapp_title", "Officina — Monitor annunci"),
        "subtitle": config.get("webapp_subtitle", "Annunci aggregati di moto d'epoca e ricambi"),
        "total_current": sum(len(b["current"]) for b in blocks),
        "total_new": sum(len(b["new"]) for b in blocks),
        "total_drops": sum(len(b.get("price_drops", [])) for b in blocks),
        "portal_issues": [
            {"label": i["label"], "level": i["level"], "detail": i["detail"]}
            for i in (portal_issues or [])
        ],
        "watches": [
            {
                "name": b["name"],
                "current": len(b["current"]),
                "new": len(b["new"]),
                "items": [clean(it) for it in b["current"]],
            }
            for b in blocks
        ],
    }

    out_dir = ROOT / "docs"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "index.html"

    # Guardia anti-svuotamento: se il giro di oggi non ha prodotto alcun
    # annuncio MA la pagina precedente ne aveva e almeno un portale ha avuto
    # problemi, quasi certamente e' stato un giro "cieco" (blocchi anti-bot,
    # rete assente, stato azzerato). Meglio lasciare online la pagina di ieri
    # che pubblicarne una vuota.
    if data["total_current"] == 0 and data["portal_issues"] and out_path.exists():
        try:
            m = re.search(r'"total_current":\s*(\d+)', out_path.read_text(encoding="utf-8"))
            if m and int(m.group(1)) > 0:
                log.warning(
                    "[webapp] 0 annunci oggi e portali in errore: mantengo la pagina precedente (%s annunci).",
                    m.group(1),
                )
                return
        except (OSError, ValueError):
            pass

    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html_out = template_path.read_text(encoding="utf-8").replace("__MOTO_DATA__", payload)
    _atomic_write(out_path, html_out)
    log.info("[webapp] docs/index.html aggiornato (%d annunci)", data["total_current"])


# --------------------------------------------------------------------------- #
# Notifiche
# --------------------------------------------------------------------------- #
def send_email(subject: str, html_body: str, cfg: dict) -> None:
    host = os.environ.get("SMTP_HOST", cfg.get("smtp_host", ""))
    user = os.environ.get("SMTP_USER", cfg.get("smtp_user", ""))
    pwd = os.environ.get("SMTP_PASS", "")
    to = os.environ.get("MAIL_TO", cfg.get("mail_to", ""))
    port = int(os.environ.get("SMTP_PORT", cfg.get("smtp_port", 587)))
    if not (host and user and pwd and to):
        log.warning("[email] configurazione incompleta, invio saltato.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, pwd)
            server.sendmail(user, [t.strip() for t in to.split(",")], msg.as_string())
        log.info("[email] inviata a %s", to)
    except Exception as exc:  # noqa: BLE001
        log.error("[email] errore invio: %s", exc)


def send_telegram(text: str, cfg: dict) -> None:
    token = os.environ.get("TELEGRAM_TOKEN", cfg.get("telegram_token", ""))
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", cfg.get("telegram_chat_id", ""))
    if not (token and chat_id):
        return
    # Telegram limita i messaggi a 4096 caratteri
    for chunk in (text[i:i + 3900] for i in range(0, len(text), 3900)):
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                timeout=20,
            )
        except requests.RequestException as exc:
            log.error("[telegram] errore invio: %s", exc)
            return
    log.info("[telegram] inviato a chat %s", chat_id)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
# Esecuzione (condivisa tra CLI e server)
# --------------------------------------------------------------------------- #
def load_config(config_path: str | Path) -> dict:
    cfg_path = Path(config_path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Configurazione non trovata: {cfg_path}")
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def execute(config: dict, notify: bool = True, use_state: bool = True) -> dict:
    """Un giro completo: carica le ricerche, interroga i portali, aggiorna stato,
    rigenera report/e-mail/web app e (se richiesto) invia le notifiche.
    Restituisce un riepilogo usato anche dal server web.
    """
    _setup_logging()
    global HTTP_RETRIES, EBAY_CLIENT_ID, EBAY_CLIENT_SECRET
    HTTP_RETRIES = max(0, int(config.get("http_retries", 2)))
    EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", config.get("ebay_client_id", "") or "")
    EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", config.get("ebay_client_secret", "") or "")
    if _ebay_api_enabled():
        log.info("[ebay-api] chiavi presenti: eBay.it/eBay.de via API ufficiale")
    if chrome_requests is not None:
        log.info("[http] motore: curl_cffi (impronta Chrome)")
    else:
        log.warning(
            "[http] motore: requests standard — molti portali lo bloccano con 403. "
            "Consigliato: pip install curl-cffi"
        )

    searches = load_searches(config)
    state = load_state(use_state=use_state)
    blocks = run_searches(config, state, searches)

    # Pulizia: elimina dallo stato le ricerche che non esistono piu'
    # (cancellate o rinominate), per non trascinarsi voci orfane per sempre.
    valid_keys: set[str] = set()
    for s in searches:
        valid_keys.add(s.get("id") or _slug(s["name"]))
        valid_keys.add(s["name"])
    for stale_key in [k for k in state.get("seen", {}) if k not in valid_keys]:
        del state["seen"][stale_key]
        log.info("[state] rimossa dallo stato la ricerca eliminata '%s'", stale_key)
    for stale_key in [k for k in state.get("archive", {}) if k not in valid_keys]:
        del state["archive"][stale_key]

    issues = collect_portal_issues()
    email_max = int(config.get("email_max_items", 60))
    md_text, html_text, totals = build_reports(blocks, issues, email_max)
    write_webapp(blocks, config, issues)

    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    if use_state:
        state["last_run"] = now_iso
        save_state(state)

    pieces = []
    if totals["new"]:
        pieces.append(f"{totals['new']} nuovi")
    if totals["drops"]:
        pieces.append(f"{totals['drops']} ribassi")
    if totals["removed"]:
        pieces.append(f"{totals['removed']} usciti")
    subject = f"[Moto] {', '.join(pieces) if pieces else 'nessuna novità'} — {dt.date.today():%d/%m/%Y}"

    always = bool(config.get("notify_when_empty", False))
    if notify and (any(totals.values()) or always):
        if config.get("notify_email", True):
            send_email(subject, html_text, config)
        if config.get("notify_telegram", False):
            send_telegram(f"{subject}\n\n{md_text}", config)

    return {
        "last_run": now_iso,
        "total_new": totals["new"],
        "total_drops": totals["drops"],
        "total_removed": totals["removed"],
        "total_current": sum(len(b["current"]) for b in blocks),
        "portal_issues": issues,
        "per_search": [
            {
                "name": b["name"],
                "current": len(b["current"]),
                "new": len(b["new"]),
                "drops": len(b.get("price_drops", [])),
                "removed": len(b.get("removed", [])),
            }
            for b in blocks
        ],
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Moto Monitor")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="non inviare notifiche")
    ap.add_argument("--no-state", action="store_true", help="ignora lo stato salvato")
    args = ap.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(exc)
        return 1

    summary = execute(config, notify=not args.dry_run, use_state=not args.no_state)
    if args.dry_run:
        print("\n[notifiche] saltate (dry-run).")
    print(
        f"\nFatto. Nuovi: {summary['total_new']} · Ribassi: {summary['total_drops']} · "
        f"Rimossi: {summary['total_removed']} · Attivi: {summary['total_current']}."
    )
    for i in summary["portal_issues"]:
        stato = "nessuna risposta" if i["level"] == "down" else "risposte parziali"
        print(f"  [!] {i['label']}: {stato} ({i['detail']})")
    print("Report in report.md / report.html · Web app in docs/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
