# Cheaper Cardboard

A free price list for Magic: The Gathering sealed products. Prices are collected from each store's own website and link back to it. No ads, no trackers, no accounts.

> This file is public, because the repository is public. It is the owner's guide for running the site. Keep private notes and spreadsheets out of this repository.

## How it works

1. **Your computer collects the prices** (stores often block GitHub's servers, but not a home connection).
2. It pushes two small files to GitHub: `site/data.js` (today's prices) and `data/history.json` (the daily price history).
3. **GitHub publishes the site.** It builds a page for every product from those two files and puts it online. It never contacts a store.

## One-time setup checklist

- [ ] Repository is named `cheapercardboard.github.io` (it must match the account name exactly).
- [ ] **Settings > Pages > Build and deployment > Source** is set to **GitHub Actions**.
- [ ] `.github/workflows/publish.yml` exists, and there is **no** `update.yml` in `.github/workflows`.
- [ ] Python 3 is installed, and `py -3 -m pip install certifi` has been run once.
- [ ] Optional but kind: give the collector a real contact address so a store owner could reach you. In a terminal run `setx CONTACT_EMAIL "your-project-email@example.com"` with your own address, then open a new terminal.

The first push shows a **red "Publish site" run**. That is expected: the starter `data.js` is sample data, and the safety check refuses to publish fewer than 200 products. It turns green after your first real collection.

## Daily routine

1. Double-click **`update-and-publish.bat`**.
2. Leave the window open until it says **Done**. A full collection takes a while.
3. Wait two or three minutes, then open `https://cheapercardboard.github.io/` and check the "Updated" date under the title.

The daily script uses **smart mode**: stores that carry normal sealed product are re-read every day, huge-catalog stores every other day, and stores with no Magic sealed product once a week. Stores that failed last time are always retried. The very first run on a computer reads every store, so it is the long one. Use `--refresh-hours 0` in place of `--smart` if you ever want to force everything to be re-read.

Keep the computer plugged in and awake while it runs (Windows: Settings > System > Power > Screen and sleep).

**On a Mac:** double-click `update-and-publish.command` instead of the `.bat` file. The first time, right-click it, choose **Open**, then **Open** again. If it will not run, type `chmod +x update-and-publish.command` in a terminal in this folder. You can also type the steps by hand:

```
git pull --rebase --autostash
caffeinate -i python3 collect_prices.py --all --refresh-hours 0
git add site/data.js data/history.json
git commit -m "Update prices"
git push
```

The last lines of a run look like this:

```
Done: 1800 products (120 without a matched set) written to site/data.js.
  60 suspicious prices set aside (...); 900 listings left out, each with a reason (...).
  40 listings were on the page last time and are gone now; see changes_since_last_build.csv for why.
  Watchlist: ALL OK.
```

**Healthy:** a product count close to yesterday's, and "Watchlist: ALL OK".
**Look closer if:** the product count drops by about a third or more, the watchlist says MISSING, or many stores show `failed` in the log.

### Run it automatically (optional)

1. Open **Task Scheduler > Create Basic Task**.
2. Name it, choose **Daily**, and pick a time (the computer must be on and awake).
3. Action: **Start a program**, and choose `update-and-publish.bat` in this folder.
4. Open the `.bat` file and delete the final `pause` line, so it can finish without waiting for a key.

Run it **no more than once a day**. Stores are small businesses, and the collector already waits between requests.

## Weekly check (five minutes)

- Read the summary lines from a recent run (above).
- Open `store_report.csv` in Excel and sort by the `error` column. A few failures are normal. If many stores fail, check your internet, VPN, or antivirus.
- Open `watchlist_report.csv`. Any `MISSING` row means a listing you wanted is gone. Search `dropped_listings.csv` for the store name to see why.
- Skim the live site on your phone and in dark mode.

## Monthly upkeep

- Run `py -3 -m pip install --upgrade certifi`.
- Look at the **Actions** tab for yellow warnings. GitHub occasionally retires old versions of the tools the workflow uses. If you see one, ask for an updated `publish.yml`.
- Check the repository size (Settings > General). `data.js` changes every day, so the history slowly grows.
- Review the store list (below).

## Adding or removing a store

Open `store_websites.csv`. Each row is `name,website,is_shopify`:

```
Example Games,https://examplegames.com,yes
```

- **To add one:** first test that it is a Shopify store by opening `https://their-site.com/products.json` in a browser. If you see text starting with `{"products":[`, it is, so use `yes`. If you get an error page, it is not supported yet.
- **To remove one:** delete its row.
- Then run the daily routine as usual.

## Guarding listings you care about

`must_keep.csv` lists listings that must always appear, one per row:

```
store,product,max_price
Guubuu,Reality Fracture Play Booster Box,125
```

After each run, `watchlist_report.csv` says `OK` or `MISSING` for each row.

## Files in your project folder

| File or folder | What it is | Pushed to GitHub? |
|---|---|---|
| `collect_prices.py` | The price collector | Yes |
| `update-and-publish.bat`, `update-and-publish.command` | The daily one-click script (Windows, Mac) | Yes |
| `store_websites.csv` | Store names and websites | Yes |
| `must_keep.csv` | Your watchlist | Yes |
| `site/index.html`, `site/product-template.html` | The main page and the product page design | Yes |
| `site/data.js` | Today's prices (made by each run) | Yes |
| `data/history.json` | Daily price history (made by each run) | Yes |
| `site/p/` | One page per product (made automatically) | No (GitHub builds these) |
| `cache/`, `backups/`, `scryfall_sets.json` | Saved store data, your last 10 `data.js` files, the set list | No |
| `store_report.csv`, `dropped_listings.csv`, `suspicious_prices.csv`, `assumed_matches.csv`, `changes_since_last_build.csv`, `watchlist_report.csv`, `price_history.csv` | Reports about each run | No |

## Troubleshooting

| What you see | What to do |
|---|---|
| The "Publish site" run is red and says `N products` with N under 200 | `data.js` is the sample file or the last collection failed. Run `update-and-publish.bat`. |
| The site shows a 404 page | Check Settings > Pages > Source is **GitHub Actions**, and that the latest "Publish site" run is green. |
| The site's "Updated" date is old | The daily routine did not run or did not push. Run the `.bat` and watch for errors. |
| `git push` is rejected (`fetch first`) | Run `git pull --rebase --autostash`, then `git push`. |
| `py` is not recognized | Python is not installed, or "Add Python to PATH" was not ticked. Reinstall it. |
| A certificate error | Run `py -3 -m pip install certifi`. |
| Many stores say `failed` | Check your internet, VPN, and antivirus, then run it again. A handful of failures is normal. |
| A listing is missing from the site | Search `dropped_listings.csv` for the store name. It says why. |
| A product page chart is empty | It needs at least two days of data. It fills in as you run the routine daily. |
| Something broke the site | Copy your newest good file from `backups\` over `site\data.js`, then commit and push. |

## Keeping private things out of the repository

The repository is public, so only the files in the table above should ever be pushed. The `.bat` file pushes only `site/data.js` and `data/history.json`. Before any manual `git add .`, run `git status` and read the list. Never add spreadsheets that came from another site's directory, and never add passwords, tokens, or private email addresses.

## Footer promise

The site's footer says "No trackers, no accounts, no ads." If you ever add analytics, a visit counter, or ads, change that line first.
