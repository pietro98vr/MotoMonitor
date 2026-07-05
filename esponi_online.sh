#!/usr/bin/env bash
# =====================================================================
#  Mette online il Moto Monitor GRATIS e SENZA REGISTRAZIONE, esponendo
#  il server che gira sul TUO computer tramite un tunnel pubblico.
#
#  Perche' cosi' e non su un cloud? I portali (Subito, eBay, Mobile.de...)
#  bloccano gli IP dei datacenter: un servizio in cloud verrebbe respinto
#  con 403 e non troverebbe nulla. Girando sul tuo computer (IP domestico)
#  lo scraping funziona; il tunnel rende il server raggiungibile da fuori.
#
#  Uso:   bash esponi_online.sh
#  Ferma: Ctrl+C (chiude anche il server locale)
# =====================================================================
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

# Consiglio di sicurezza: se esponi in pubblico, imposta un token.
if [ -z "${ADMIN_TOKEN:-}" ]; then
  echo "⚠  Nessun ADMIN_TOKEN impostato: chiunque abbia il link potra' modificare le ricerche."
  echo "   Per proteggere: ADMIN_TOKEN=unaparolasegreta bash esponi_online.sh"
  echo ""
fi

# Avvia il server locale in background e assicurane la chiusura all'uscita.
python3 server.py &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null' EXIT
sleep 2
echo "▶  Server locale su http://127.0.0.1:${PORT}  (PID $SERVER_PID)"
echo ""

if command -v cloudflared >/dev/null 2>&1; then
  echo "🌐  Espongo con Cloudflare (nessuna registrazione). L'URL pubblico compare qui sotto:"
  echo "    (l'editor e' all'URL principale; la vista per l'acquirente e' <URL>/view)"
  echo ""
  cloudflared tunnel --url "http://localhost:${PORT}"
else
  echo "ℹ  'cloudflared' non installato: uso localhost.run via SSH (niente da installare, niente account)."
  echo "   L'URL pubblico compare qui sotto; la vista per l'acquirente e' <URL>/view"
  echo ""
  ssh -o StrictHostKeyChecking=accept-new -R "80:localhost:${PORT}" nokey@localhost.run
fi
