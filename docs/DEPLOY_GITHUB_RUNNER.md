# Deploying via GitHub Actions runners

This repo ships two install pipelines. Both run `scripts/install_remote.sh`,
which is idempotent (clone/pull → venv → `pip install -e .` → systemd unit)
and **never** starts the trading service unless you explicitly ask, and never
creates or edits `.env` on the server.

| | Option A — SSH from a GitHub-hosted runner | Option B — self-hosted runner on the server |
|---|---|---|
| Server reachable inbound? | Yes — sshd on port 22 (or custom) must accept GitHub's IPs | No inbound ports needed — the runner polls GitHub outbound |
| Firewalled / NAT / no public IP | ✗ | ✓ |
| Secrets stored | in GitHub (Actions secrets) | none (the job runs on the box itself) |
| Repo must be private | strongly recommended | **mandatory** (see below) |
| Best for | a VPS with SSH open | anything private; or Windows hosts |

Neither option installs broker credentials — you do that yourself on the
server (`.env`). Neither flips the engine live: `deploy/apex.service` runs
`--live`, so the workflow deliberately leaves the unit **stopped and disabled
from starting** unless you pass `start_service: true`, and the demo-validation
gates in `docs/LIVE_DEPLOYMENT.md` still apply.

> **Repo visibility.** This repository is currently **public**, which means
> every workflow log is world-readable and a self-hosted runner would be
> exposed to anyone's fork/PR. Make it private before using either option:
> `Settings → General → Danger Zone → Change visibility → Private`.
> The steps below assume you did.

---

## Option A — SSH deploy (GitHub-hosted runner)

### 1. Server prep (once, on the server)

```bash
# Debian/Ubuntu — the installer also does this if your ssh user has sudo
sudo apt-get update && sudo apt-get install -y git python3 python3-venv python3-pip
sudo mkdir -p /opt/abutron-quant && sudo chown "$USER" /opt/abutron-quant
```

The SSH user must be `root` or have **passwordless sudo** (the installer
writes `/etc/systemd/system/apex.service`).

### 2. Dedicated deploy key (on your machine)

```bash
ssh-keygen -t ed25519 -C apex-deploy -f apex_deploy_ed25519 -N ""
ssh-copy-id -i apex_deploy_ed25519.pub <APEX_USER>@<server>
```

(Or append `apex_deploy_ed25519.pub` to `~/.ssh/authorized_keys` by hand.)
Only the public key goes on the server; the private key goes to GitHub.

### 3. GitHub secrets

`Settings → Secrets and variables → Actions → New repository secret` for each:

| Secret | Value |
|---|---|
| `APEX_HOST` | server hostname or IP |
| `APEX_USER` | ssh user |
| `APEX_SSH_KEY` | **private** key contents (`apex_deploy_ed25519`) |
| `APEX_PORT` | *(optional)* ssh port, default 22 |
| `APEX_KNOWN_HOSTS` | *(optional)* output of `ssh-keyscan -H <host>` for strict host-key pinning; if unset, the workflow does first-use `ssh-keyscan` (TOFU) |

### 4. Run it

* From the web UI: **Actions → deploy → Run workflow**, pick the ref
  (default `main`), leave `start_service` **off**.
* Or with the CLI:

```bash
gh workflow run deploy.yml -f ref=main -f install_dir=/opt/abutron-quant
gh run list --workflow=deploy.yml --limit 1
gh run watch <run-id>
```

Auto-install on merges to `main` is already wired (`push: [main]`) — it
installs but never restarts the service.

### 5. After the install succeeds

```bash
# on the server
cp /opt/abutron-quant/.env.example /opt/abutron-quant/.env && chmod 600 /opt/abutron-quant/.env
$EDITOR /opt/abutron-quant/.env        # DERIV_TOKEN etc. — see docs/LIVE_DEPLOYMENT.md
/opt/abutron-quant/.venv/bin/python -m apex doctor   # expect green for your broker
sudo systemctl start apex              # WARNING: the unit ships with --live
```

---

## Option B — self-hosted runner on the server (no inbound access)

### 1. Make the repo private (mandatory), then register a runner

`Settings → Actions → Runners → New self-hosted runner` → choose Linux
(x64) or Windows → run the printed commands on the server, e.g. Linux:

```bash
mkdir actions-runner && cd actions-runner
./config.sh --url https://github.com/abutronmall-netizen/ABUTRON-QUANT \
            --token <token-from-the-page> --labels abutron --unattended
sudo ./svc.sh install <svc-user>   # installs as a service
sudo ./svc.sh start
```

The token is minted on that page (it expires in ~1 hour and is single-use).
Consider `--disableupdate` only if you patch it yourself; otherwise keep
auto-update on.

Give the service user **passwordless sudo** (or run the runner as root, at
your risk):

```
<svc-user> ALL=(ALL) NOPASSWD: /usr/bin/apt-get, /usr/bin/cp, /usr/bin/systemctl, /usr/bin/mkdir, /usr/bin/chown
```

(or all-commands `NOPASSWD: ALL` if you accept the broader blast radius —
the runner can execute any workflow someone with write access dispatches.)

### 2. Run it

**Actions → deploy-self-hosted → Run workflow** — or:

```bash
gh workflow run deploy-self-hosted.yml -f ref=main -f install_dir=/opt/abutron-quant
```

The job runs `scripts/install_remote.sh` directly on the box. No SSH, no
inbound firewall rules, no SSH secrets.

### Windows (FP Markets MT5)

The same flow works with a self-hosted **Windows** runner, but note: MT5
needs a logged-in terminal and the `MetaTrader5` Python package on that
host, and the runner itself installs fine — trade execution is a separate
concern handled by the terminal, not by Actions.

---

## What the installer guarantees

* Idempotent: re-running is safe; `runs/` (the journal and risk state) and
  `.env` are never touched — no `git clean`, no force-overwrite of local files.
* Installs `deploy/apex.service` (and enables it for boot) but leaves it
  **stopped** unless `start_service: true` was explicitly passed.
* Fails fast with a clear message if the server lacks root/sudo, git/python3,
  or Python ≥ 3.10.
* Prints `apex doctor` output at the end for situational awareness.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Preflight — Missing repository secret APEX_*` | the secrets aren't set (or the integration you use can't see them); add them in Settings |
| `Permission denied (publickey)` | wrong key in `APEX_SSH_KEY`, or pubkey not in `authorized_keys` |
| `Host key verification failed` | unset or stale `APEX_KNOWN_HOSTS`; refresh with `ssh-keyscan -H <host>` |
| `need root or passwordless sudo` | give the ssh user sudo, or use Option B |
| doctor red on Deriv | `.env` missing/invalid token — expected until you fill it |
| workflow not triggerable | `workflow_dispatch` requires the workflow to exist on the **default branch**; merge the PR or trigger from a pushed branch |
