"""Send sliced .3mf files to a Bambu Lab P1S over the local network.

Usage:
    python autoprint.py status
    python autoprint.py files
    python autoprint.py upload model.gcode.3mf
    python autoprint.py print model.gcode.3mf [--plate N] [--ams] [--yes]

Printer connection details are read from printer.json (see printer.example.json).
The printer must be in LAN Only + Developer Mode.

This module is also used by server.py.
"""

import argparse
import ftplib
import json
import re
import ssl
import sys
import threading
import uuid
import zipfile
from pathlib import Path

import paho.mqtt.client as mqtt

MQTT_PORT = 8883
FTP_PORT = 990
USERNAME = "bblp"
DEFAULT_URL = "file:///sdcard/{name}"

BUSY_STATES = {"PREPARE", "RUNNING", "PAUSE", "SLICING"}

KNOWN_ERRORS = {
    84033543: "command not authorized - enable LAN Only mode and Developer Mode on the printer",
}


class PrintError(Exception):
    """The printer refused or failed to start a job, or the job file is unusable."""


def ack_failed(ack):
    return bool(ack.get("err_code")) or ack.get("result", "").lower() not in ("", "success")


def insecure_tls_context():
    # The printer uses a self-signed certificate.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def load_config(path):
    path = Path(path)
    if not path.exists():
        sys.exit(f"Config not found: {path}. Copy printer.example.json to printer.json and fill it in.")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("ip", "serial", "access_code"):
        if not cfg.get(key):
            sys.exit(f"Missing '{key}' in {path}")
    return cfg


# --------------------------------------------------------------------------- FTPS


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP over implicit TLS (port 990), as used by Bambu printers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        # Wrap the control connection in TLS as soon as it is opened.
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        # The printer requires the data channel to reuse the control channel's TLS session.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=65536, callback=None, rest=None):
        # Same as ftplib's, minus conn.unwrap(): the printer never answers
        # the TLS close_notify, so the stock implementation hangs.
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while buf := fp.read(blocksize):
                conn.sendall(buf)
                if callback:
                    callback(buf)
        return self.voidresp()


def ftp_connect(cfg):
    ftp = ImplicitFTPS(context=insecure_tls_context(), timeout=30)
    ftp.connect(cfg["ip"], FTP_PORT)
    ftp.login(USERNAME, cfg["access_code"])
    ftp.prot_p()
    return ftp


def upload(cfg, local_path, remote_name, log=print):
    size = local_path.stat().st_size
    sent = 0
    last_pct = -1

    def progress(buf):
        nonlocal sent, last_pct
        sent += len(buf)
        pct = sent * 100 // size
        if pct != last_pct and pct % 10 == 0:
            log(f"  {pct}%")
            last_pct = pct

    log(f"Uploading {local_path.name} -> {remote_name} ({size / 1_048_576:.1f} MB)")
    ftp = ftp_connect(cfg)
    try:
        with local_path.open("rb") as fp:
            ftp.storbinary(f"STOR {remote_name}", fp, callback=progress)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    log("Upload complete.")


# --------------------------------------------------------------------------- MQTT


class Printer:
    def __init__(self, cfg, debug=False):
        self.cfg = cfg
        self.debug = debug
        self.state = {}
        self.acks = []
        self.cond = threading.Condition()
        self.seq = 0
        # Called as fn(old_gcode_state, new_gcode_state) from the MQTT thread.
        self.on_state_change = None
        self.report_topic = f"device/{cfg['serial']}/report"
        self.request_topic = f"device/{cfg['serial']}/request"

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"autoprint-{uuid.uuid4().hex[:8]}",
        )
        self.client.username_pw_set(USERNAME, cfg["access_code"])
        self.client.tls_set_context(insecure_tls_context())
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.online = threading.Event()
        self.connect_attempted = threading.Event()
        self.connect_error = None

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.connect_error = str(reason_code)
        else:
            self.connect_error = None
            client.subscribe(self.report_topic)
            self.online.set()
            self.send({"pushing": {"sequence_id": self.next_seq(), "command": "pushall"}})
        self.connect_attempted.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.online.clear()

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        if self.debug:
            text = json.dumps(data)
            print(f"[mqtt] {text[:1500]}{'...' if len(text) > 1500 else ''}", flush=True)
        report = data.get("print")
        if not isinstance(report, dict):
            return
        with self.cond:
            old = self.state.get("gcode_state")
            # P1 series sends partial updates, so merge them into one view.
            self.state.update(report)
            new = self.state.get("gcode_state")
            if report.get("command") == "project_file":
                self.acks.append(report)
            self.cond.notify_all()
        if self.on_state_change and new != old:
            self.on_state_change(old, new)

    def connect(self, timeout=10):
        """Connect once and fail fast; for command-line use."""
        self.client.connect(self.cfg["ip"], MQTT_PORT, keepalive=60)
        self.client.loop_start()
        if not self.connect_attempted.wait(timeout):
            raise TimeoutError(f"No MQTT response from {self.cfg['ip']}:{MQTT_PORT}")
        if self.connect_error:
            raise ConnectionError(f"MQTT connection refused: {self.connect_error} (check access code)")

    def start(self):
        """Connect in the background and keep reconnecting; for long-running use."""
        self.client.reconnect_delay_set(1, 60)
        self.client.connect_async(self.cfg["ip"], MQTT_PORT, keepalive=60)
        self.client.loop_start()

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()

    def next_seq(self):
        self.seq += 1
        return str(self.seq)

    def send(self, payload):
        if self.debug:
            print(f"[send] {json.dumps(payload)}", flush=True)
        self.client.publish(self.request_topic, json.dumps(payload))

    def snapshot(self):
        with self.cond:
            return dict(self.state)

    def wait_for(self, predicate, timeout):
        with self.cond:
            return self.cond.wait_for(lambda: predicate(self.state, self.acks), timeout)

    def wait_state(self, timeout=10):
        if not self.wait_for(lambda s, a: "gcode_state" in s, timeout):
            raise TimeoutError("Printer did not report its status")
        return self.snapshot()

    def start_print(self, remote_name, plate, use_ams, ams_mapping, bed_leveling, timelapse, url):
        self.send({
            "print": {
                "sequence_id": self.next_seq(),
                "command": "project_file",
                "param": f"Metadata/plate_{plate}.gcode",
                "project_id": "0",
                "profile_id": "0",
                "task_id": "0",
                "subtask_id": "0",
                "subtask_name": remote_name.removesuffix(".3mf").removesuffix(".gcode"),
                "url": url,
                "md5": "",
                "timelapse": timelapse,
                "bed_type": "auto",
                "bed_levelling": bed_leveling,
                "flow_cali": False,
                "vibration_cali": False,
                "layer_inspect": False,
                "use_ams": use_ams,
                "ams_mapping": ams_mapping,
            }
        })


