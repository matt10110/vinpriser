# vinpriser

Daglig tilbudsvagt for Matt. En Raspberry Pi henter kl. 06:15 tilbud hos Supervin, Jysk Vin,
Uhrskov Vine og Laudrup Vin, fører prishistorik og skubber resultatet hertil. En planlagt
Claude-opgave læser `candidates.json` hver morgen og skriver kun, når der er et reelt kup.

## Hvad overvåges
| Gruppe | Prisloft (1–3 fl.) |
|---|---|
| Bourgogne (Pinot Noir, Chardonnay, Aligoté) | 350 kr. |
| Tysk Riesling | 250 kr. |
| Piemonte/Nebbiolo | 250 kr. |
| Etna | 250 kr. |
| Jura | 250 kr. |
| Bierzo/Galicien (Mencía, Godello m.fl.) | 250 kr. |

Kilder: Supervins vine på tilbud, Jysk Vins `/vintilbud` (druefiltre), Uhrskovs Bourgogne (rød/hvid),
Nebbiolo, tysk hvidvin og outlet, Laudrups tilbudsliste. Adresser, som butikkens robots.txt spærrer,
hentes ikke.

## Hvornår er noget en kandidat (`watch.py`)
- **fald**: prisen ved 1–3 fl. er mindst 15 % under vinens egen tidsvægtede 60-dages pris i butikken
  (kræver 14 dages historik), og under prisloftet.
- **fald_6**: 6-flaskeprisen er mindst 20 % under vinens egen 60-dages 6-pris.
- **ny**: ny vin på listen med mindst 30 % angivet rabat ved 1–3 fl., og mindst 10 point mere end
  butikkens sædvanlige rabat (butikkens ankre tæller ikke). Først efter 7 dages overvågning.
- **ny_6**: ny vin kun med 6-pris: mindst 45 % og 15 point over butikkens sædvane. Højst 8 nye pr. dag.
- Samme vin meldes kun igen, hvis prisen falder yderligere 5 %.
- Fejler en kilde, eller giver den under halvt så mange vine som dagen før, røres dens historik ikke.

## Filer
- `scrape.py` – henter siderne i headless Chromium (med sandbox) og gemmer JSON i `snapshots/<dato>/`
- `watch.py` – prishistorik i `state/` og dagens kandidater i `snapshots/<dato>/candidates.json`
- `run.sh` – kører begge, rydder snapshots ældre end 30 dage ud af mappen (de ligger i git-historikken),
  committer og pusher. Kaldes af cron.
- `setup.sh` – engangsopsætning på Pi'en (venv, Chromium, cron)

Ingen automatisk `git pull` på Pi'en: ny kode køres kun, når den er læst og hentet manuelt.
Stop det hele med `crontab -e` (fjern linjen), slet deploy-nøglen under Settings → Deploy keys
og slet `~/.ssh/vinpriser` på Pi'en.
