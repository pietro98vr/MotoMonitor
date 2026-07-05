# Moto Monitor

Servizio di tracciamento quotidiano degli annunci di moto d'epoca e ricambi sui
portali italiani ed europei. Ogni giorno interroga i portali per le ricerche
configurate, riconosce gli annunci **nuovi** rispetto al giorno prima e invia un
**riepilogo** (via e-mail o Telegram) con i nuovi annunci e l'elenco di tutti
quelli attualmente attivi.

## Come funziona

1. Le *ricerche* (già precompilate con Kawasaki 350 S2, KTM 125 GS, KTM 175 GS e
   i ricambi Kawasaki: borsa attrezzi, parafango posteriore, portachiavi) si
   gestiscono dall'**interfaccia web** o nel file `searches.json`. Ogni ricerca ha
   più stringhe etichettate e i portali su cui cercarla.
2. Per ogni ricerca il programma **genera da solo le varianti** delle parole
   chiave (inversione marca/modello/cilindrata, sinonimi di contesto come
   "conservata" o "restauro") e interroga i portali attivi.
3. Confronta i risultati con lo stato precedente (`state.json`) e a ogni giro
   produce tre cose: il riepilogo per l'**e-mail** (con foto), i file
   `report.md`/`report.html` e la **web app** `docs/index.html`.

## Due modi di consultare gli stessi dati

- **E-mail giornaliera** — riepilogo con sezione *Nuovi* ed elenco generale di
  tutti gli annunci attivi, **foto incluse**. Va bene per te che segui il flusso.
