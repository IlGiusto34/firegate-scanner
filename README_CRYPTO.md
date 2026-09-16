# FireGate Crypto Scanner

Scanner crypto gratuito, H24, ogni 5 minuti. Non usa OpenAI e non richiede API key di mercato.

## Fonti
- Coinbase public market data: prezzo, volume, spread, candele
- GDELT: news globali
- Google News RSS: seconda fonte news, best effort
- Alternative.me Fear & Greed
- Telegram Bot API

Nessuna fonte gratuita copre letteralmente "tutto il web". GDELT + Google News vengono usati come aggregatori ampi. I rumor hanno peso ridotto e non possono creare un alert da soli.

## Segnali analizzati
- liquidita 24h
- spread
- momentum 5m, 15m, 1h, 4h
- volume spike 5m
- range expansion
- breakout 6h / 24h
- EMA 9/21
- RSI
- news positive/negative
- rumor
- Fear & Greed
- penalita per movimenti troppo estesi

Serve almeno una combinazione di segnali. Lo script non invia semplicemente il maggiore gainer.

## File
Metti nello stesso repository:
- crypto_scanner.py
- requirements_crypto.txt
- crypto_alerts_state.json
- .github/workflows/crypto-scanner.yml

Se usi lo stesso repo azionario, i secrets esistono gia:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

## Test consigliato
GitHub > Actions > FireGate Crypto Scanner > Run workflow

Prima:
- test_telegram = false
- dry_run = true

Controlla i log:
- [CHECK] = asset analizzato
- [FINAL] = candidato forte
- [DRY RUN] AddFiregate XYZ = avrebbe inviato l'alert

Poi esegui:
- test_telegram = false
- dry_run = false

## Alert
Telegram riceve solo:
AddFiregate BTC

## Anti-spam
Cooldown 6 ore. Dopo il cooldown lo stesso asset torna eleggibile solo se:
- prezzo almeno +3% dal precedente alert, oppure
- score migliorato di almeno 2 punti.

## Costo zero
Per eseguire 24/7 ogni 5 minuti senza consumare rapidamente i minuti di GitHub Free, usa un repository pubblico. Non inserire token nel codice: restano nei GitHub Secrets.

## Limiti
- GitHub Actions non e HFT e le esecuzioni schedulate possono subire ritardi.
- Il feed Coinbase non rappresenta tutta la liquidita globale.
- Google News RSS e best effort.
- Il sentiment e euristico, non AI.
- Nessun segnale garantisce profitto o rialzo futuro.
