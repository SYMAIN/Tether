# Deploying Tether to Oracle Cloud (Always Free)

Moves Tether off the Windows host onto an always-on free VM for the long-term
ledger experiment. Oracle's Always Free tier (not a trial) is the only major
free option that stays up 24/7 without spinning down on idle.

## 1. Provision the VM

Oracle Console → Compute → Instances → Create Instance.

- **Shape:** `VM.Standard.E2.1.Micro` (AMD, 1/8 OCPU, 1GB RAM). Not the
  Ampere A1 shape — A1 is more capacity (4 OCPU/24GB free) but frequently
  fails to provision ("Out of host capacity") in most regions right now.
  E2.1.Micro is small but discord.py + APScheduler + SQLite is I/O-bound,
  not compute-bound — plenty for this workload. You get two of these free;
  keep the second in reserve.
- **Image:** Ubuntu 24.04 minimal.
- **Networking:** default VCN is fine. In the subnet's security list, only
  open ingress on **22 (SSH)** — restrict the source CIDR to your current
  IP if it's static, otherwise leave 0.0.0.0/0 but rely on key-only auth.
  No other inbound ports needed: Tether is fully outbound (Discord gateway,
  Gemini API, Google Calendar API, git pull).
- **SSH key:** generate a fresh keypair for this VM, add the public key at
  creation time.

## 2. Base VM setup

SSH in, then:

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
# log out/in for the group change to apply
sudo systemctl enable --now docker
```

Harden SSH (optional but recommended since the VM has a public IP):

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

## 3. Clone the repos

```bash
mkdir -p /opt/tether && cd /opt/tether
git clone <tether-repo-url> app
```

Vault repo — sparse-checkout just the Belki folder so the rest of your
personal vault isn't sitting on a third-party disk:

```bash
git clone --filter=blob:none --sparse <vault-repo-url> vault
cd vault
git sparse-checkout set _belki_files
cd ..
```

Use a **read-only deploy key** for both clones rather than a personal
access token — scoped to exactly what this VM needs.

## 4. Keep the vault current

Belki sync inside Tether runs every 30 min; the VM's local vault clone
needs to be at least that fresh. Cron:

```bash
crontab -e
# add:
*/15 * * * * cd /opt/tether/vault && git pull --quiet
```

## 5. Secrets

These never go through git. Copy from the Windows box via `scp`:

```bash
# from X:\Dev\Tether on Windows (PowerShell / Git Bash):
scp .env credentials.json token.json user@<vm-ip>:/opt/tether/app/
```

`token.json` is the already-authorized OAuth token — reuse it rather than
running the browser consent flow on a headless VM. It's volume-mounted, so
the container picks it up without a rebuild.

**Before cutover:** flip the Google Cloud OAuth consent screen from
Testing → Production (Google Cloud Console → APIs & Services → OAuth
consent screen). Testing-status refresh tokens expire after 7 days; on a
box you're not manually restarting/re-authing daily, that will silently
kill Calendar access. Single-user, Calendar-only scope — no formal review
required for Production.

## 6. Point the Belki mount at the VM's vault clone

In `/opt/tether/app/.env` on the VM, add:

```env
BELKI_HOST_PATH=/opt/tether/vault/_belki_files
```

`docker-compose.yml` already reads this (falls back to the Windows `X:`
drive path if unset, so the Windows dev setup is untouched).

## 7. First run

```bash
cd /opt/tether/app
docker compose up --build -d
docker compose logs -f tether
```

Confirm: Discord connects, startup Belki auto-sync runs, no OAuth errors.

## 8. Cutover

Once the VM copy has run cleanly through at least one nag cycle and one
midnight rollover:

1. Stop the Windows container: `docker-compose down` in `X:\Dev\Tether`.
2. Disable the Windows Task Scheduler entry that runs `start.bat`.
3. VM is now the sole running instance (`restart: always` in compose
   handles reboots/crashes).

## 9. Ongoing deploys

No CI — deploy by hand:

```bash
ssh user@<vm-ip>
cd /opt/tether/app && git pull
docker compose up --build -d
```

## Notes

- `docker-compose.yml`'s hardcoded DNS (`8.8.8.8`/`1.1.1.1`) was a fix for
  Docker's embedded DNS breaking when the Windows host switched to mobile
  tethering. Harmless on the VM (static network), left as-is.
- `data/ledger.db` and `tether.log` are bind-mounted from `/opt/tether/app`
  on the VM — back them up (`scp` or a cron'd copy) before any destructive
  VM operation, same as you would on Windows.
