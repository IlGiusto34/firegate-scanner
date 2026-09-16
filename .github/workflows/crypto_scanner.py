name: FireGate Crypto Scanner

on:
  workflow_dispatch:
    inputs:
      test_telegram:
        description: "Invia AddFiregate BTC per test"
        required: false
        default: false
        type: boolean
      dry_run:
        description: "Analizza senza inviare Telegram"
        required: false
        default: true
        type: boolean

  schedule:
    - cron: "2/5 * * * *"

permissions:
  contents: write

concurrency:
  group: firegate-crypto-scanner
  cancel-in-progress: false

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 4

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"

      - name: Installa dipendenze
        run: pip install -r requirements_crypto.txt

      - name: Esegui crypto scanner
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          TEST_TELEGRAM: ${{ github.event_name == 'workflow_dispatch' && inputs.test_telegram && 'true' || 'false' }}
          DRY_RUN: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run && 'true' || 'false' }}

          SCAN_TOP_LIQUID: "60"
          MIN_DOLLAR_VOLUME_24H: "15000000"
          MIN_MARKET_CAP_USD: "100000000"
          MAX_SPREAD_PCT: "0.80"

          MIN_PRE_SCORE: "7.0"
          MIN_FINAL_SCORE: "10.0"
          MAX_ALERTS_PER_RUN: "3"

          COOLDOWN_HOURS: "6"
          REALERT_PRICE_GAIN_PCT: "3.0"
          REALERT_SCORE_IMPROVEMENT: "2.0"
          MAX_WORKERS: "5"
        run: python crypto_scanner.py

      - name: Salva stato anti-duplicati
        if: always()
        run: |
          git config user.name "firegate-crypto-scanner"
          git config user.email "firegate-crypto-scanner@users.noreply.github.com"
          touch crypto_alerts_state.json
          git add crypto_alerts_state.json

          if git diff --cached --quiet; then
            echo "Nessuna modifica da salvare."
            exit 0
          fi

          git commit -m "Update crypto scanner state [skip ci]"
          git pull --rebase
          git push
