"""Print server for a home Linux box: accepts jobs over HTTP and starts them at the scheduled time.

    python server.py [--config server.json] [--printer-config printer.json]

Jobs are sent with client.py. Every request needs "Authorization: Bearer <token>".

Plate safety: a job only starts if the build plate was confirmed clear (when the job
was created, or later with `client.py confirm`) after the most recent print started.
"""

import argparse
import ftplib
import hmac
import json
import logging
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import autoprint

log = logging.getLogger("autoprint")

HERE = Path(__file__).resolve().parent
TICK_SECONDS = 15
MAX_UPLOAD = 200 * 1024 * 1024
KEEP_DAYS = 14
FINAL_STATUSES = {"started", "failed", "missed", "cancelled"}


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- storage


class Store:
    """Jobs and server state, persisted as JSON files under data_dir."""

    def __init__(self, data_dir):
        self.jobs_dir = data_dir / "jobs"
        self.files_dir = data_dir / "files"
        self.state_path = data_dir / "state.json"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

        self.state = {"last_print_start": 0}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))

        self.jobs = {}
        for p in self.jobs_dir.glob("*.json"):
            job = json.loads(p.read_text(encoding="utf-8"))
            if job["status"] == "starting":
                job.update(status="failed", error="server restarted while starting this job; check the printer")
                write_json(p, job)
            self.jobs[job["id"]] = job

    @property
    def last_print_start(self):
        return self.state["last_print_start"]

    def mark_print_started(self, t):
        with self.lock:
            self.state["last_print_start"] = t
            write_json(self.state_path, self.state)

    def new_id(self):
        while (job_id := secrets.token_hex(3)) in self.jobs:
            pass
        return job_id

    def job_file(self, job):
        return self.files_dir / job["id"] / job["name"]

    def save(self, job):
        with self.lock:
            self.jobs[job["id"]] = job
            write_json(self.jobs_dir / f"{job['id']}.json", job)

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ApiError(404, f"No job {job_id}")
            return self.jobs[job_id]

    def list(self):
        with self.lock:
            return sorted((dict(j) for j in self.jobs.values()), key=lambda j: j["at"])

    def cleanup(self, now):
        with self.lock:
            for job in list(self.jobs.values()):
                if job["status"] in FINAL_STATUSES and now - job["at"] > KEEP_DAYS * 86400:
                    shutil.rmtree(self.files_dir / job["id"], ignore_errors=True)
                    (self.jobs_dir / f"{job['id']}.json").unlink(missing_ok=True)
                    del self.jobs[job["id"]]


# --------------------------------------------------------------------------- scheduler


class Scheduler:
    def __init__(self, store, printer, printer_cfg, max_late, light_off_delay=None):
        self.store = store
        self.printer = printer
        self.printer_cfg = printer_cfg
        self.max_late = max_late
        self.light_off_delay = light_off_delay

    def on_state_change(self, old, new):
        log.info("Printer state: %s -> %s", old, new)
        # A resume (PAUSE -> RUNNING) is not a new print; anything else entering
        # PREPARE/RUNNING means something is now on the plate.
        if new in ("PREPARE", "RUNNING") and old not in autoprint.BUSY_STATES:
            self.store.mark_print_started(time.time())
        # Only a successful finish; after FAILED the light stays on for the camera.
        # Runs on a timer thread: set_light waits for MQTT replies, which this thread delivers.
        if new == "FINISH" and old in autoprint.BUSY_STATES and self.light_off_delay is not None:
            threading.Timer(self.light_off_delay, self.light_off_after_finish).start()

    def light_off_after_finish(self):
        if self.printer.snapshot().get("gcode_state") != "FINISH":
            return  # a new print started in the meantime
        if not self.printer.online.is_set():
            log.warning("Print finished, but the printer is offline; light not switched off")
            return
        confirmed = self.printer.set_light(False)
        log.info("Print finished; light off%s", "" if confirmed else " (not confirmed by printer)")

    def plate_confirmed(self, job):
        return bool(job["confirmed_at"]) and job["confirmed_at"] > self.store.last_print_start

    def run_forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            time.sleep(TICK_SECONDS)

    def tick(self):
        now = time.time()
        for job in self.store.list():
            if job["status"] == "pending" and job["at"] <= now:
                self.run_job(job["id"])
        self.store.cleanup(now)

    def finish(self, job, status, error=None):
        job.update(status=status, error=error)
        self.store.save(job)
        log.log(logging.INFO if status == "started" else logging.WARNING,
                "Job %s (%s): %s%s", job["id"], job["name"], status, f" - {error}" if error else "")

    def run_job(self, job_id):
        now = time.time()
        with self.store.lock:
            job = self.store.get(job_id)
            if job["status"] != "pending":
                return
            if now - job["at"] > self.max_late:
                return self.finish(job, "missed", f"could not start within {self.max_late // 60} min "
                                                  f"of the scheduled time"
                                                  + (f" (last error: {job['error']})" if job["error"] else ""))
            if not self.plate_confirmed(job):
                return self.finish(job, "failed", "build plate not confirmed clear since the last print")
            job.update(status="starting", attempts=job["attempts"] + 1)
            self.store.save(job)

        log.info("Job %s (%s): starting", job["id"], job["name"])
        path = self.store.job_file(job)
        try:
            filaments = job.get("filaments") or autoprint.prepare_job(path, job["plate"])[1]
            autoprint.run_print(self.printer, self.printer_cfg, path, job["plate"], filaments,
                                job.get("external", False), job.get("ams_mapping"),
                                bed_leveling=job["bed_leveling"], timelapse=job["timelapse"],
                                flow_cali=job.get("flow_cali", False),
                                vibration_cali=job.get("vibration_cali", False),
                                log=log.info)
        except (autoprint.PrintError, ftplib.error_perm) as e:
            self.finish(job, "failed", str(e))
        except (OSError, ftplib.Error) as e:
            # Printer off or unreachable: keep retrying until max_late runs out.
            job.update(status="pending", error=str(e) or type(e).__name__)
            self.store.save(job)
            log.warning("Job %s: printer not reachable (%s); will retry", job["id"], job["error"])
        else:
            job["started_at"] = time.time()
            self.store.mark_print_started(job["started_at"])
            self.finish(job, "started")


