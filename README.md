# Notification Tracker (Ticket Module First)

Projekt jest budowany jako szersza aplikacja do monitoringu i powiadomień, a obecna implementacja to pierwszy moduł: watcher biletów (domyślnie `Dziady`, `Wesele`).

Architektura jest przygotowana pod kolejne źródła i use-case'y trackerowe (nie tylko bilety).

## Co monitoruje

- `teatr_repertuar`: https://teatrwkrakowie.pl/repertuar  
  wykrywa status `Bilet do teatru` vs `Bilety do teatru wyprzedane`.
- `teatr_ticket_listing`: oficjalna lista terminów `bilety.teatrwkrakowie.pl`  
  wykrywa nowe terminy pojawiające się w systemie.
- `biletomat` (opcjonalnie): wykrywa terminy widoczne na stronie wydarzenia.
- `facebook` (opcjonalnie, Graph API): filtruje posty tylko po słowach kluczowych (np. `Dziady`, `Wesele`).

## Gdzie trafiają alerty

- jeśli ntfy jest skonfigurowane (`NTFY_SERVER` + `NTFY_TOPIC`): alerty idą przez ntfy,
- jeśli ntfy zawiedzie lub nie jest ustawione, watcher próbuje Signal (`SIGNAL_ACCOUNT` + `SIGNAL_RECIPIENTS`),
- jeśli email fallback dla alertów jest włączony (`EMAIL_FALLBACK_ON_TICKET_ALERTS=true`), watcher próbuje email (`SMTP_*` + `EMAIL_FROM` + `EMAIL_TO`),
- jeśli email też nie działa, watcher próbuje Telegram (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`),
- jeśli żaden kanał nie jest skonfigurowany/dostępny: alerty są wypisywane w logu STDOUT (`journalctl`).

## Szybki start (lokalnie)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m watcher run-once --dry-run --print-events
```

Po uzupełnieniu konfiguracji notyfikacji:

```bash
python -m watcher test-ntfy
python -m watcher test-signal
python -m watcher test-email
python -m watcher test-telegram
python -m watcher run-once
```

## Tryby pracy

- `python -m watcher run-once`  
  pojedyncze sprawdzenie + ewentualne alerty.
- `python -m watcher watch`  
  pętla co `CHECK_INTERVAL_SECONDS`.
- `python -m watcher smoke-check`  
  sanity check parserów i źródeł (kod 1 przy błędzie źródła/parsera).
- `python -m watcher test-ntfy`  
  test kanału ntfy.
- `python -m watcher test-signal`  
  test kanału Signal.
- `python -m watcher test-email`  
  test kanału email.

## Facebook (strona i grupa)

Parser FB działa przez oficjalne Graph API, nie przez scrape HTML.  
Skonfiguruj:

- `ENABLE_FACEBOOK=true`
- `FACEBOOK_ACCESS_TOKEN=...`
- `FACEBOOK_PAGE_ID=...` (np. strona teatru)
- `FACEBOOK_GROUP_ID=...` (np. grupa odsprzedażowa)
- `FACEBOOK_KEYWORDS_INCLUDE=Dziady,Wesele`
- opcjonalnie `FACEBOOK_KEYWORDS_EXCLUDE=...`

Uwaga praktyczna:

- bez tokena i uprawnień API nie da się stabilnie czytać treści postów,
- dla grup FB zwykle potrzebne są dodatkowe uprawnienia/aplikacja po stronie Meta (app review).

## ntfy + fallbacki

Konfiguracja minimalna ntfy (publiczny serwer `ntfy.sh`):

- `NTFY_SERVER=https://ntfy.sh`
- `NTFY_TOPIC=<losowy_długi_topic>`
- opcjonalnie: `NTFY_TOKEN` lub `NTFY_USERNAME`/`NTFY_PASSWORD`
- opcjonalnie: `NTFY_PRIORITY_*`, `NTFY_TAGS_*`

Przykładowy test ręczny:

```bash
curl -H "Title: Bilety watcher test" -H "Priority: urgent" -d "test" https://ntfy.sh/TWOJ_TOPIC
```

Signal zostaje jako opcjonalny fallback:

Konfiguracja minimalna Signal:

- `SIGNAL_ACCOUNT=+48...` (konto/numer zarejestrowany w `signal-cli`)
- `SIGNAL_RECIPIENTS=+48...` (jeden lub więcej odbiorców, po przecinku)
- opcjonalnie `SIGNAL_CLI_PATH` i `SIGNAL_TIMEOUT_SECONDS`

Konfiguracja email fallback:

- `SMTP_HOST`, `SMTP_PORT`
- `EMAIL_FROM`, `EMAIL_TO`
- opcjonalnie autoryzacja: `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`
- dla zwykłych alertów biletowych: `EMAIL_FALLBACK_ON_TICKET_ALERTS=true` (domyślnie `false`)

Kolejność wysyłki: `ntfy -> Signal -> (opcjonalnie) Email -> Telegram`.

## Deploy na Ubuntu (systemd timer)

Szybki flow (2 komendy):

1. Lokalnie (push kodu na serwer):

```bash
./deploy/push.sh waysker@orbit /opt/notification-tracker
```

2. Na serwerze (setup venv + dependencies + systemd):

```bash
cd /opt/notification-tracker
./deploy/install.sh --app-user waysker --app-group waysker
```

Skrypty:

- `deploy/push.sh`: tworzy katalog docelowy na serwerze i robi `rsync` (z wykluczeniem `.env`, `.venv`, `data`).
- `deploy/install.sh`: stawia `.venv`, instaluje dependencies, kopiuje unit files, ustawia `User/Group`, reloaduje systemd i odpala timery.

Ręczny fallback (gdy nie chcesz używać skryptów):

```bash
sudo mkdir -p /opt/notification-tracker
sudo chown -R $USER:$USER /opt/notification-tracker
rsync -av --delete ./ /opt/notification-tracker/
cd /opt/notification-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Diagnostyka:

```bash
systemctl list-timers | rg teatr-bilety
journalctl -u teatr-bilety.service -n 200 --no-pager
journalctl -u teatr-bilety-smoke.service -n 200 --no-pager
```

## Uwagi

- Na pierwszym uruchomieniu watcher zapisuje stan bazowy; kolejne uruchomienia zgłaszają tylko zmiany.
- Auto-zakup nie jest zaimplementowany (celowo).
- Źródła mogą zmienić HTML; wtedy parser wymaga drobnej aktualizacji.
