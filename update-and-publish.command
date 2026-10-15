#!/bin/bash
# Mac version: collect prices on this computer, then push the two small data files to GitHub.
# GitHub then builds the product pages and publishes the site.
cd "$(dirname "$0")" || exit 1

fail() {
  echo
  echo "Something went wrong. Scroll up to read the message above, and copy it if you need help."
  read -n 1 -s -r -p "Press any key to close."
  exit 1
}

echo "=== Getting the latest from GitHub ==="
git pull --rebase --autostash || fail

echo "=== Collecting prices (this takes a while; the Mac is kept awake) ==="
caffeinate -i python3 collect_prices.py --all --refresh-hours 0 || fail

echo "=== Sending the new prices to GitHub ==="
git add site/data.js data/history.json
git diff --cached --quiet || git commit -m "Update prices $(date +%F)"
git push || fail

echo
echo "Done. GitHub will publish the site in a couple of minutes."
read -n 1 -s -r -p "Press any key to close."
