#!/usr/bin/env python3
"""Low-rate Pi telemetry agent; standard library only, separate from inference.

With a "control" block in its config the agent also runs administrator commands the server hands back in the
telemetry reply: restart/stop/start the inference service, return the last log lines and, only where
allow_reboot is set, reboot. Nothing else can be run; the server never connects to the device."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
import urllib.request
from decision_log_runtime import LogAgent

FLAGS = ('undervoltage', 'frequency_capped', 'throttled', 'soft_temperature_limit')


def throttle_flags(raw):
    if raw is None:
        return {'raw': None, 'current': None, 'since_boot': None}
    return {
        'raw': raw,
        'current': {name: bool(raw & (1 << i)) for i, name in enumerate(FLAGS)},
        'since_boot': {name: bool(raw & (1 << (i + 16))) for i, name in enumerate(FLAGS)},
    }


def read(path):
    try:
        return Path(path).read_text().strip().strip('\x00')
    except (OSError, UnicodeError):
        return None


CONTROL_COMMANDS = (
    'restart_inference',
    'stop_inference',
    'start_inference',
    'logs',
    'decision_log_start',
    'decision_log_stop',
    'decision_log_collect',
)
LOG_LINES = 200


class Controller:
    """Runs one allow-listed command and returns (ok, output). `run` is injectable for tests."""

    def __init__(self, control, run=subprocess.run):
        service = control.get('inference_service', '')
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', service):
            raise ValueError('control.inference_service must be a systemd service name')
        self.service, self.log_file, self.run = service, control.get('log_file'), run
        self.reboot = control.get('allow_reboot') is True
        self.log_agent = (
            LogAgent(control.get('decision_log_dir') or str(Path(self.log_file).parent / 'decision_logs'))
            if self.log_file
            else None
        )
        self.commands = list(CONTROL_COMMANDS) + (['reboot'] if self.reboot else [])

    def _proc(self, argv, timeout=30):
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=timeout)
            return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f'{type(exc).__name__}: {exc}'

    def execute(self, name):
        if name not in self.commands:
            return False, f'command not allowed on this device: {name}'
        if name == 'logs':
            if not self.log_file:
                return False, 'no log_file configured'
            try:
                with open(self.log_file, 'rb') as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 256 * 1024))
                    lines = stream.read().replace(b'\x00', b'').decode('utf-8', 'replace').splitlines()
                return True, '\n'.join(lines[-LOG_LINES:])
            except OSError as exc:
                return False, f'cannot read log: {exc}'
        if name == 'reboot':
            return self._proc(['sudo', '-n', 'systemctl', 'reboot'])
        verb = name.split('_')[0]
        ok, output = self._proc(['systemctl', '--user', verb, self.service])
        _, state = self._proc(['systemctl', '--user', 'is-active', self.service], timeout=5)
        return ok, f'{output}\nservice {self.service}: {state}'.strip()


def post_json(url, payload, headers, timeout=3):
    req = urllib.request.Request(
        url, data=json.dumps(payload, allow_nan=False).encode(), headers=headers, method='POST'
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read() or b'{}')


def command(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=0.8)
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


NET_FIELDS = {'rx_bytes': 0, 'rx_packets': 1, 'tx_bytes': 8, 'tx_packets': 9}


def network_counters(proc_net_dev='/proc/net/dev', sys_net='/sys/class/net'):
    """Cumulative byte/packet counters of the physical interfaces (lo, docker, veth and bridges are skipped).

    The server turns successive samples into rates; this covers everything the device sends, not only Fall-Relay.
    """
    out = {}
    for line in (read(proc_net_dev) or '').splitlines()[2:]:
        name, _, values = line.partition(':')
        name = name.strip()
        if not Path(sys_net, name, 'device').exists():
            continue
        values = values.split()
        try:
            out[name] = {key: int(values[i]) for key, i in NET_FIELDS.items()}
        except (ValueError, IndexError):
            continue
    return out


class Sampler:
    def __init__(self, config):
        self.config = config
        self.previous = None
        self.process_previous = {}
        self.cores = os.cpu_count() or 1
        self.hz = os.sysconf('SC_CLK_TCK')
        self.spec = {
            'model': read('/proc/device-tree/model'),
            'os': next(
                (
                    s.partition('=')[2].strip('"')
                    for s in (read('/etc/os-release') or '').splitlines()
                    if s.startswith('PRETTY_NAME=')
                ),
                None,
            ),
            'kernel': platform.release(),
            'architecture': platform.machine(),
            'cpu_cores': self.cores,
            'python': platform.python_version(),
            'camera': config.get('camera'),
            'cooling': config.get('cooling'),
            'power_supply_rating': config.get('power_supply_rating'),
            'metadata_source': 'OS readings; camera, cooling and power rating come from the device configuration',
            'model_sha256': {name: self.file_hash(path) for name, path in config.get('model_paths', {}).items()},
        }
        versions = {}
        for name in ('numpy', 'requests', 'ncnn', 'ai-edge-litert', 'picamera2', 'opencv-python'):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = None
        self.spec['runtime_versions'] = versions

    @staticmethod
    def file_hash(path):
        try:
            with open(path, 'rb') as stream:
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
                return digest.hexdigest()
        except OSError:
            return None

    def sample(self):
        started = time.monotonic()
        errors = []
        cpu = None
        try:
            # Linux guest counters are already included in user/nice: exclude them.
            values = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(values), values[3] + values[4]
            if self.previous and total > self.previous[0]:
                cpu = round(100 * (1 - (idle - self.previous[1]) / (total - self.previous[0])), 2)
            self.previous = (total, idle)
        except (OSError, ValueError, IndexError):
            errors.append('cpu unavailable')
        memory = {}
        for line in (read('/proc/meminfo') or '').splitlines():
            parts = line.split()
            if parts[0] in ('MemTotal:', 'MemAvailable:'):
                memory[parts[0][:-1]] = int(parts[1]) * 1024
        ram_total, ram_available = memory.get('MemTotal'), memory.get('MemAvailable')
        temp = read('/sys/class/thermal/thermal_zone0/temp')
        freq = read('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq')
        raw = command(['vcgencmd', 'get_throttled'])
        match = re.fullmatch(r'throttled=(0x[0-9a-fA-F]+)', raw or '')
        if not match:
            errors.append('vcgencmd unavailable, refused or unrecognised')
        processes = []
        now = time.monotonic()
        active_keys = set()
        for service in self.config.get('services', []):
            output = command(['systemctl', '--user', 'show', service, '--property=MainPID', '--value'])
            pid = int(output) if output and output.isdigit() else None
            stat = read(f'/proc/{pid}/stat') if pid else None
            item = {'service': service, 'pid': pid or None, 'running': bool(stat), 'rss_bytes': None, 'cpu_pct': None}
            if stat:
                try:
                    fields = stat[stat.rindex(')') + 2 :].split()
                    ticks, start_ticks = int(fields[11]) + int(fields[12]), int(fields[19])
                    key = (service, pid, start_ticks)
                    prev = self.process_previous.get(key)
                    if prev and now > prev[0]:
                        item['cpu_pct'] = round(100 * (ticks - prev[1]) / self.hz / (now - prev[0]) / self.cores, 2)
                    self.process_previous[key] = (now, ticks)
                    active_keys.add(key)
                    item['rss_bytes'] = int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
                except (ValueError, IndexError):
                    errors.append(f'process stats unavailable: {service}')
            processes.append(item)
        self.process_previous = {k: v for k, v in self.process_previous.items() if k in active_keys}
        try:
            disk = shutil.disk_usage(self.config.get('disk_path', '/'))
            disk_total, disk_free = disk.total, disk.free
        except OSError:
            disk_total = disk_free = None
            errors.append('disk unavailable')
        uptime = read('/proc/uptime')
        metrics = {
            'cpu_pct': cpu,
            'temperature_c': float(temp) / 1000 if temp else None,
            'clock_mhz': float(freq) / 1000 if freq else None,
            'ram_total_bytes': ram_total,
            'ram_available_bytes': ram_available,
            'ram_used_bytes': ram_total - ram_available
            if ram_total is not None and ram_available is not None
            else None,
            'disk_total_bytes': disk_total,
            'disk_free_bytes': disk_free,
            'uptime_s': float(uptime.split()[0]) if uptime else None,
        }
        return {
            'schema_version': 1,
            'device_id': self.config['device_id'],
            'camera_id': str(self.config['camera_id']),
            'measured_at': time.time(),
            'interval_s': self.config['interval_s'],
            'spec': self.spec,
            'metrics': metrics,
            'throttle': throttle_flags(int(match[1], 16) if match else None),
            'processes': processes,
            'network': network_counters(),
            'errors': errors,
            'collection_ms': round((time.monotonic() - started) * 1000, 3),
        }


def load_config(path):
    config = json.loads(Path(path).read_text())
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', config.get('device_id', '')):
        raise ValueError('device_id must be 1-64 letters, digits, _ or -')
    config['camera_id'] = str(config['camera_id'])
    config['interval_s'] = float(config.get('interval_s', 5))
    if not 2 <= config['interval_s'] <= 300:
        raise ValueError('interval_s must be in [2, 300]')
    config.setdefault('services', [])
    if not isinstance(config['services'], list) or any(
        not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', s) for s in config['services']
    ):
        raise ValueError('services must contain systemd service names')
    return config


def run_command(controller, item, server, device_id, headers):
    """Run one command handed back by the server and report its result. A reboot is reported first."""
    name, id = item.get('command'), item.get('id')
    logging.info('command %s: %s', id, name)
    result_url = f'{server}/api/device_commands/{int(id)}/result'
    if name == 'reboot' and controller.reboot:
        post_json(
            result_url,
            {
                'device_id': device_id,
                'ok': True,
                'output': 'reboot requested; the device will be offline for about a minute',
            },
            headers,
        )
        ok, output = controller.execute(name)
        if not ok:
            logging.warning('reboot failed: %s', output)
        return
    if name.startswith('decision_log_') and controller.log_agent:
        ok, output = controller.log_agent.execute(
            name, item.get('params', {}), id, item.get('expires_at', time.time() - 1)
        )
    else:
        ok, output = controller.execute(name)
    try:
        post_json(result_url, {'device_id': device_id, 'ok': ok, 'output': output}, headers, timeout=10)
    except (OSError, ValueError) as exc:
        logging.warning('command result upload failed: %s', type(exc).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--once', action='store_true', help='print local measurement without posting')
    args = parser.parse_args()
    config = load_config(args.config)
    sampler = Sampler(config)
    controller = Controller(config['control']) if config.get('control') else None
    if args.once:
        sampler.sample()
        time.sleep(0.2)
        print(json.dumps(sampler.sample(), indent=2))
        return
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('FALLRELAY_CAMERA_TOKEN', '').strip()
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Camera-Token'] = token
    server = config['server_url'].rstrip('/')
    if controller and controller.log_agent:
        controller.log_agent.run(server, config['device_id'], headers)
    url = server + '/api/device_health'
    while True:
        started = time.monotonic()
        payload = sampler.sample()
        if controller:
            payload['control'] = {'commands': controller.commands}
            if controller.log_agent:
                payload['control']['decision_log'] = controller.log_agent.status()
        try:
            reply = post_json(url, payload, headers)
            for item in (reply.get('commands') or []) if controller else []:
                run_command(controller, item, server, config['device_id'], headers)
            logging.info(
                'device=%s cpu=%s temp=%s throttle=%s collection_ms=%s',
                config['device_id'],
                payload['metrics']['cpu_pct'],
                payload['metrics']['temperature_c'],
                payload['throttle']['raw'],
                payload['collection_ms'],
            )
        except (OSError, ValueError) as exc:
            logging.warning('telemetry upload failed: %s', type(exc).__name__)
        time.sleep(max(0, config['interval_s'] - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
