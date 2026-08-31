# VM deployment

The application serves both the PWA and API from FastAPI. The durable state is one
SQLite file; no portfolio data or settings are stored by the browser.

## Install

The examples assume Ubuntu, `/opt/risk-sizer` for code, and
`/var/lib/risk-sizer/risk_sizer.db` for durable data.

```bash
sudo useradd --system --home /opt/risk-sizer --shell /usr/sbin/nologin risk-sizer
sudo mkdir -p /opt/risk-sizer /var/lib/risk-sizer /var/backups/risk-sizer
sudo chown -R risk-sizer:risk-sizer /opt/risk-sizer /var/lib/risk-sizer /var/backups/risk-sizer
sudo -u risk-sizer python3 -m venv /opt/risk-sizer/.venv
sudo -u risk-sizer /opt/risk-sizer/.venv/bin/pip install -r /opt/risk-sizer/requirements.txt
sudo -u risk-sizer /opt/risk-sizer/.venv/bin/python /opt/risk-sizer/scripts/init_db.py --db /var/lib/risk-sizer/risk_sizer.db
```

Copy `deploy/risk-sizer.env.example` to `/etc/risk-sizer.env`, restrict it to root,
and replace the API key with a long random value. Copy the systemd unit to
`/etc/systemd/system/risk-sizer.service`, then enable it:

```bash
sudo chmod 600 /etc/risk-sizer.env
sudo systemctl daemon-reload
sudo systemctl enable --now risk-sizer
sudo systemctl status risk-sizer
```

### Direct stop alerts

Generate one VAPID key pair and place its public key, private key, and a contact subject
in the root-readable environment file as `RISK_SIZER_VAPID_PUBLIC_KEY`,
`RISK_SIZER_VAPID_PRIVATE_KEY`, and `RISK_SIZER_VAPID_SUBJECT`. The private key must
never be committed, copied into a browser, or placed in GitHub Actions secrets. It is
needed only by the application server to send Web Push after a finalized stop move.
The browser receives only the public key when the owner enables alerts.

The service intentionally uses one Uvicorn worker. SQLite supports concurrent readers
and serialized writes, but multiple application processes add no value for this
single-user workload. Put Caddy or Nginx in front of `127.0.0.1:8000` and expose only
HTTPS (ports 80/443). Do not expose port 8000 publicly: the API key protects requests,
but HTTPS protects the key while it crosses the network.

When the page and API share the same domain, the browser does not use CORS. If a
separately hosted PWA calls the API, set `RISK_SIZER_CORS_ORIGINS` to its exact HTTPS
origin (comma-separated for multiple origins). CORS is not authentication.

## Daily SQLite backups

The backup script uses SQLite's online backup API, so the result includes committed WAL
transactions and is consistent while the server is running. Install this root crontab:

```cron
0 3 * * * /opt/risk-sizer/.venv/bin/python /opt/risk-sizer/scripts/backup_sqlite.py --db /var/lib/risk-sizer/risk_sizer.db --backup-dir /var/backups/risk-sizer --retention-days 30 >> /var/log/risk-sizer-backup.log 2>&1
```

Periodically copy backups off the VM. A backup on the same disk does not protect against
disk or VM loss. Test restoration before relying on it:

```bash
cp /var/backups/risk-sizer/risk_sizer-YYYYMMDDTHHMMSSZ.db /var/lib/risk-sizer/restored.db
sqlite3 /var/lib/risk-sizer/restored.db 'PRAGMA integrity_check;'
```

## Upgrade and health check

Run `scripts/init_db.py` after pulling a new version; schema creation is idempotent.
Restart the service and verify `https://your-domain/api/health` returns
`{"status":"ok","storage":"sqlite"}`.
