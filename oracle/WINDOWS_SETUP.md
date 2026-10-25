# Paper market-maker on your Windows PC (free, no card, low resources)

PAPER MODE ONLY. No wallet, no private key, no real orders. The bot refuses to start if
`PK` is ever filled in `.env`.

**Lite mode** = 300 hand-filtered markets, bot capped at **½ a CPU core and 700 MB RAM**,
the whole WSL VM capped at 1.5 GB, journal capped at 200 MB, dashboard refresh every 30 s.
Your PC stays usable for everything else.

Time: ~15 minutes. Needs Windows 10 (2004+) or Windows 11.

---

## 1. Install WSL + Ubuntu
Open **PowerShell as Administrator** (Start → type *PowerShell* → right-click → *Run as administrator*):
```powershell
wsl --install -d Ubuntu
```
Restart the PC if it asks. Ubuntu then opens by itself and asks for a **username + password**
(this is just for Ubuntu; remember the password, `sudo` asks for it).

## 2. Turn on systemd (auto-restart services need it)
In the **Ubuntu** window:
```bash
cat /etc/wsl.conf 2>/dev/null
```
If it doesn't show `systemd=true`, run:
```bash
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
```
Then in **PowerShell**: `wsl --shutdown`, and open **Ubuntu** again from the Start menu.

## 3. Cap WSL's resources
In PowerShell (normal, not admin):
```powershell
notepad $env:USERPROFILE\.wslconfig
```
Paste this, save, close:
```
[wsl2]
memory=1536MB
processors=2
swap=1GB
```
Then `wsl --shutdown` and reopen Ubuntu.

## 4. Install the bot (3 commands, in Ubuntu)
```bash
git clone https://github.com/offcarshook/polymarket-maker
cd polymarket-maker && git checkout claude/magical-ritchie-b6wz2p
MODE=lite bash oracle/setup.sh
```
Takes ~3–5 minutes. At the end you should see 5 services `active (running)`.

## 5. Open the dashboard
In your normal Windows browser: **http://localhost:8080**

It's live: it refreshes itself every 30 s with new quotes, fills, markouts and the
biggest-losses table. If the page doesn't load, see *Troubleshooting*.

## 6. Keep it running 24/7
WSL shuts down when no Ubuntu window is open. To keep it alive (and start it at every login):
1. In Ubuntu: `cp ~/polymarket-maker/oracle/windows/keep-wsl-alive.ps1 /mnt/c/Users/Public/`
2. In PowerShell: `powershell -ExecutionPolicy Bypass -File C:\Users\Public\keep-wsl-alive.ps1`

Also set **Settings → System → Power → Sleep → Never** (when plugged in), or the PC's
sleep pauses everything.

After a power cut, the bot comes back by itself once you log in to Windows
(*Settings → Accounts → Sign-in options* can auto-login if you want it fully hands-off).

## 7. Everyday commands (in Ubuntu)
| Do | Command |
|---|---|
| Status | `systemctl status 'polymaker-*' --no-pager \| grep -E '●\|Active'` |
| Bot log | `journalctl -u polymaker-paper -f` |
| Pause everything | `sudo systemctl stop 'polymaker-*'` |
| Start again | `sudo systemctl start polymaker-paper polymaker-markout polymaker-dashboard polymaker-web polymaker-journal-guard` |
| Refresh markets now | `sudo systemctl start polymaker-refresh` (also every 6 h by itself) |
| Go bigger (~2000 markets) | `cd ~/polymarket-maker && bash oracle/setup.sh` |
| Back to lite | `cd ~/polymarket-maker && MODE=lite bash oracle/setup.sh` |
| Update the kit | `cd ~/polymarket-maker && git pull && MODE=lite bash oracle/setup.sh` |
| Turn it all off for good | `sudo systemctl disable --now 'polymaker-*'` + in PowerShell `Unregister-ScheduledTask -TaskName PolymakerWSL -Confirm:$false` |

## 8. Where the results are
In Windows Explorer, type `\\wsl$\Ubuntu\home\<your-ubuntu-username>\poly-maker\my_tools`:
| What | File |
|---|---|
| Fills the moment they happen | `fills_live.csv` |
| Fills + 10s/1m/5m markouts | `markout.csv` |
| Only big losers (≥ $1 after 5 min) | `big_losses.csv` |

Open them straight in Excel.

## Troubleshooting
- **`systemd is not running`** → redo step 2 (`wsl --shutdown` matters).
- **Dashboard doesn't load** → in Ubuntu, `curl -s localhost:8080 | head -3`. If that works but
  Windows doesn't, run `wsl --shutdown` in PowerShell and reopen Ubuntu (localhost forwarding
  sometimes needs a restart).
- **PC feels slow** → lower `processors=1` in `.wslconfig`, or pause with `sudo systemctl stop 'polymaker-*'`.
- **Polymarket unreachable** → `curl -s -o /dev/null -w "%{http_code}\n" https://clob.polymarket.com/time`
  should print `200`. Anything else means your network is blocking it.
