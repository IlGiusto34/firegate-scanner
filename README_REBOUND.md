# FireGate Rebound Scanner

Scanner contrarian intraday gratuito per titoli USA.

## Obiettivo
Parte dai peggiori titoli liquidi della seduta e cerca quelli che stanno mostrando un vero tentativo di rimbalzo. Invia al massimo 5 ticker per scansione nel formato:

`AddFiregate TICKER`

## Fonti
- Alpaca: movers, market clock, intraday 5m IEX, storico giornaliero SIP, news
- yfinance: market cap, analyst recommendation e price target
- GDELT + Google News RSS: conferma news/sentiment
- Telegram Bot API

## Filtri duri
- market cap >= $2B
- prezzo >= $5
- average dollar volume >= $20M/giorno
- perdita intraday fra -3% e -15%

## Rebound Score
Premia recupero dal minimo, ultime 15/30m positive, higher low, recupero VWAP, volume in accelerazione, andamento settimanale non deteriorato, analyst consensus positivo, target upside e news positive.

## Veto automatici
Scarta setup con segnali di bankruptcy, frode/accounting concerns, short report, SEC investigation, guidance cut, offering/dilution, going concern/default, restatement/material weakness, delisting, FDA rejection o clinical hold.

## Installazione
Carica nello stesso repository GitHub:
- rebound_scanner.py
- requirements_rebound.txt
- rebound_alerts_state.json
- rebound_meta_cache.json
- .github/workflows/rebound-scanner.yml

Riutilizza i Secrets gia esistenti:
- ALPACA_API_KEY
- ALPACA_SECRET_KEY
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

## Test
GitHub > Actions > FireGate Rebound Scanner > Run workflow

Prima usa:
- test_telegram = false
- dry_run = true

Nei log:
- `[CHECK]` = candidato analizzato
- `signal=True` = setup qualificato
- `[DRY RUN] AddFiregate XYZ` = avrebbe inviato il comando

Poi esegui con `dry_run = false`.

## Frequenza
Ogni 5 minuti nei giorni feriali. Lo script opera solo 09:45-15:45 ET per evitare i primi e ultimi 15 minuti.

## Nota
Non esistono titoli destinati con certezza a risalire. Lo scanner seleziona configurazioni di rebound con piu conferme contemporanee.
