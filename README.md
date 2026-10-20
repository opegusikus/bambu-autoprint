# autoprint-bambu

Scheduled printing for a Bambu Lab P1S over the local network.

- `autoprint.py`: direct LAN control from any PC (status, upload, print now).
- `server.py`: runs on the home Linux server, holds jobs, starts them at the scheduled time.
- `client.py`: schedules jobs on the server from Windows or Termux (standard library only).

The printer must be in **LAN Only mode** with **Developer Mode** on.

## Server (home Linux box)

Needs Python 3.9+.

```bash
mkdir -p ~/autoprint
# copy autoprint.py server.py requirements.txt autoprint.service
#      printer.json server.example.json into ~/autoprint, then:
cd ~/autoprint
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp server.example.json server.json
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # put this in server.json as "token"
.venv/bin/python server.py                                      # test run, Ctrl+C to stop
```

Run it as a service that starts on boot:

```bash
mkdir -p ~/.config/systemd/user
cp autoprint.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now autoprint
sudo loginctl enable-linger $USER     # keep running when you're logged out
journalctl --user -u autoprint -f     # logs
```

`server.json`:

| key | meaning |
|---|---|
| `listen`, `port` | where the HTTP API listens (default `0.0.0.0:8765`) |
| `token` | shared secret; clients send it as a Bearer token |
| `data_dir` | where jobs and uploaded files are kept (14 days) |
| `max_late_minutes` | if the printer is offline at the scheduled time, keep retrying this long, then mark the job `missed` |

## Client (Windows / Termux)

Copy `client.py` and `client.example.json` (renamed `client.json`, with the server URL and token).
On Termux: `pkg install python`.

```
python client.py schedule model.gcode.3mf --at 14:00          # next 14:00
python client.py schedule model.gcode.3mf --at "2026-10-21 09:30"
python client.py schedule model.gcode.3mf --in 2h30m
python client.py schedule model.gcode.3mf --now
python client.py status                                      # printer + upcoming jobs
python client.py jobs [--all]
python client.py confirm <id>                                # plate is clear again
python client.py cancel <id>
```

`schedule` takes the same `--plate`, `--ams`, `--ams-mapping`, `--no-level`, `--timelapse` options as `autoprint.py print`.

## Plate safety

A job only starts if the build plate was confirmed clear after the most recent print started.
Scheduling a job counts as confirming (you're asked first). So:

- Two jobs scheduled back to back: the second one fails instead of printing onto the first.
- A print started from Bambu Studio after you scheduled a job blocks that job too.
- A job scheduled while the printer is printing starts unconfirmed; run `confirm <id>` once you've cleared the plate.

The server only notices prints while it's running. If it was down during a print, it can't know the plate is occupied.
