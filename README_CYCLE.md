# FireGate Cycle Scanner

Scanner gratuito per titoli USA con **ciclicità/statistica favorevole a una risalita**
e conferma intraday corrente.

## Cosa significa "ciclicità"
Lo script non usa una vaga etichetta di "titolo ciclico". Calcola statisticamente:
- frequenza storica di rialzo nello stesso giorno della settimana;
- rendimento medio e frequenza positiva nello stesso mese;
- rendimento del giorno successivo dopo pullback 1 giorno simili a quello attuale;
- rendimento del giorno successivo dopo pullback 3 giorni simili;
- rendimento del giorno successivo dopo pullback 5 giorni simili;
- mean reversion dopo sequenze di giorni rossi.

Usa circa 3 anni di storico giornaliero disponibile.

## Conferma live
Un buon pattern storico NON basta. Per mandare Telegram servono anche:
- ultimi 15 minuti positivi;
- prezzo sopra VWAP oppure higher low;
- posizione intraday non sui minimi;
- volume / RVOL di supporto;
- sentiment news non fortemente negativo;
- market cap >= $2B;
- average dollar volume >= $20M/giorno.

## Analyst estimates
yfinance aggiunge:
- consensus Buy / Hold / Sell;
- target medio;
- upside rispetto al prezzo;
- numero di analisti.

È solo un fattore del punteggio, non un trigger da solo.

## Universo
Parte dagli 80 titoli USA più attivi secondo Alpaca e analizza al massimo i primi 60.

Alpaca documenta l'endpoint:
`/v1beta1/screener/stocks/most-actives`

## Output
Massimo 5 alert per scansione:
`AddFiregate TICKER`

Se non c'è una configurazione abbastanza forte, non invia nulla.

## Installazione
Carica nello stesso repository GitHub:
- cycle_scanner.py
- requirements_cycle.txt
- cycle_alerts_state.json
- cycle_meta_cache.json
- .github/workflows/cycle-scanner.yml

Riutilizza i Secrets:
- ALPACA_API_KEY
- ALPACA_SECRET_KEY
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

## Primo test
GitHub > Actions > FireGate Cycle Scanner > Run workflow

Usa:
- test_telegram = false
- dry_run = true

Nei log cerca:
`[CHECK]`
e
`[DRY RUN] AddFiregate XYZ`

## Attivazione
Esegui una volta con:
- test_telegram = false
- dry_run = false

Lo schedule poi gira automaticamente ogni 5 minuti, 09:00-15:59 ET.
Lo script effettua realmente scansioni solo 09:45-15:45 ET.

## Nota
La stagionalità e la mean reversion sono statistiche storiche; non implicano che
un rialzo debba verificarsi oggi. Per questo lo script richiede anche conferme
intraday reali prima dell'alert.
