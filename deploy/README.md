# Bancostore Production Deploy Runbook

Manual deploy process for the Hostinger VPS, written after Task 24's first real deploy
(2026-08-10). No CI/CD auto-deploy exists yet — every release is a manual SSH session
following the steps below.

## Server facts

- **Host:** `186.240.150.230` (`srv1882501.hstgr.cloud`), Hostinger KVM 2, Ubuntu 24.04 LTS
- **Domain:** `bancostore.com` / `www.bancostore.com`, real Let's Encrypt cert (auto-renews via
  `certbot.timer`)
- **App user:** `bancostore` (non-root, passwordless sudo — the security boundary is SSH key
  possession, not a second sudo password; root login and password auth are both disabled)
- **App directory:** `/home/bancostore/bancostore` (git checkout of `main`)
- **Services (all under Supervisor, configs in this repo's `deploy/supervisor/`):**
  - `bancostore-daphne` — ASGI server, binds `127.0.0.1:8001` only (never public — Nginx is the
    sole internet-facing process)
  - `bancostore-celery-worker` — Celery worker
  - `bancostore-celery-beat` — Celery Beat (schedule lives in MySQL via `django_celery_beat`,
    **must never run more than one instance** — a second Beat process would double-fire every
    periodic commission/withdrawal task)
- **Web server:** Nginx (config in this repo's `deploy/nginx/bancostore.conf`, deployed to
  `/etc/nginx/sites-available/bancostore`) — reverse-proxies to Daphne, serves `/static/`/`/media/`
  directly, sets `X-Real-IP`/`X-Forwarded-Proto` (which `bancostore/settings.py`'s production
  security block trusts)
- **Database:** MySQL 8, database `bancostore`, user `bancostore`@`localhost` (credentials only in
  the server's own `.env`, never committed)
- **Logs:** `/home/bancostore/bancostore/logs/*.log` (Supervisor-managed, stdout/stderr per
  program)

## Deploying a new release

Run these as the `bancostore` user (`ssh bancostore@186.240.150.230`, using the project's own SSH
key — password auth is disabled):

```bash
cd /home/bancostore/bancostore
git pull origin main
source venv/bin/activate
pip install -r requirements.txt        # only if requirements.txt changed
npm install && npm run build           # only if frontend deps/assets changed
python manage.py migrate               # only if new migrations exist
python manage.py collectstatic --noinput
sudo supervisorctl restart bancostore-daphne bancostore-celery-worker bancostore-celery-beat
```

**Always run `python manage.py check` before restarting** — catches settings/import errors before
they take down the live site:

```bash
python manage.py check
```

**Verify after restart:**

```bash
sudo supervisorctl status              # all three RUNNING with fresh pids
curl -I https://bancostore.com/        # 200, not 500
```

## Environment variables (`.env`)

Lives only on the server at `/home/bancostore/bancostore/.env` (`chmod 600`, never committed).
See `.env.example` in the repo root for the full documented list of variables and what production
values should look like. Two settings deserve special care:

- **`MNOTIFY_API_KEY`** — real key, real SMS costs money per send. Never blank this in a live
  session without a clear plan to restore it (see "Temporarily disabling real SMS sends" below).
- **`PAYSTACK_SECRET_KEY`/`PAYSTACK_PUBLIC_KEY`** — currently **TEST mode keys** (deliberate,
  confirmed with the user 2026-08-10). Switching to live keys is a real launch decision, not a
  routine deploy step — see "Going live with real payments" below.

## Restarting after a crash / VPS reboot

Nothing to do manually — Supervisor (`systemctl status supervisor`) and all three programs are
`autostart`/`autorestart`, and Supervisor itself is enabled at boot. Verified live: a full
`sudo reboot` brings all three back with zero manual intervention.

If something looks wrong after a reboot:

```bash
sudo supervisorctl status
sudo systemctl status nginx mysql redis-server supervisor
tail -50 /home/bancostore/bancostore/logs/daphne_error.log
tail -50 /home/bancostore/bancostore/logs/celery_worker_error.log
```

## Temporarily disabling real SMS sends (for safe testing)

Matches the pattern used for Task 24h's own smoke test — safe when you need to exercise a
distributor-facing flow (registration, login, KYC) without spending real mNotify credit:

```bash
cd /home/bancostore/bancostore
cp .env .env.bak-with-real-mnotify-key
sed -i 's/^MNOTIFY_API_KEY=.*/MNOTIFY_API_KEY=/' .env
sudo supervisorctl restart bancostore-daphne bancostore-celery-worker bancostore-celery-beat
```

OTP/notification codes will then print to `logs/daphne.log` or `logs/celery_worker.log` instead of
sending (via `apps/notifications/sms.py`'s fake sender) — **make sure `PYTHONUNBUFFERED=1` is set
in the Supervisor configs** (it is, as of Task 24h) or the print output silently sits in a buffer
and never reaches the log.

**Always restore afterward:**

```bash
mv .env.bak-with-real-mnotify-key .env
sudo supervisorctl restart bancostore-daphne bancostore-celery-worker bancostore-celery-beat
```

## Going live with real payments

Production currently runs Paystack **test mode** keys. Before flipping to live keys:

1. Get live `PAYSTACK_PUBLIC_KEY`/`PAYSTACK_SECRET_KEY` from the Paystack dashboard
2. Update `.env` on the server directly (never commit real keys)
3. Restart: `sudo supervisorctl restart bancostore-daphne bancostore-celery-worker
   bancostore-celery-beat`
4. Do one real, small end-to-end purchase to confirm the live keys actually work before
   announcing launch
5. This is a real business decision — confirm with the platform owner before doing this, same as
   any other "ask first" production action

## Setting up the Didit KYC webhook (deferred until this point)

`DIDIT_WEBHOOK_SECRET` is intentionally blank in production — Didit's webhook needs a real HTTPS
callback URL to register against, which only exists now that Task 24g's HTTPS is live. KYC still
works correctly without it (the callback path independently re-verifies with Didit's API), but the
webhook is a useful backup for distributors who close their browser before the callback redirect
completes. To wire it up:

1. Log into Didit's Business Console (business.didit.me)
2. Add a webhook subscription pointing to `https://bancostore.com/distributors/webhooks/didit/`
3. Copy the `secret_shared_key` Didit shows at creation time (shown once only)
4. Set `DIDIT_WEBHOOK_SECRET` in production `.env`, restart the three Supervisor programs

## Known limitations (not fixed in Task 24, tracked deliberately)

- **Paystack Transfers are blocked at the account-tier level** — this sandbox account's "Starter
  Business" tier blocks real Transfers even in test mode. The Friday withdrawal-payout batch will
  reach the Transfer API call and fail there; everything before that (request, admin approval,
  wallet debit, tax calculation) works correctly. See `project_paystack_transfer_account_tier_blocked`
  memory.
- **`npm audit` reports 2 high-severity findings** in dev dependencies (frontend build tooling,
  not runtime) — flagged during Task 24e, not triaged yet.
- **No CI/CD auto-deploy** — every release goes through this manual runbook. Automating this (e.g.
  a GitHub Actions workflow that SSHes in and runs the steps above on push to `main`) is real
  future work, not started.
