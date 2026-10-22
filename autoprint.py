"""Send sliced .3mf files to a Bambu Lab P1S over the local network.

Usage:
    python autoprint.py status
    python autoprint.py files
    python autoprint.py upload model.gcode.3mf
    python autoprint.py print model.gcode.3mf [--plate N] [--external] [--yes]
    python autoprint.py light on|off

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
        self.ams_reports = 0
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
            # P1 series sends partial updates, so merge them into one view
            # (one level deep, so a partial "ams" update keeps the tray list).
            for key, value in report.items():
                if isinstance(value, dict) and isinstance(self.state.get(key), dict):
                    self.state[key] = {**self.state[key], **value}
                else:
                    self.state[key] = value
            new = self.state.get("gcode_state")
            if report.get("command") == "project_file":
                self.acks.append(report)
            if "ams" in report:
                self.ams_reports += 1
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
        return self.client.publish(self.request_topic, json.dumps(payload))

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

    def pushall(self, timeout=5):
        """Request a full status report (including AMS contents) and wait briefly for it."""
        with self.cond:
            seen = self.ams_reports
        self.send({"pushing": {"sequence_id": self.next_seq(), "command": "pushall"}})
        self.wait_for(lambda s, a: self.ams_reports > seen, timeout)
        return self.wait_state()

    def set_light(self, on, timeout=5):
        """Switch the chamber light; returns True once the printer reports the new state."""
        mode = "on" if on else "off"
        self.send({
            "system": {
                "sequence_id": self.next_seq(),
                "command": "ledctrl",
                "led_node": "chamber_light",
                "led_mode": mode,
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0,
            }
        }).wait_for_publish(timeout)
        return self.wait_for(lambda s, a: light_mode(s) == mode, timeout)

    def start_print(self, remote_name, plate, use_ams, ams_mapping, bed_leveling, timelapse, url,
                    flow_cali=False, vibration_cali=False):
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
                "flow_cali": flow_cali,
                "vibration_cali": vibration_cali,
                "layer_inspect": False,
                "use_ams": use_ams,
                "ams_mapping": ams_mapping,
            }
        })


def light_mode(state):
    for light in state.get("lights_report") or []:
        if light.get("node") == "chamber_light":
            return light.get("mode")
    return None


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
    if light := light_mode(s):
        lines.append(f"Light:    {light}")
    if err := s.get("print_error"):
        lines.append(f"Error:    {err >> 16:04X}_{err & 0xFFFF:04X}")
    for h in s.get("hms") or []:
        attr, code = h.get("attr", 0), h.get("code", 0)
        lines.append(f"HMS:      HMS_{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 3MF


def inspect_3mf(path):
    """Return {plate_number: [{"id", "type", "color"}, ...]} for the sliced plates in a .3mf."""
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
            filaments[int(idx.group(1))] = [
                {"id": int(m.group(1)),
                 "type": (t.group(1).upper() if (t := re.search(r'\btype="([^"]*)"', m.group(0))) else None),
                 "color": (c.group(1).lstrip("#")[:6].upper()
                           if (c := re.search(r'\bcolor="([^"]*)"', m.group(0))) else None)}
                for m in re.finditer(r'<filament id="(\d+)"[^>]*>', block)
            ]
    unknown = [{"id": 1, "type": None, "color": None}]
    return {p: filaments.get(p) or unknown for p in plates}


def ams_trays(state):
    """{slot: (type, color)} for every loaded AMS slot. Slot = unit * 4 + tray (A1 = 0)."""
    trays = {}
    for unit in (state.get("ams") or {}).get("ams") or []:
        for tray in unit.get("tray") or []:
            if tray.get("tray_type"):
                trays[int(unit["id"]) * 4 + int(tray["id"])] = (
                    tray["tray_type"].upper(), (tray.get("tray_color") or "")[:6].upper())
    return trays


def slot_name(slot):
    return f"{'ABCD'[slot // 4]}{slot % 4 + 1}" if 0 <= slot < 16 else f"slot {slot}"


def color_distance(a, b):
    try:
        return sum((int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) ** 2 for i in (0, 2, 4))
    except (TypeError, ValueError):
        return 0


def choose_ams_mapping(filaments, trays):
    """Pick an AMS slot per filament: same type, closest color, distinct slots where possible."""
    mapping = [-1] * max(f["id"] for f in filaments)
    used = set()
    for f in filaments:
        candidates = [s for s, (t, _) in trays.items() if f["type"] in (None, t)]
        if not candidates:
            loaded = ", ".join(f"{slot_name(s)} {t} #{c}" for s, (t, c) in sorted(trays.items()))
            raise PrintError(f"No {f['type']} loaded in the AMS for filament {f['id']} "
                             f"(AMS has: {loaded or 'nothing'}). Load it, or use --external / --ams-mapping.")
        best = min(candidates, key=lambda s: (s in used, color_distance(f["color"], trays[s][1]), s))
        mapping[f["id"] - 1] = best
        used.add(best)
    return mapping


def describe_mapping(filaments, mapping, trays):
    parts = []
    for f in filaments:
        slot = mapping[f["id"] - 1]
        tray_type, color = trays.get(slot, ("?", "?"))
        parts.append(f"filament {f['id']} ({f['type'] or '?'} #{f['color'] or '?'}) -> "
                     f"{slot_name(slot)} ({tray_type} #{color})")
    return "; ".join(parts)


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


def prepare_job(path, plate=None):
    """Check that a .3mf is sliced; return (plate, filaments used on that plate)."""
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
    return plate, plates[plate]


def resolve_filament_source(state, filaments, external=False, ams_mapping=None):
    """Return (use_ams, ams_mapping, description) for the printer's current AMS contents."""
    trays = ams_trays(state)
    if external or not (state.get("ams") or {}).get("ams"):
        return False, [0], "external spool"
    mapping = list(ams_mapping) if ams_mapping else choose_ams_mapping(filaments, trays)
    return True, mapping, describe_mapping(filaments, mapping, trays)