- **Web app** (`docs/index.html`) — pagina unica con la vista aggregata a schede,
  foto, filtro per categoria, ricerca, "solo nuovi" e ordinamento. È il link da
  passare alla persona per cui fai da intermediario: apre la pagina e sfoglia gli
  annunci con le immagini, senza dover leggere l'e-mail. È un'app React (senza
  build, come l'editor), curata per l'uso da telefono.

La web app non ha bisogno di alcun server: è un singolo file che il servizio
Python rigenera a ogni esecuzione. Puoi aprirlo con un doppio clic **oppure**
pubblicarlo come sito (vedi sotto) per avere un indirizzo condivisibile.


## Perché serve farlo girare "da qualche parte"

Io (l'assistente) non posso inviarti messaggi ogni giorno da solo: un servizio che
lavora in autonomia deve girare su un computer sempre acceso o su un servizio
programmato. Hai due strade.

### Opzione A — Sul tuo computer (la più affidabile)

```bash
pip install -r requirements.txt
python monitor.py --no-state      # primo giro di prova: mostra tutto, non invia
python monitor.py --dry-run       # giro reale ma senza inviare notifiche
python monitor.py                 # giro reale con invio
```

Poi programma l'esecuzione giornaliera:

- **macOS/Linux** (`crontab -e`), esempio ogni giorno alle 8:00:
  ```
  0 8 * * * cd /percorso/moto-monitor && /usr/bin/python3 monitor.py >> log.txt 2>&1
  ```
- **Windows**: *Utilità di pianificazione* → azione "Avvia programma" → `python` con
  argomento `monitor.py` e "Inizia in" = cartella del progetto.

### Opzione B — GitHub Actions (gratis, zero manutenzione)

Il file `.github/workflows/monitor.yml` è già pronto: carica il progetto in un
repository GitHub e l'esecuzione parte ogni giorno da sola (e con il pulsante
*Run workflow* nella scheda **Actions**). Lo stato viene salvato nel repository.

> **Avvertenza onesta**: i portali a volte bloccano gli IP dei datacenter (quindi
> anche quelli di GitHub), mostrando una pagina di verifica invece dei risultati.
> Se noti report vuoti su GitHub ma pieni sul tuo computer, usa l'Opzione A oppure
> aggiungi un proxy. Per un uso personale l'Opzione A resta la più solida.

## Pubblicare la web app come sito (link condivisibile)

Se usi GitHub, puoi trasformare `docs/index.html` in un sito con un indirizzo da
inviare alla persona interessata, gratis e senza server: vai su **Settings →
Pages**, imposta *Source: Deploy from a branch*, scegli il branch `main` e la
cartella **/docs**, salva. Dopo il primo aggiornamento avrai un indirizzo del tipo
`https://<tuo-utente>.github.io/<nome-repo>/`. La pagina si aggiorna da sola ogni
volta che il servizio gira e riscrive `docs/index.html`.

In locale, invece, apri direttamente `docs/index.html` con un doppio clic.

### Aggiornare la pagina dal tuo PC a ogni accesso (Windows)

Questa è la combinazione migliore: il giro parte dal **tuo computer** (IP
domestico, quindi lo scraping funziona) e poi pubblica solo la pagina già pronta
su GitHub Pages, che ti dà un indirizzo fisso e sempre raggiungibile. Lo script
`aggiorna_pages.ps1` esegue il monitor e fa `commit` + `push` della nuova pagina.

Prerequisiti (una volta sola): Python e Git installati; la cartella del progetto
è un repository Git collegato al tuo repo GitHub (`origin`) con Pages su
`main`/`docs`; e aver fatto **un `git push` manuale** all'inizio, così le
credenziali restano memorizzate (i push automatici non possono chiedere la
password).

```powershell
# Esecuzione singola (giro + pubblicazione)
powershell -NoProfile -ExecutionPolicy Bypass -File .\aggiorna_pages.ps1

# Avvio automatico a ogni accesso a Windows
powershell -NoProfile -ExecutionPolicy Bypass -File .\aggiorna_pages.ps1 -Install

# Rimuovere l'avvio automatico
powershell -NoProfile -ExecutionPolicy Bypass -File .\aggiorna_pages.ps1 -Uninstall
```

`-Install` registra un'attività in *Utilità di pianificazione* con avvio "al
login". Se non vuoi che rigiri a ogni accesso ravvicinato, usa
`-MinIntervalHours 12` (salta se l'ultimo giro è più recente di 12 ore). Le
esecuzioni vengono annotate in `pages_update.log`. Se lo script non pubblica
nulla, di norma è perché non ci sono novità (nessun commit vuoto) o perché da IP
domestico un portale non ha restituito risultati.


## Notifiche

Imposta i recapiti come **secrets** (GitHub: *Settings → Secrets and variables →
Actions*) oppure come variabili d'ambiente in locale. Non metterli in chiaro in
`config.yaml`.

**E-mail (con Gmail):** attiva la verifica in due passaggi e crea una *password per
le app*; usala come `SMTP_PASS`.
```
SMTP_HOST=smtp.gmail.com   SMTP_PORT=587
SMTP_USER=tuo.indirizzo@gmail.com
SMTP_PASS=la-password-per-le-app
MAIL_TO=tuo.indirizzo@email.it
```

**Telegram (opzionale, comodo sul telefono):** crea un bot con @BotFather, avvia una
chat con lui, poi imposta `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`, e in `config.yaml`
metti `notify_telegram: true`.

## Gestire le ricerche dall'interfaccia web (server locale)

Per aggiungere o modificare le ricerche senza toccare i file c'è un piccolo
server web. Lo avvii sul tuo computer:

```bash
pip install -r requirements.txt
python server.py
```

L'editor è un'app React caricata al volo, senza passaggi di build: non serve
installare Node. Le librerie (React e il traduttore JSX) e i caratteri vengono
presi da CDN, quindi al primo avvio serve una connessione a internet.

Poi apri **http://127.0.0.1:8000**. Da lì puoi:

- creare/eliminare ricerche e dare a ciascuna un nome;- scegliere **su quali portali** cercarla (Subito, eBay…);
- aggiungere **quante stringhe vuoi** per la stessa ricerca, ognuna con una
  propria **etichetta** (es. «diretta», «invertita», «restauro») e un
  interruttore per attivarla/disattivarla singolarmente — l'etichetta ricompare
  poi sull'annuncio trovato ("trovato con: invertita · Subito"), così sai sempre
  quale stringa e quale portale l'hanno pescato;
- «Proponi varianti» genera automaticamente permutazioni e sinonimi da una
  stringa base, che puoi tenere o scartare;
- «Esegui ora» lancia subito un giro (con o senza invio e-mail) e aggiorna la
  vista annunci.

Tutto viene salvato in **searches.json**, che è la fonte unica: il servizio
giornaliero (`monitor.py`) e la web app leggono sempre da lì. Il blocco `watches`
in `config.yaml` serve solo a creare `searches.json` la prima volta, se manca.

### Chi fa cosa

- **Tu (intermediario)** usi il server locale per impostare ed eseguire le
  ricerche. Girando sul tuo computer, il recupero dati non incontra i blocchi
  degli IP dei datacenter.
- **La persona interessata** riceve il link della **vista annunci** (statica, con
  foto) — `docs/index.html`, apribile in locale o pubblicata su GitHub Pages. Non
  serve che acceda al server.
- **L'e-mail giornaliera** continua ad arrivarti con «Nuovi» e riepilogo generale.

### Esporre il server in rete (facoltativo)

Di default il server ascolta solo su `127.0.0.1` (il tuo computer). Per un accesso
da altri dispositivi o un link condivisibile, vedi la sezione qui sotto.

## Mettere online gratis, senza registrazione

**Premessa onesta.** Il "deploy su un cloud" è la risposta ovvia ma qui *non*
funziona per la parte che conta: i portali (Subito, eBay, Mobile.de, …) bloccano
gli IP dei datacenter — nel collaudo hanno tutti risposto 403. Un servizio messo
su Render/Railway/Fly & simili girerebbe, ma le ricerche tornerebbero vuote. In
più quei piani gratuiti richiedono comunque la registrazione. Lo scraping deve
partire da un IP "domestico": cioè dal tuo computer.

La soluzione che risolve tutto — gratis e senza account — è **tenere il server sul
tuo computer ed esporlo con un tunnel pubblico**. Così lo scraping funziona (IP
domestico) e ottieni un indirizzo pubblico da usare ovunque e da girare
all'acquirente.

Ho incluso due script che avviano il server e aprono il tunnel:

```bash
# macOS / Linux
ADMIN_TOKEN=unaparolasegreta bash esponi_online.sh
```
```bat
:: Windows (doppio clic, oppure da terminale)
esponi_online.bat
```

Lo script stampa un URL pubblico. L'**editor** è all'indirizzo principale; la
**vista per l'acquirente** è `<URL>/view` — è quello il link da girare.

Due modi, entrambi senza registrazione:
- **localhost.run** (predefinito negli script): usa solo `ssh`, già presente su
  macOS/Linux e Windows 10+. Niente da installare, nessun account.
- **Cloudflare quick tunnel**: più stabile. Installa una volta `cloudflared`
  (`brew install cloudflared` o scaricando il binario); nessun account. Se
  presente, lo script lo usa in automatico.

**Importante (sicurezza).** Un tunnel è pubblico: chi ha l'URL può aprire
l'editor. Imposta sempre un `ADMIN_TOKEN` (come sopra) — così *modificare* le
ricerche e *lanciare* un giro richiedono il token, che scrivi nel campo apposito
dell'editor; l'acquirente con il solo link `/view` vede gli annunci ma non può
toccare nulla. Tieni `server_host: "127.0.0.1"`: il tunnel si collega comunque a
localhost.

**Limite da sapere.** L'URL gratuito è temporaneo e cambia ad ogni riavvio del
tunnel, e resta attivo solo mentre il tuo computer è acceso (è la stessa
condizione che fa funzionare lo scraping). Per un indirizzo *stabile* e
sempre-attivo della sola **vista annunci** (che è statica e non ha problemi di
blocco) hai due opzioni gratuite:
- **GitHub Pages** (vedi sopra) — richiede un account GitHub ma dà un URL fisso e
  si aggiorna da solo col giro giornaliero.
- **Netlify Drop** (app.netlify.com/drop) — trascini la cartella `docs/` nella
  pagina e ottieni un URL pubblico **senza login**; per aggiornarlo ritrascini la
  cartella dopo un nuovo giro.

## Modificare le ricerche via file (alternativa)

Puoi anche editare direttamente `searches.json`. Struttura di una ricerca:

```json
{
  "name": "Kawasaki 350 S2",
  "portals": ["subito", "ebay"],
  "price_max": 20000,
  "subito_category": "moto-e-scooter",
  "relevance_filter": true,
  "queries": [
    {"text": "kawasaki 350 s2", "label": "diretta",   "enabled": true},
    {"text": "kawasaki s2 350", "label": "invertita",  "enabled": true}
  ]
}
```


## Portali supportati

- **Subito.it** (Italia) — portale principale. Estrazione dai dati `__NEXT_DATA__`
  della pagina di ricerca: metodo robusto.
- **eBay.it** — utile per i ricambi italiani.
- **eBay.de** — ricambi dal mercato tedesco, spesso il più fornito per la
  Kawasaki S2 (borsa attrezzi, parafango, minuteria).
- **Mobile.de** (Germania) — grande bacino di usato d'epoca. *Best-effort*: legge
  i dati strutturati (JSON-LD) e ripiega sui link annuncio. È il portale con la
  protezione anti-bot più aggressiva: da un IP di datacenter viene quasi sempre
  bloccato, da rete domestica può funzionare; per un uso intenso servirebbe l'API
  ufficiale (con credenziali) o un browser reale. I parametri di ricerca possono
  richiedere un ritocco.
- **Kleinanzeigen.de** (ex eBay Kleinanzeigen, Germania) — moto e ricambi.
  *Best-effort* via lettura della lista annunci HTML.

Nell'editor web ogni ricerca ha le caselle di tutti questi portali: scegli caso
per caso su quali cercare. In `config.yaml`, sotto `portals`, puoi invece
disattivarne uno per tutte le ricerche. Come Subito, i portali tedeschi ed eBay
bloccano gli IP dei datacenter: girando il servizio sul tuo computer (Opzione A)
è dove hanno più probabilità di rispondere.

## Aggiungere ancora altri portali

Ogni portale è una funzione `fetch_<nome>(query, …)` in `monitor.py` che
restituisce dizionari con i campi `id, portal, title, price, location, url,
image`. Ne aggiungi una, la registri in `ADAPTERS` e in `PORTAL_LABELS`, e la
abiliti in `config.yaml`: comparirà da sola tra le caselle dei portali nell'editor.
Candidati non ancora inclusi: Marktplaats (Paesi Bassi), LeBonCoin (Francia),
Catawiki (aste) — questi ultimi con protezioni forti che di norma richiedono un
browser reale. Indicami quale ti serve e lo scrivo.

## Manutenzione

Gli adapter leggono la struttura attuale dei siti; i portali cambiano nel tempo e
un adapter può richiedere un ritocco. Subito è gestito leggendo il blocco dati
`__NEXT_DATA__` della pagina di ricerca; Mobile.de i dati JSON-LD; eBay e
Kleinanzeigen la lista annunci HTML. Se un giorno un portale smette di restituire
risultati, segnalamelo: di norma è una piccola correzione al relativo adapter.