def format_status(s):
    lines = [f"State:    {s.get('gcode_state', '?')}"]
    if s.get("subtask_name"):
        lines.append(f"Job:      {s['subtask_name']}")
    if s.get("gcode_state") in BUSY_STATES:
        lines.append(f"Progress: {s.get('mc_percent', '?')}%  "
                     f"layer {s.get('layer_num', '?')}/{s.get('total_layer_num', '?')}  "
                     f"{s.get('mc_remaining_time', '?')} min left")
    lines.append(f"Nozzle:   {s.get('nozzle_temper', '?')} / {s.get('nozzle_target_temper', '?')} C")
    lines.append(f"Bed:      {s.get('bed_temper', '?')} / {s.get('bed_target_temper', '?')} C")
    if err := s.get("print_error"):
        lines.append(f"Error:    {err >> 16:04X}_{err & 0xFFFF:04X}")
    for h in s.get("hms") or []:
        attr, code = h.get("attr", 0), h.get("code", 0)
        lines.append(f"HMS:      HMS_{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 3MF


def inspect_3mf(path):
    """Return {plate_number: [filament ids]} for the sliced plates in a .3mf."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        plates = sorted(int(m.group(1)) for n in names
                        if (m := re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n)))
        slice_info = zf.read("Metadata/slice_info.config").decode("utf-8", "replace") \
            if "Metadata/slice_info.config" in names else ""

    filaments = {}
    for block in re.findall(r"<plate>(.*?)</plate>", slice_info, re.S):
        idx = re.search(r'key="index"\s+value="(\d+)"', block)
        if idx:
            filaments[int(idx.group(1))] = [int(i) for i in re.findall(r'<filament id="(\d+)"', block)]
    return {p: filaments.get(p, [1]) for p in plates}


def remote_name_for(local_path):
    # Spaces and unusual characters break the file:// URL on the printer.
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(local_path).name)
    return name if name.endswith(".3mf") else name + ".3mf"


def parse_ams_mapping(text):
    """'0,2' -> [0, 2]; None/'' -> None."""
    if not text:
        return None
    try:
        return [int(x) for x in text.split(",")]
    except ValueError:
        raise PrintError(f"Invalid AMS mapping '{text}', expected e.g. 0,2")


def prepare_job(path, plate=None, use_ams=False, ams_mapping=None):
    """Check that a .3mf is sliced and work out (plate, ams_mapping) for it."""
    try:
        plates = inspect_3mf(path)
    except zipfile.BadZipFile:
        raise PrintError(f"{Path(path).name} is not a valid .3mf file")
    if not plates:
        raise PrintError(f"{Path(path).name} contains no sliced G-code. In Bambu Studio, slice the "
                         "plate and use 'Export plate sliced file' (.gcode.3mf).")

    if plate is None:
        if len(plates) > 1:
            raise PrintError(f"File has several sliced plates {sorted(plates)}; choose one with --plate")
        plate = next(iter(plates))
    elif plate not in plates:
        raise PrintError(f"Plate {plate} is not sliced in this file. Sliced plates: {sorted(plates)}")

    if not use_ams:
        return plate, [0]
    if ams_mapping:
        return plate, list(ams_mapping)
    # Filament N in the file -> AMS slot N-1; unused filaments get -1.
    used = plates[plate]
    return plate, [i - 1 if i in used else -1 for i in range(1, max(used) + 1)]


def run_print(printer, cfg, path, plate, use_ams, ams_mapping, bed_leveling=True,
              timelapse=False, url_template=DEFAULT_URL, skip_upload=False, log=print):
    """Upload a sliced .3mf and start it on a connected Printer.

    Raises PrintError if the printer is busy or rejects the job, and
    OSError/TimeoutError if it can't be reached.
    """
    if not printer.online.is_set():
        raise ConnectionError("Printer is offline")
    state = printer.wait_state()
    if state.get("gcode_state") in BUSY_STATES:
        raise PrintError(f"Printer is busy ({state['gcode_state']})")

    remote_name = remote_name_for(path)
    if not skip_upload:
        upload(cfg, Path(path), remote_name, log)

    url = url_template.format(name=remote_name)
    log(f"Starting {url}, plate {plate}"
        + (f", AMS mapping {ams_mapping}" if use_ams else ", external spool"))
    with printer.cond:
        printer.acks.clear()
    printer.start_print(remote_name, plate, use_ams, ams_mapping,
                        bed_leveling=bed_leveling, timelapse=timelapse, url=url)

    def started_or_failed(s, acks):
        return s.get("gcode_state") in BUSY_STATES or any(map(ack_failed, acks))

    if not printer.wait_for(started_or_failed, timeout=60):
        diag = {k: printer.state.get(k) for k in
                ("gcode_state", "print_error", "mc_print_stage", "mc_print_sub_stage",
                 "gcode_file", "subtask_name", "hms", "sdcard")}
        raise PrintError(f"Printer did not confirm the job within 60 s. "
                         f"Replies: {printer.acks or 'none'}. State: {json.dumps(diag)}")

    failed = [a for a in printer.acks if ack_failed(a)]
    if failed:
        err = failed[-1].get("err_code")
        hint = KNOWN_ERRORS.get(err, failed[-1].get("reason") or failed[-1].get("result"))
        raise PrintError(f"Printer rejected the job (err_code {err}): {hint}")
    return printer.snapshot()


# --------------------------------------------------------------------------- commands


def cmd_status(cfg, args):
    printer = Printer(cfg, debug=args.debug)
    printer.connect()
    try:
        print(format_status(printer.wait_state()))
    finally:
        printer.close()


def cmd_files(cfg, args):
    ftp = ftp_connect(cfg)
    try:
        for name in sorted(ftp.nlst()):
            print(name)
    finally:
        ftp.quit()


def cmd_upload(cfg, args):
    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"File not found: {path}")
    upload(cfg, path, remote_name_for(path))


def cmd_print(cfg, args):
    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"File not found: {path}")
    plate, ams_mapping = prepare_job(path, args.plate, args.ams, parse_ams_mapping(args.ams_mapping))

    printer = Printer(cfg, debug=args.debug)
    printer.connect()
    try:
        state = printer.wait_state()
        print(format_status(state))
        if state.get("gcode_state") in BUSY_STATES:
            sys.exit("Printer is busy; not starting a new job.")

        if not args.yes:
            answer = input("\nIs the build plate empty and ready? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                sys.exit("Cancelled.")

        state = run_print(printer, cfg, path, plate, args.ams, ams_mapping,
                          bed_leveling=not args.no_level, timelapse=args.timelapse,
                          url_template=args.url, skip_upload=args.skip_upload)
        print(f"Print started (state: {state.get('gcode_state')}).")
    finally:
        printer.close()


def main():
    parser = argparse.ArgumentParser(description="Send prints to a Bambu Lab P1S over LAN.")
    parser.add_argument("--config", default=Path(__file__).with_name("printer.json"),
                        help="path to printer.json (default: next to this script)")
    parser.add_argument("--debug", action="store_true", help="print raw MQTT traffic")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show printer state")
    sub.add_parser("files", help="list files on the printer's SD card")

    p = sub.add_parser("upload", help="upload a .3mf without printing")
    p.add_argument("file")

    p = sub.add_parser("print", help="upload a sliced .3mf and start printing")
    p.add_argument("file", help="sliced .gcode.3mf exported from Bambu Studio / OrcaSlicer")
    p.add_argument("--plate", type=int, help="plate number (default: the only sliced plate)")
    p.add_argument("--ams", action="store_true", help="feed filament from the AMS")
    p.add_argument("--ams-mapping", help="comma-separated AMS slot per filament, e.g. 0,2 (default: 1:1)")
    p.add_argument("--no-level", action="store_true", help="skip bed leveling")
    p.add_argument("--timelapse", action="store_true", help="record a timelapse")
    p.add_argument("--skip-upload", action="store_true", help="file is already on the SD card")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask whether the plate is clear")
    p.add_argument("--url", default=DEFAULT_URL,
                   help="file URL sent to the printer; {name} is the uploaded file name "
                        "(default: file:///sdcard/{name}; alternatives: ftp:///{name}, ftp://{name})")

    args = parser.parse_args()
    cfg = load_config(args.config)
    commands = {"status": cmd_status, "files": cmd_files, "upload": cmd_upload, "print": cmd_print}
    try:
        commands[args.command](cfg, args)
    except (PrintError, OSError, ftplib.Error) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
