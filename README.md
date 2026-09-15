# Firegate Momentum Scanner — configurazione gratuita

## Cosa fa
Ogni 10 minuti durante la seduta USA:
1. legge i top gainers da Alpaca;
2. richiede almeno +4%;
3. verifica market cap > $2 miliardi;
4. verifica liquidità media > $20M/giorno;
5. cerca volume in accelerazione e/o breakout;
6. se il segnale è forte, invia su Telegram solo:

    AddFiregate TMO

7. non reinvia lo stesso ticker nella stessa giornata.

## Limiti della versione gratuita
- Il prezzo/movers arriva da Alpaca.
- Il volume intraday consolidato usa `delayed_sip`, quindi è circa 15 minuti ritardato.
- La capitalizzazione viene recuperata gratuitamente tramite `yfinance` e messa in cache per 7 giorni.
- Non c'è analisi AI delle notizie. La presenza di news recenti è solo un fattore aggiuntivo.

## 1. Crea un repository GitHub
Può essere pubblico o privato.
- Pubblico: GitHub Actions standard è gratuito e illimitato.
- Privato: GitHub Free include un monte minuti mensile; lo scheduler a 10 minuti è più prudente.

Carica nel repository:
- `scanner.py`
- `requirements.txt`
- `.github/workflows/scanner.yml`
- `alerts_state.json`
- `market_caps.json`

## 2. Aggiungi i Secrets
Repository → Settings → Secrets and variables → Actions → New repository secret

Crea esattamente questi 4:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Per un canale pubblico Telegram, `TELEGRAM_CHAT_ID` può essere:
`@NomeDelCanale`

Il bot deve essere amministratore e avere il permesso di pubblicare.

## 3. Test Telegram
GitHub → Actions → Firegate Momentum Scanner → Run workflow

Seleziona:
`test_telegram = true`

Premi Run workflow.

Deve arrivare:
`AddFiregate TMO`

## 4. Test scanner
Esegui di nuovo manualmente con:
`test_telegram = false`

Se il mercato USA è chiuso, vedrai nei log:
`Mercato USA chiuso. Nessuna scansione.`

Durante il mercato, vedrai i ticker controllati nei log.

## 5. Scheduler
Di default gira ogni 10 minuti tra le 09:00 e le 16:59 ET, lun-ven.
Lo script usa l'orologio Alpaca e lavora solo se la sessione è effettivamente aperta.

Se il repository è pubblico e vuoi controlli più frequenti, modifica:
`*/10`
in:
`*/5`

GitHub Actions supporta come intervallo minimo 5 minuti.

## Regole iniziali
Sempre:
- rialzo >= 4%
- market cap >= $2B
- average dollar volume >= $20M

Poi serve almeno una conferma forte:
- RVOL pace >= 1.8x
- breakout 20 giorni
- breakout / test massimo 52 settimane
- rialzo >= 7% con volume almeno 1.2x
- breakout del massimo precedente con RVOL >= 1.4x
- news recente + rialzo >= 5% + RVOL >= 1.3x

Le soglie si modificano nel file `.github/workflows/scanner.yml`.

## Nota su Alpaca gratuito
Per l'RVOL usiamo il volume SIP consolidato ritardato di circa 15 minuti, mentre il mover/prezzo viene usato come trigger più tempestivo. È un compromesso per restare senza abbonamenti a dati real-time consolidati.
