# Zvonok lead relay

Internal service that receives only the positive Zvonok campaign branch (`CRM (Webhook)`) and posts lead cards to a private Telegram group every 15 minutes.

Each card contains the phone number and, when Zvonok supplies it, a **Запись звонка** button. Duplicate deliveries are prevented by `call_id`.

## Why a webhook instead of statistics polling

Zvonok's documented call-detailing API returns phone numbers and recording URLs, but not the scenario action (such as pressing `1`). It therefore cannot reliably distinguish a lead from a refusal. Configure the webhook only in the positive campaign branch; the service then receives only leads.

## Configuration

Copy `.env.example` to `/etc/zvonok-lead-relay.env` on the server and set the real values there. Keep that file readable only by root and the `zvonok` service account.

The Zvonok webhook URL will be:

```
https://YOUR_DOMAIN/zvonok/YOUR_WEBHOOK_SECRET
```

The app accepts JSON and form-encoded webhooks. Its health endpoint is `GET /health`.

## Local test

```sh
cp .env.example .env
set -a; . ./.env; set +a
python3 -m unittest -v
python3 app.py
```

## Docker deployment

The included `docker-compose.yml` joins the existing `price-master_default`
network so that Caddy can reach the `relay` container. Add this route before
Caddy's catch-all route:

```caddy
handle_path /zvonok-lead-relay/* {
  reverse_proxy relay:8080
}
```

With `WEBHOOK_SECRET=example`, the final URL is:

```
https://api.pricemasterapp.ru/zvonok-lead-relay/zvonok/example
```

Deploy it with:

```sh
git clone git@github.com:dashkpnknd/zvonok-lead-relay.git /opt/zvonok-lead-relay
cd /opt/zvonok-lead-relay
docker compose up -d --build
```

## systemd alternative

```sh
sudo useradd --system --home /opt/zvonok-lead-relay --shell /usr/sbin/nologin zvonok
sudo mkdir -p /opt/zvonok-lead-relay
sudo cp app.py /opt/zvonok-lead-relay/
sudo cp zvonok-lead-relay.service /etc/systemd/system/
sudo install -m 640 -o root -g zvonok /path/to/env /etc/zvonok-lead-relay.env
sudo systemctl daemon-reload
sudo systemctl enable --now zvonok-lead-relay
```

Put a reverse proxy with a valid TLS certificate in front of port 8080 before adding the Zvonok webhook URL.
