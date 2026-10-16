@echo off
REM Collect prices on this computer, then push the two small data files to GitHub.
REM GitHub then builds the product pages and publishes the site.
cd /d "%~dp0"

echo === Getting the latest from GitHub ===
git pull --rebase --autostash
if errorlevel 1 goto :fail

echo === Collecting prices (this takes a while) ===
py -3 collect_prices.py --all --smart
if errorlevel 1 goto :fail

echo === Sending the new prices to GitHub ===
git add site/data.js data/history.json
git diff --cached --quiet || git commit -m "Update prices %date%"
git push
if errorlevel 1 goto :fail

echo.
echo Done. GitHub will publish the site in a couple of minutes.
pause
exit /b 0

:fail
echo.
echo Something went wrong. Scroll up to read the message above, and copy it if you need help.
pause
exit /b 1
