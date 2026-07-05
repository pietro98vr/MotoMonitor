#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Moto Monitor — tracciamento quotidiano di annunci di moto d'epoca e ricambi.

Il programma interroga i portali configurati per ogni "ricerca" definita in
config.yaml, riconosce gli annunci nuovi rispetto all'esecuzione precedente
(state.json) e produce un riepilogo (report.html / report.md) che puo' essere
inviato via e-mail o Telegram.

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
import os
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

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
SEARCHES_PATH = ROOT / "searches.json"
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
def _http_get(url: str, params: dict | None = None) -> requests.Response | None:
    headers = {
        "User-Agent": UA,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Accept": "text/html,application/json,*/*",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=25)
        if r.status_code != 200:
            print(f"    [http] {url} -> HTTP {r.status_code}")
            return None
        return r
    except requests.RequestException as exc:
        print(f"    [http] errore su {url}: {exc}")
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


def _find_image_url(node) -> str | None:
    """Cerca ricorsivamente il primo URL di immagine in un sottoalbero JSON,
    dando priorita' alle chiavi che tipicamente contengono le foto.
    """
    if isinstance(node, str):
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
    """Estrae la foto principale di un annuncio (limitandosi ai rami 'immagini'
    per non prendere per errore l'avatar del venditore).
    """
    for key in ("images", "image", "thumbnail"):
        if key in node:
            found = _find_image_url(node[key])
            if found:
                return found
    return ""


def fetch_subito(query: str, category: str = "moto-e-scooter", region: str = "") -> list[dict]:
    """Adapter Subito.it — sito Next.js: si estrae il blob __NEXT_DATA__ dalla
    pagina dei risultati e se ne ricavano gli annunci. Nessun endpoint privato,
    solo la pagina pubblica di ricerca.
    """
    area = region if region else "italia"
    url = f"https://www.subito.it/annunci-{area}/vendita/{category}/"
    r = _http_get(url, params={"q": query})
    if r is None:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        print("    [subito] __NEXT_DATA__ non trovato (struttura cambiata?)")
        return []

    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        print("    [subito] JSON non valido")
        return []

    raw: list[dict] = []
    _walk_json(data, raw)

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


def fetch_ebay(query: str, domain: str = "www.ebay.it", portal_label: str = "eBay", id_prefix: str = "ebay") -> list[dict]:
    """Adapter eBay (best-effort). Ordina per annunci piu' recenti e legge la
    lista dei risultati dall'HTML. Utile soprattutto per i ricambi. Riutilizzabile
    su piu' domini eBay (es. eBay.de) cambiando 'domain'.
    """
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
        title = title_el.get_text(strip=True)
        if title.lower() in ("shop on ebay", "nuova inserzione", "neues angebot"):
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
def load_state(use_state: bool) -> dict:
    if not use_state or not STATE_PATH.exists():
        return {"seen": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"seen": {}}
    # Migrazione dal vecchio formato (lista di id) al nuovo (id -> data avvistamento)
    seen = state.get("seen", {})
    now = dt.datetime.now().isoformat(timespec="seconds")
    for name, val in list(seen.items()):
        if isinstance(val, list):
            seen[name] = {i: now for i in val}
    state["seen"] = seen
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
            print("[searches] searches.json non valido, uso config.yaml")
    # primo avvio: genera searches.json dal config e salvalo
    data = default_searches_from_config(config)
    save_searches(data)
    return data["searches"]


def save_searches(data: dict) -> None:
    SEARCHES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Raccolta
# --------------------------------------------------------------------------- #
def run_searches(config: dict, state: dict, searches: list[dict]) -> list[dict]:
    global_portals = config.get("portals", {})
    delay = float(config.get("request_delay_seconds", 2))
    seen = state.setdefault("seen", {})
    blocks: list[dict] = []

    for search in searches:
        name = search["name"]
        # portali: intersezione tra quelli scelti per la ricerca e quelli attivi globalmente
        chosen = search.get("portals") or [p for p, on in global_portals.items() if on]
        portals = [p for p in chosen if global_portals.get(p, False) and p in ADAPTERS]
        queries = [q for q in search.get("queries", []) if q.get("enabled", True)]
        query_texts = [q["text"] for q in queries]
        print(f"\n== {name} ==  portali={portals}  stringhe attive={len(queries)}")

        collected: dict[str, dict] = {}
        for portal in portals:
            adapter = ADAPTERS[portal]
            plabel = PORTAL_LABELS.get(portal, portal)
            for q in queries:
                text = q["text"]
                tag = q.get("label") or text
                print(f"  - [{plabel}] '{text}'  (etichetta: {tag})")
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

                for it in items:
                    if search.get("relevance_filter", True) and not is_relevant(it["title"], query_texts):
                        continue
                    pmax = search.get("price_max")
                    if pmax and _numeric_price(it["price"]) and _numeric_price(it["price"]) > pmax:
                        continue
                    it.setdefault("via_label", tag)
                    it.setdefault("via_portal", plabel)
                    it.setdefault("via", f"{tag} · {plabel}")
                    collected.setdefault(it["id"], it)
                time.sleep(delay)

        known = dict(seen.get(name, {}))
        now_iso = dt.datetime.now().isoformat(timespec="seconds")
        current = list(collected.values())
        new_items = []
        updated = dict(known)
        for it in current:
            prev = known.get(it["id"])
            it["first_seen"] = prev or now_iso
            it["is_new"] = it["id"] not in known
            if it["is_new"]:
                new_items.append(it)
            updated[it["id"]] = it["first_seen"]
        seen[name] = updated

        print(f"  => {len(current)} attivi, {len(new_items)} nuovi")
        blocks.append(
            {
                "name": name,
                "new": sorted(new_items, key=lambda x: x["title"].lower()),
                "current": sorted(current, key=lambda x: x["title"].lower()),
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
def build_reports(blocks: list[dict]) -> tuple[str, str, int]:
    today = dt.date.today().strftime("%d/%m/%Y")
    total_new = sum(len(b["new"]) for b in blocks)

    # ---- Markdown ----
    md = [f"# Riepilogo annunci moto — {today}", ""]
    md.append(f"**Nuovi annunci oggi: {total_new}**" if total_new else "*Nessun nuovo annuncio oggi.*")
    md.append("")
    for b in blocks:
        md.append(f"## {b['name']}")
        if b["new"]:
            md.append(f"### Nuovi ({len(b['new'])})")
            for it in b["new"]:
                md.append(f"- **{it['title']}** — {it['price']} · {it['location']} · {it['portal']}\n  {it['url']}")
        md.append(f"### Tutti gli annunci attivi ({len(b['current'])})")
        if not b["current"]:
            md.append("_Nessun risultato._")
        for it in b["current"]:
            flag = "🆕 " if it in b["new"] else ""
            md.append(f"- {flag}{it['title']} — {it['price']} · {it['location']} · {it['portal']}\n  {it['url']}")
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
        badge = (
            "<span style=\"background:#d2681e;color:#fff;font-size:11px;font-weight:bold;"
            "padding:2px 6px;border-radius:3px;margin-right:6px\">NUOVO</span>"
            if it.get("is_new")
            else ""
        )
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
        meta = " · ".join(filter(None, [esc(it["price"]), esc(it["location"]), esc(it["portal"]), f"dal {seen_date}" if seen_date else ""]))
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

    parts = [
        "<div style=\"font-family:Arial,Helvetica,sans-serif;max-width:680px;margin:auto;color:#222\">",
        f"<h2 style=\"margin-bottom:4px\">Riepilogo annunci moto — {today}</h2>",
    ]
    if total_new:
        parts.append(f"<p style=\"color:#0a7d29;font-weight:bold\">Nuovi annunci oggi: {total_new}</p>")
    else:
        parts.append("<p style=\"color:#777\">Nessun nuovo annuncio oggi.</p>")

    for b in blocks:
        parts.append(f"<h3 style=\"border-bottom:2px solid #eee;padding-bottom:4px;margin-top:22px\">{esc(b['name'])}</h3>")
        if b["new"]:
            parts.append(f"<p style=\"font-weight:bold;color:#0a7d29;margin-bottom:2px\">Nuovi ({len(b['new'])})</p>")
            for it in b["new"]:
                parts.append(item_row(it))
        parts.append(f"<p style=\"color:#555;margin-bottom:2px;margin-top:14px\">Tutti gli annunci attivi ({len(b['current'])})</p>")
        if not b["current"]:
            parts.append("<p style=\"color:#999\">Nessun risultato.</p>")
        for it in b["current"]:
            parts.append(item_row(it))
    parts.append("<hr><p style=\"font-size:12px;color:#999\">Generato automaticamente da Moto Monitor.</p></div>")
    html_text = "\n".join(parts)

    (ROOT / "report.md").write_text(md_text, encoding="utf-8")
    (ROOT / "report.html").write_text(html_text, encoding="utf-8")
    return md_text, html_text, total_new


def write_webapp(blocks: list[dict], config: dict) -> None:
    """Rigenera la web app statica (docs/index.html) iniettando i dati aggregati,
    foto comprese, nel template. Nessun backend: un unico file da aprire o da
    pubblicare (es. GitHub Pages).
    """
    template_path = ROOT / "webapp" / "template.html"
    if not template_path.exists():
        print("[webapp] template.html non trovato, generazione saltata.")
        return

    def clean(it: dict) -> dict:
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
        }

    data = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "title": config.get("webapp_title", "Officina — Monitor annunci"),
        "subtitle": config.get("webapp_subtitle", "Annunci aggregati di moto d'epoca e ricambi"),
        "total_current": sum(len(b["current"]) for b in blocks),
        "total_new": sum(len(b["new"]) for b in blocks),
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

    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html_out = template_path.read_text(encoding="utf-8").replace("__MOTO_DATA__", payload)

    out_dir = ROOT / "docs"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "index.html").write_text(html_out, encoding="utf-8")
    print(f"[webapp] docs/index.html aggiornato ({data['total_current']} annunci)")


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
        print("[email] configurazione incompleta, invio saltato.")
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
        print(f"[email] inviata a {to}")
    except Exception as exc:  # noqa: BLE001
        print(f"[email] errore invio: {exc}")


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
            print(f"[telegram] errore invio: {exc}")
            return
    print(f"[telegram] inviato a chat {chat_id}")


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
    searches = load_searches(config)
    state = load_state(use_state=use_state)
    blocks = run_searches(config, state, searches)
    md_text, html_text, total_new = build_reports(blocks)
    write_webapp(blocks, config)

    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    if use_state:
        state["last_run"] = now_iso
        save_state(state)

    subject = (
        f"[Moto] {total_new} nuovi annunci — {dt.date.today():%d/%m/%Y}"
        if total_new
        else f"[Moto] nessun nuovo annuncio — {dt.date.today():%d/%m/%Y}"
    )
    always = bool(config.get("notify_when_empty", False))
    if notify and (total_new or always):
        if config.get("notify_email", True):
            send_email(subject, html_text, config)
        if config.get("notify_telegram", False):
            send_telegram(f"{subject}\n\n{md_text}", config)

    return {
        "last_run": now_iso,
        "total_new": total_new,
        "total_current": sum(len(b["current"]) for b in blocks),
        "per_search": [{"name": b["name"], "current": len(b["current"]), "new": len(b["new"])} for b in blocks],
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
        f"\nFatto. Nuovi: {summary['total_new']} · Attivi: {summary['total_current']}. "
        "Report in report.md / report.html · Web app in docs/index.html"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
