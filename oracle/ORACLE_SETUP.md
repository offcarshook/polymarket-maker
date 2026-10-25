# Paper market-maker on Oracle Cloud (free, 24/7)

PAPER MODE ONLY. No wallet, no private key, no real orders. The bot service refuses
to start if `PK` is ever filled in `.env`.

Total time: ~15–20 minutes. Oracle's menus get renamed sometimes; if a label
doesn't match exactly, look for the closest one.

---

## 1. Create the Oracle account
1. Go to **oracle.com/cloud/free** → *Start for free*.
2. Fill in the details. A **card is required for verification**; Always Free resources don't charge it.
3. **Home region**: pick one close to you. **You can't change it later.**
   Popular regions often run out of free ARM capacity, so a less popular one can be easier.
4. Check Polymarket's terms for your country/region. This test only *reads* public market data.

## 2. Create the free server
1. Console → **☰ → Compute → Instances → Create instance**.
2. **Image**: *Canonical Ubuntu* (24.04 or 22.04).
3. **Shape** → *Change shape* → **Ampere** → `VM.Standard.A1.Flex`, **1–2 OCPU, 6–12 GB RAM**
   (inside the Always Free allowance). Avoid the 1 GB AMD micro shape: too small for ~2000 markets.
4. **Networking**: keep the defaults (it gets a public IP).
5. **SSH keys**: *Generate a key pair for me* → **Download private key** (keep it safe, it's the login).
6. **Create**. If you see **"Out of capacity"**, retry later or pick another availability domain. That's normal.
7. When it's *Running*, copy the **Public IP address**.

## 3. Log in to the server
On your computer's terminal (Windows: PowerShell; Mac: Terminal):
```bash
ssh -i path/to/your-private.key ubuntu@YOUR_PUBLIC_IP
```
- Mac/Linux: if it complains about permissions, run `chmod 400 path/to/your-private.key` first.
- No terminal? Use Oracle's **Cloud Shell** (the `>_` icon in the console) and upload the key there.

## 4. Install everything (3 commands)
```bash
git clone https://github.com/offcarshook/polymarket-maker
cd polymarket-maker && git checkout claude/magical-ritchie-b6wz2p
bash oracle/setup.sh
```
It takes ~3–5 minutes: installs tools, downloads the bot, applies our fixes, scans ~11k
markets, picks the eligible ones (~2000+), and starts everything as auto-restart services.

## 5. Check it's alive
```bash
systemctl status 'polymaker-*' --no-pager | grep -E '●|Active'
journalctl -u polymaker-paper -f        # live bot log (Ctrl+C to exit)
tail -f ~/poly-maker/my_tools/fills_live.csv
```

## 6. Open the dashboard on your phone / laptop
The dashboard only listens on the server itself (`127.0.0.1:8080`): it is **not** on the open internet.
Pick one way in:

**A) Tailscale (best, works on phone)**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up            # open the printed link, log in (free account)
sudo tailscale serve --bg 8080
```
`tailscale serve` prints a private `https://...ts.net` link. Install the **Tailscale app** on your
phone/laptop, log in with the same account, and open that link. Only your devices can see it.

**B) SSH tunnel (laptop, no extra apps)**
```bash
ssh -i path/to/your-private.key -L 8080:127.0.0.1:8080 ubuntu@YOUR_PUBLIC_IP
```
Leave that window open and visit http://localhost:8080.

Don't open port 8080 in Oracle's security list / firewall.

## 7. Everyday commands
| Do | Command |
|---|---|
| Status | `systemctl status 'polymaker-*' --no-pager` |
| Bot log | `journalctl -u polymaker-paper -f` |
| Restart bot | `sudo systemctl restart polymaker-paper` |
| Stop everything | `sudo systemctl stop 'polymaker-*'` |
| Start everything | `sudo systemctl start polymaker-paper polymaker-markout polymaker-dashboard polymaker-web polymaker-journal-guard` |
| Refresh markets now | `sudo systemctl start polymaker-refresh` (also runs every 6h by itself) |
| Update after I change the kit | `cd ~/polymarket-maker && git pull && bash oracle/setup.sh` |

## 8. Where the results are (`~/poly-maker/`)
| What | File |
|---|---|
| Fills the moment they happen | `my_tools/fills_live.csv` |
| Fills + 10s/1m/5m markouts | `my_tools/markout.csv` |
| Only the big losers (≥ $1 after 5 min) | `my_tools/big_losses.csv` |
| Dashboard page | `web/index.html` |
| Bot log | `logs/paper.jsonl` |

Download a CSV to your laptop:
`scp -i path/to/your-private.key ubuntu@YOUR_PUBLIC_IP:poly-maker/my_tools/markout.csv .`

## Notes
- **Idle reclaim:** Oracle can reclaim Always Free instances it considers idle. A busy bot
  usually isn't, but if it gets stopped, upgrading the account to *Pay As You Go* (Always Free
  resources stay free) is the usual fix. Check Oracle's current policy.
- **Disk:** the raw journal grows ~3 GB/hour; `polymaker-journal-guard` caps it at 1 GB.
- **Strategy:** `config/strategy.toml` profile `paper-aggressive` (1-tick quotes, $500 × 3 layers,
  fake $10k cap in `config/config.toml`). Paper only. Never use these settings with real money.