def run_print(printer, cfg, path, plate, filaments, external=False, ams_mapping=None,
              bed_leveling=True, timelapse=False, flow_cali=False, vibration_cali=False,
              url_template=DEFAULT_URL, skip_upload=False, log=print):
    """Upload a sliced .3mf and start it on a connected Printer.

    Filament comes from the AMS (slots matched by type and color) unless
    external=True or there is no AMS; ams_mapping overrides the matching.

    Raises PrintError if the printer is busy or rejects the job, and
    OSError/TimeoutError if it can't be reached.
    """
    if not printer.online.is_set():
        raise ConnectionError("Printer is offline")
    state = printer.pushall()
    if state.get("gcode_state") in BUSY_STATES:
        raise PrintError(f"Printer is busy ({state['gcode_state']})")
    use_ams, ams_mapping, source = resolve_filament_source(state, filaments, external, ams_mapping)

    remote_name = remote_name_for(path)
    if not skip_upload:
        upload(cfg, Path(path), remote_name, log)

    url = url_template.format(name=remote_name)
    log(f"Starting {url}, plate {plate}, {source}")
    with printer.cond:
        printer.acks.clear()
    printer.start_print(remote_name, plate, use_ams, ams_mapping,
                        bed_leveling=bed_leveling, timelapse=timelapse, url=url,
                        flow_cali=flow_cali, vibration_cali=vibration_cali)

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
    plate, filaments = prepare_job(path, args.plate)

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

        state = run_print(printer, cfg, path, plate, filaments, args.external,
                          parse_ams_mapping(args.ams_mapping), bed_leveling=not args.no_level,
                          timelapse=args.timelapse, flow_cali=args.flow_cali,
                          vibration_cali=args.vibration_cali,
                          url_template=args.url, skip_upload=args.skip_upload)
        print(f"Print started (state: {state.get('gcode_state')}).")
    finally:
        printer.close()


def cmd_light(cfg, args):
    printer = Printer(cfg, debug=args.debug)
    printer.connect()
    try:
        printer.wait_state()
        if printer.set_light(args.state == "on"):
            print(f"Light {args.state}.")
        else:
            print(f"Sent light {args.state}, but the printer didn't confirm it.")
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
    p.add_argument("--external", action="store_true",
                   help="use the external spool instead of the AMS")
    p.add_argument("--ams-mapping", help="AMS slot per filament, 0-based (A1=0, A2=1...), e.g. 1,3 "
                                         "(default: match by type and color)")
    p.add_argument("--no-level", action="store_true", help="skip bed leveling")
    p.add_argument("--timelapse", action="store_true", help="record a timelapse")
    p.add_argument("--flow-cali", action="store_true", help="run flow dynamics calibration first")
    p.add_argument("--vibration-cali", action="store_true", help="run vibration calibration first")
    p.add_argument("--skip-upload", action="store_true", help="file is already on the SD card")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask whether the plate is clear")
    p.add_argument("--url", default=DEFAULT_URL,
                   help="file URL sent to the printer; {name} is the uploaded file name "
                        "(default: file:///sdcard/{name}; alternatives: ftp:///{name}, ftp://{name})")

    p = sub.add_parser("light", help="switch the chamber light")
    p.add_argument("state", choices=["on", "off"])

    args = parser.parse_args()
    cfg = load_config(args.config)
    commands = {"status": cmd_status, "files": cmd_files, "upload": cmd_upload, "print": cmd_print,
                "light": cmd_light}
    try:
        commands[args.command](cfg, args)
    except (PrintError, OSError, ftplib.Error) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
