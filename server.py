#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Moto Monitor — server web locale.

Fornisce l'interfaccia per creare/modificare le ricerche, scegliere i portali e
gestire piu' stringhe (etichettate) per ogni ricerca. Salva tutto in
searches.json, che il servizio giornaliero (monitor.py) legge come fonte unica.

Avvio:
    python server.py
Poi apri  http://127.0.0.1:8000

Sicurezza: di default ascolta solo su 127.0.0.1 (il tuo computer). Se lo esponi
in rete, imposta la variabile d'ambiente ADMIN_TOKEN e passala come header
'X-Token' nelle richieste di scrittura.
"""

from __future__ import annotations

import os
import threading

from flask import Flask, jsonify, request, send_from_directory

import monitor

CONFIG_PATH = os.environ.get("MOTO_CONFIG", str(monitor.ROOT / "config.yaml"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

app = Flask(__name__, static_folder=None)

# Stato dell'esecuzione in background
RUN = {"running": False, "last_run": None, "summary": None, "error": None}
_lock = threading.Lock()


def _config() -> dict:
    return monitor.load_config(CONFIG_PATH)


def _authorized() -> bool:
    if not ADMIN_TOKEN:
        return True
    return request.headers.get("X-Token", "") == ADMIN_TOKEN


# --------------------------------------------------------------------------- #
# Pagine
# --------------------------------------------------------------------------- #
@app.get("/")
def admin():
    return send_from_directory(monitor.ROOT / "webapp", "admin.html")


@app.get("/view")
def view():
    docs = monitor.ROOT / "docs"
    if not (docs / "index.html").exists():
        return (
            "<p style='font-family:sans-serif;padding:2rem'>Nessuna vista ancora. "
            "Premi <b>Esegui ora</b> nell'editor per generarla.</p>"
        )
    return send_from_directory(docs, "index.html")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/portals")
def api_portals():
    cfg = _config()
    portals = [
        {"id": pid, "label": monitor.PORTAL_LABELS.get(pid, pid), "enabled": bool(cfg.get("portals", {}).get(pid))}
        for pid in monitor.ADAPTERS
    ]
    return jsonify(
        {
            "portals": portals,
            "subito_categories": ["moto-e-scooter", "accessori-moto"],
        }
    )


@app.get("/api/searches")
def api_get_searches():
    return jsonify({"searches": monitor.load_searches(_config())})


@app.put("/api/searches")
def api_put_searches():
    if not _authorized():
        return jsonify({"error": "non autorizzato"}), 403
    data = request.get_json(silent=True) or {}
    searches = data.get("searches")
    if not isinstance(searches, list):
        return jsonify({"error": "formato non valido: manca 'searches'"}), 400

    cleaned = []
    for s in searches:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        queries = []
        for q in s.get("queries", []):
            text = (q.get("text") or "").strip()
            if not text:
                continue
            queries.append(
                {
                    "text": text,
                    "label": (q.get("label") or "").strip(),
                    "enabled": bool(q.get("enabled", True)),
                }
            )
        cleaned.append(
            {
                "id": (s.get("id") or monitor._slug(name)).strip(),
                "name": name,
                "portals": [p for p in s.get("portals", []) if p in monitor.ADAPTERS],
                "price_max": s.get("price_max") if isinstance(s.get("price_max"), (int, float)) else None,
                "subito_category": (s.get("subito_category") or "moto-e-scooter").strip(),
                "relevance_filter": bool(s.get("relevance_filter", True)),
                "queries": queries,
            }
        )
    monitor.save_searches({"searches": cleaned})
    return jsonify({"ok": True, "count": len(cleaned)})


@app.get("/api/status")
def api_status():
    return jsonify(RUN)


@app.post("/api/run")
def api_run():
    if not _authorized():
        return jsonify({"error": "non autorizzato"}), 403
    if RUN["running"]:
        return jsonify({"error": "un'esecuzione e' gia' in corso"}), 409
    notify = bool((request.get_json(silent=True) or {}).get("notify", False))

    def worker():
        with _lock:
            RUN.update(running=True, error=None)
        try:
            summary = monitor.execute(_config(), notify=notify, use_state=True)
            RUN.update(summary=summary, last_run=summary["last_run"])
        except Exception as exc:  # noqa: BLE001
            RUN.update(error=str(exc))
        finally:
            RUN.update(running=False)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True, "notify": notify})


if __name__ == "__main__":
    cfg = _config()
    host = cfg.get("server_host", "127.0.0.1")
    port = int(cfg.get("server_port", 8000))
    print(f"Moto Monitor server su http://{host}:{port}  (Ctrl+C per fermare)")
    app.run(host=host, port=port, debug=False)