# --------------------------------------------------------------------------- HTTP API


class App:
    def __init__(self, store, printer, token):
        self.store = store
        self.printer = printer
        self.token = token

    def status(self):
        state = self.printer.snapshot()
        online = self.printer.online.is_set()
        return {
            "online": online,
            "text": autoprint.format_status(state) if online and state else "Printer offline",
            "gcode_state": state.get("gcode_state") if online else None,
            "last_print_start": self.store.last_print_start,
            "server_time": time.time(),
        }

    def printer_busy(self):
        return (self.printer.online.is_set()
                and self.printer.snapshot().get("gcode_state") in autoprint.BUSY_STATES)

    def create_job(self, query, body, length):
        name = autoprint.remote_name_for(query.get("name") or "model.3mf")
        try:
            at = float(query["at"])
            plate = int(query["plate"]) if query.get("plate") else None
        except (KeyError, ValueError):
            raise ApiError(400, "'at' (unix time) is required; 'plate' must be a number")
        external = query.get("external") == "1"

        job_id = self.store.new_id()
        job = {"id": job_id, "name": name}
        path = self.store.job_file(job)
        path.parent.mkdir(parents=True)
        with path.open("wb") as f:
            remaining = length
            while remaining:
                chunk = body.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        try:
            if remaining:
                raise autoprint.PrintError("Upload was cut short")
            plate, filaments = autoprint.prepare_job(path, plate)
            ams_mapping = autoprint.parse_ams_mapping(query.get("ams_mapping"))
        except autoprint.PrintError as e:
            shutil.rmtree(path.parent, ignore_errors=True)
            raise ApiError(400, str(e))

        busy = self.printer_busy()
        now = time.time()
        job.update(
            created=now, at=at, plate=plate, filaments=filaments, external=external, ams_mapping=ams_mapping,
            bed_leveling=query.get("level", "1") == "1", timelapse=query.get("timelapse") == "1",
            flow_cali=query.get("flow_cali") == "1", vibration_cali=query.get("vibration_cali") == "1",
            confirmed_at=None if busy else now, status="pending", error=None, attempts=0,
        )
        self.store.save(job)
        log.info("Job %s (%s): scheduled for %s", job_id, name, time.ctime(at))
        result = dict(job)
        warnings = []
        if busy:
            warnings.append("The printer is printing right now, so the plate can't be clear yet. "
                            f"Run `confirm {job_id}` after removing the current print.")
        if self.printer.online.is_set():
            # Slots are matched again at start time; this just flags a problem early.
            try:
                result["filament"] = autoprint.resolve_filament_source(
                    self.printer.snapshot(), filaments, external, ams_mapping)[2]
            except autoprint.PrintError as e:
                warnings.append(f"{e} The job will fail unless this is fixed before it starts.")
        if warnings:
            result["warning"] = "\n".join(warnings)
        return result

    def set_light(self, state):
        if state not in ("on", "off"):
            raise ApiError(400, "state must be 'on' or 'off'")
        if not self.printer.online.is_set():
            raise ApiError(503, "Printer is offline")
        confirmed = self.printer.set_light(state == "on")
        log.info("Light %s%s", state, "" if confirmed else " (not confirmed by printer)")
        return {"light": state, "confirmed": confirmed}

    def confirm_job(self, job_id):
        with self.store.lock:
            job = self.store.get(job_id)
            if job["status"] != "pending":
                raise ApiError(409, f"Job {job_id} is {job['status']}")
            if self.printer_busy():
                raise ApiError(409, "The printer is printing; confirm after removing that print")
            job["confirmed_at"] = time.time()
            self.store.save(job)
            return dict(job)

    def cancel_job(self, job_id):
        with self.store.lock:
            job = self.store.get(job_id)
            if job["status"] != "pending":
                raise ApiError(409, f"Job {job_id} is {job['status']}")
            job.update(status="cancelled")
            self.store.save(job)
            log.info("Job %s (%s): cancelled", job_id, job["name"])
            return dict(job)


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "autoprint/1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def send_json(self, code, body):
        data = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_api(self, method):
        expected = f"Bearer {self.app.token}".encode()
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
            raise ApiError(401, "Bad or missing token")

        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}

        if method == "GET" and parts == ["status"]:
            return self.app.status()
        if method == "GET" and parts == ["jobs"]:
            return self.app.store.list()
        if method == "POST" and parts == ["jobs"]:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_UPLOAD:
                raise ApiError(400, f"Upload must be 1 byte to {MAX_UPLOAD // 1_048_576} MB")
            return self.app.create_job(query, self.rfile, length)
        if method == "POST" and parts == ["light"]:
            return self.app.set_light(query.get("state"))
        if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "confirm":
            return self.app.confirm_job(parts[1])
        if method == "DELETE" and len(parts) == 2 and parts[0] == "jobs":
            return self.app.cancel_job(parts[1])
        raise ApiError(404, f"Unknown endpoint {method} {url.path}")

    def dispatch(self, method):
        try:
            self.send_json(200, self.handle_api(method))
        except ApiError as e:
            self.send_json(e.code, {"error": str(e)})
        except Exception as e:
            log.exception("Request failed")
            self.send_json(500, {"error": str(e)})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")


