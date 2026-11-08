# vinpriser

Dagligt snapshot (kl. 06:15) af Bourgogne-tilbud hos Supervin, Jysk Vin og Uhrskov,
taget af en Raspberry Pi og skubbet hertil. Claude læser `snapshots/<dato>/` ved de planlagte tjek
(singles day, black week, black friday).

- `scrape.py` – henter siderne i headless Chromium og gemmer JSON
- `run.sh` – kører scraperen, committer og pusher (kaldes af cron)
- `setup.sh` – engangsopsætning på Pi'en (venv, Chromium, cron)

Fjern cron-linjen igen med `crontab -e`, når perioden er slut.
