CHEAPER CARDBOARD - SETUP
=========================

How it works
  Your computer collects the prices (stores often block GitHub's servers, but not your home
  connection). It pushes two small files: site/data.js (the prices) and data/history.json
  (the daily history). GitHub then builds a page for every product and publishes the site.
  GitHub never contacts a store.

What is in this folder
  collect_prices.py           the price collector
  update-and-publish.bat      one double-click: collect prices, then send them to GitHub (Windows)
  store_websites.csv          store name + website only
  must_keep.csv               listings you want guarded (edit as you like)
  site/index.html             the main page
  site/product-template.html  the design for each product's own page (price chart)
  site/data.js                sample data (replaced by your real prices after the first run)
  data/history.json           the daily price history, grows every time you run it
  .github/workflows/publish.yml   publishes the site whenever you push new data
  optional-cloud-collection.yml.txt  the old GitHub-collects-prices workflow. NOT used. Ignore it.

ONE-TIME SETUP
  1. Settings > Pages > Build and deployment > Source: choose "GitHub Actions".
  2. If your repository has a file named update.yml under .github/workflows, delete it
     (it is the old workflow that tried to collect prices on GitHub).
  3. Push these files to the repository.

EVERY DAY
  Double-click update-and-publish.bat. It takes a while the first time. When it says Done,
  wait a couple of minutes and your site is updated.
  The history chart gets one new point each day you run it; a product needs two days of
  data before its chart shows a line.

Run it automatically (optional)
  Windows Task Scheduler > Create Basic Task > Daily > Start a program > pick update-and-publish.bat.
  The computer must be on and awake at that time. Remove the final "pause" line from the .bat
  file if you do this.

If something looks wrong
  dropped_listings.csv says why any listing was left out; store_report.csv shows what
  happened at each store; changes_since_last_build.csv lists listings that disappeared.
  These files are made in your project folder each time you run it.

Running only part of it
  py -3 collect_prices.py                (test: first 5 stores)
  py -3 collect_prices.py --stores "Many Realms,Flipside"
  py -3 collect_prices.py --rebuild      (rebuild from saved files; no downloading)