# --------------------------------------------------------------------------- main


def load_server_config(path):
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"Config not found: {path}. Copy server.example.json to server.json and fill it in.")
    cfg = {"listen": "0.0.0.0", "port": 8765, "data_dir": "data", "max_late_minutes": 60,
           "light_off_after_finish_minutes": 1}
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    if len(cfg.get("token", "")) < 16 or cfg["token"].startswith("CHANGE"):
        raise SystemExit("Set a random 'token' of at least 16 characters in server.json, e.g. from:\n"
                         "  python3 -c \"import secrets; print(secrets.token_urlsafe(24))\"")
    return cfg


def main():
    parser = argparse.ArgumentParser(description="Scheduled printing server for a Bambu Lab printer.")
    parser.add_argument("--config", default=HERE / "server.json")
    parser.add_argument("--printer-config", default=HERE / "printer.json")
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("paho").setLevel(logging.WARNING)

    cfg = load_server_config(args.config)
    printer_cfg = autoprint.load_config(args.printer_config)
    store = Store(HERE / cfg["data_dir"])

    printer = autoprint.Printer(printer_cfg)
    light_off = cfg["light_off_after_finish_minutes"]
    scheduler = Scheduler(store, printer, printer_cfg, max_late=int(cfg["max_late_minutes"] * 60),
                          light_off_delay=None if light_off is None else light_off * 60)
    printer.on_state_change = scheduler.on_state_change
    printer.start()
    threading.Thread(target=scheduler.run_forever, daemon=True).start()

    Handler.app = App(store, printer, cfg["token"])
    httpd = ThreadingHTTPServer((cfg["listen"], cfg["port"]), Handler)
    log.info("Listening on %s:%s, printer %s", cfg["listen"], cfg["port"], printer_cfg["ip"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        printer.close()


if __name__ == "__main__":
    main()
