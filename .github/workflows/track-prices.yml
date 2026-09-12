name: Track prices

on:
  schedule:
    # Runs every 30 minutes, all day and night. Times are in UTC.
    - cron: "*/30 * * * *"
  workflow_dispatch: {}   # lets you also trigger it manually from the Actions tab

permissions:
  contents: write

jobs:
  check-prices:
    runs-on: ubuntu-latest
    steps:
      - name: Check out repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install Python dependencies
        run: pip install -r requirements.txt

      - name: Install Playwright browser
        run: python -m playwright install --with-deps chromium

      - name: Run price tracker
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python price_tracker.py

      - name: Save updated prices back to the repo
        run: |
          git config user.name "price-tracker-bot"
          git config user.email "actions@github.com"
          git add last_prices.json
          git diff --quiet --cached || git commit -m "Update tracked prices"
          git push
