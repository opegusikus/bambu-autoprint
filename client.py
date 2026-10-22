"""Client for the autoprint server. Standard library only, so it runs as is on Windows and Termux.

    python client.py schedule model.gcode.3mf --at 14:00
    python client.py schedule model.gcode.3mf --at "2026-10-21 09:30"
    python client.py schedule model.gcode.3mf --in 2h30m
    python client.py schedule model.gcode.3mf --now
    python client.py jobs
    python client.py status
    python client.py confirm <id>     # plate is clear again
    python client.py cancel <id>
    python client.py light on|off

Server URL and token are read from client.json (see client.example.json).
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path


def load_config(path):
    path = Path(path)
    if not path.exists():
        sys.exit(f"Config not found: {path}. Copy client.example.json to client.json and fill it in.")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("url", "token"):
        if not cfg.get(key):
            sys.exit(f"Missing '{key}' in {path}")
    return cfg


def api(cfg, method, path, query=None, body=None, timeout=60):
    url = cfg["url"].rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": f"Bearer {cfg['token']}"})
    if body is not None:
        req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            message = json.load(e).get("error")
        except ValueError:
            message = e.reason
        sys.exit(f"Server error {e.code}: {message}")
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"Cannot reach server at {cfg['url']}: {getattr(e, 'reason', e)}")


def parse_when(args, now):
    if args.now:
        return now
    if args.in_:
        m = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*", args.in_)
        if not m or not any(m.groups()):
            sys.exit(f"Can't read --in '{args.in_}'; use e.g. 45m, 2h, 2h30m")
        return now + timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    try:
        t = datetime.strptime(args.at, "%H:%M")
    except ValueError:
        try:
            return datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        except ValueError:
            sys.exit(f"Can't read --at '{args.at}'; use HH:MM or 'YYYY-MM-DD HH:MM'")
    t = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")


def fmt_delta(seconds):
    minutes = int(seconds // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def print_job(job):
    print(f"{job['id']}  {fmt_time(job['at'])}  {job['status']:<9}  {job['name']}  (plate {job['plate']})")
    if job["status"] == "pending" and not job.get("confirmed_at"):
        print(f"        plate not confirmed clear - run: confirm {job['id']}")
    if job.get("error") and job["status"] != "started":
        print(f"        {job['error']}")


# --------------------------------------------------------------------------- commands


def cmd_schedule(cfg, args):
    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"File not found: {path}")
    now = datetime.now()
    when = parse_when(args, now)

    if not args.yes:
        prompt = "now" if args.now else f"at {when:%a %H:%M}"
        answer = input(f"Will the build plate be empty {prompt}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            sys.exit("Cancelled.")

    query = {"name": path.name, "at": f"{when.timestamp():.0f}",
             "external": int(args.external), "level": int(not args.no_level), "timelapse": int(args.timelapse),
             "flow_cali": int(args.flow_cali), "vibration_cali": int(args.vibration_cali)}
    if args.plate:
        query["plate"] = args.plate
    if args.ams_mapping:
        query["ams_mapping"] = args.ams_mapping

    job = api(cfg, "POST", "/jobs", query, path.read_bytes())
    print(f"Scheduled job {job['id']}: {job['name']} plate {job['plate']} at {fmt_time(job['at'])}"
          + ("" if args.now else f" (in {fmt_delta(job['at'] - now.timestamp())})"))
    if job.get("filament"):
        print(f"Filament: {job['filament']}")
    if job.get("warning"):
        print(f"Warning: {job['warning']}")


def cmd_jobs(cfg, args):
    jobs = api(cfg, "GET", "/jobs")
    if not args.all:
        jobs = [j for j in jobs if j["status"] in ("pending", "starting")] or jobs[-5:]
    if not jobs:
        print("No jobs.")
    for job in jobs:
        print_job(job)


def cmd_status(cfg, args):
    status = api(cfg, "GET", "/status")
    print(status["text"])
    pending = [j for j in api(cfg, "GET", "/jobs") if j["status"] in ("pending", "starting")]
    if pending:
        print("\nUpcoming:")
        for job in pending:
            print_job(job)


def cmd_confirm(cfg, args):
    job = api(cfg, "POST", f"/jobs/{args.id}/confirm")
    print(f"Plate confirmed clear for job {job['id']} ({job['name']} at {fmt_time(job['at'])}).")


def cmd_light(cfg, args):
    result = api(cfg, "POST", "/light", {"state": args.state})
    print(f"Light {args.state}." if result["confirmed"]
          else f"Sent light {args.state}, but the printer didn't confirm it.")


def cmd_cancel(cfg, args):
    job = api(cfg, "DELETE", f"/jobs/{args.id}")
    print(f"Cancelled job {job['id']} ({job['name']}).")


def main():
    parser = argparse.ArgumentParser(description="Schedule prints on the autoprint server.")
    parser.add_argument("--config", default=Path(__file__).with_name("client.json"),
                        help="path to client.json (default: next to this script)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("schedule", help="upload a sliced .3mf and schedule it")
    p.add_argument("file", help="sliced .gcode.3mf exported from Bambu Studio / OrcaSlicer")
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--at", help="HH:MM (next occurrence) or 'YYYY-MM-DD HH:MM'")
    when.add_argument("--in", dest="in_", help="delay, e.g. 45m, 2h, 2h30m")
    when.add_argument("--now", action="store_true", help="start as soon as possible")
    p.add_argument("--plate", type=int, help="plate number (default: the only sliced plate)")
    p.add_argument("--external", action="store_true",
                   help="use the external spool instead of the AMS")
    p.add_argument("--ams-mapping", help="AMS slot per filament, 0-based (A1=0, A2=1...), e.g. 1,3 "
                                         "(default: match by type and color)")
    p.add_argument("--no-level", action="store_true", help="skip bed leveling")
    p.add_argument("--timelapse", action="store_true", help="record a timelapse")
    p.add_argument("--flow-cali", action="store_true", help="run flow dynamics calibration first")
    p.add_argument("--vibration-cali", action="store_true", help="run vibration calibration first")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask whether the plate will be clear")

    p = sub.add_parser("jobs", help="list upcoming jobs (or recent ones if none are upcoming)")
    p.add_argument("--all", action="store_true", help="show all jobs from the last 14 days")
    sub.add_parser("status", help="printer state and upcoming jobs")
    p = sub.add_parser("confirm", help="mark the plate clear for a pending job")
    p.add_argument("id")
    p = sub.add_parser("cancel", help="cancel a pending job")
    p.add_argument("id")
    p = sub.add_parser("light", help="switch the chamber light")
    p.add_argument("state", choices=["on", "off"])

    args = parser.parse_args()
    cfg = load_config(args.config)
    commands = {"schedule": cmd_schedule, "jobs": cmd_jobs, "status": cmd_status,
                "confirm": cmd_confirm, "cancel": cmd_cancel, "light": cmd_light}
    commands[args.command](cfg, args)


if __name__ == "__main__":
    main()
