"""What the web shows for each kind of edge device, and how it judges device health.

A profile names the design decision rate, the warning thresholds and the hardware readings a
kind of device reports, so the Devices page draws whatever a profile lists instead of assuming a
Raspberry Pi. The server picks a profile per device: an entry in server_config.json
"device_profiles" ({device_id: name or {"profile": name, ...overrides}}) wins, otherwise the
reported model decides, otherwise "generic".

The command catalogue gives each remote command its label, confirmation text and group; which
commands a device accepts still comes from the device's own agent ("control.commands").
"""

import copy

from device_commands import COMMANDS

READINGS = {  # key in the telemetry "metrics" -> label and unit for the web
    'cpu_pct': ('CPU', '%'),
    'temperature_c': ('Temperature', '°C'),
    'clock_mhz': ('CPU clock', 'MHz'),
    'ram_used_bytes': ('Memory used', 'bytes'),
    'ram_total_bytes': ('Memory total', 'bytes'),
    'disk_free_bytes': ('Disk free', 'bytes'),
    'uptime_s': ('Uptime', 's'),
}

PROFILES = {
    'raspberry-pi': {
        'label': 'Raspberry Pi',
        'design_rate_hz': 10.0,  # the v2 runtime decides once per 100 ms slot
        'degraded_rate_fraction': 0.9,  # below 90 % of the design rate counts as degraded
        'temperature_warn_c': 75.0,
        'readings': [
            'cpu_pct',
            'temperature_c',
            'clock_mhz',
            'ram_used_bytes',
            'ram_total_bytes',
            'disk_free_bytes',
            'uptime_s',
        ],
        # vcgencmd get_throttled flags reported by the device agent
        'limit_flags': {
            'undervoltage': 'Low voltage',
            'frequency_capped': 'Frequency capped',
            'throttled': 'Throttled',
            'soft_temperature_limit': 'Temperature limit',
        },
    },
    'generic': {
        'label': 'Edge device',
        'design_rate_hz': 10.0,
        'degraded_rate_fraction': 0.9,
        'temperature_warn_c': 80.0,
        'readings': ['cpu_pct', 'temperature_c', 'ram_used_bytes', 'ram_total_bytes', 'disk_free_bytes', 'uptime_s'],
        'limit_flags': {},
    },
}

COMMAND_CATALOG = {
    'restart_inference': {
        'label': 'Restart detection',
        'group': 'service',
        'interrupts_detection': True,
        'confirm': 'Restart the detection service on this device? Detection pauses for a few seconds.',
    },
    'stop_inference': {
        'label': 'Stop detection',
        'group': 'service',
        'interrupts_detection': True,
        'confirm': 'Stop the detection service? Its cameras do not detect falls until it is started again.',
    },
    'start_inference': {
        'label': 'Start detection',
        'group': 'service',
        'interrupts_detection': False,
        'confirm': 'Start the detection service on this device?',
    },
    'logs': {'label': 'Fetch log', 'group': 'service', 'interrupts_detection': False, 'confirm': None},
    'reboot': {
        'label': 'Reboot device',
        'group': 'device',
        'interrupts_detection': True,
        'confirm': 'Reboot the device? It is offline for about a minute, and other software on it restarts too.',
    },
    'decision_log_start': {
        'label': 'Start decision log',
        'group': 'decision_log',
        'interrupts_detection': False,
        'confirm': None,
    },
    'decision_log_stop': {
        'label': 'Stop decision log',
        'group': 'decision_log',
        'interrupts_detection': False,
        'confirm': None,
    },
    'decision_log_collect': {
        'label': 'Collect decision logs',
        'group': 'decision_log',
        'interrupts_detection': False,
        'confirm': None,
    },
}
assert set(COMMAND_CATALOG) == set(COMMANDS), 'every remote command needs a catalogue entry'

_OVERRIDABLE = ('design_rate_hz', 'degraded_rate_fraction', 'temperature_warn_c', 'label')


def validate_config(entries):
    """Check server_config.json "device_profiles"; returns {device_id: {"profile": name, ...}}."""
    if entries is None:
        return {}
    if not isinstance(entries, dict):
        raise ValueError('device_profiles must be an object')
    clean = {}
    for device_id, entry in entries.items():
        entry = {'profile': entry} if isinstance(entry, str) else entry
        if not isinstance(entry, dict) or entry.get('profile') not in PROFILES:
            raise ValueError(f'device_profiles.{device_id}: profile must be one of {sorted(PROFILES)}')
        extra = set(entry) - {'profile', *_OVERRIDABLE}
        if extra:
            raise ValueError(f'device_profiles.{device_id}: unsupported keys {sorted(extra)}')
        for key in ('design_rate_hz', 'temperature_warn_c'):
            if key in entry and not (isinstance(entry[key], (int, float)) and 0 < entry[key] < 1000):
                raise ValueError(f'device_profiles.{device_id}.{key} must be a positive number')
        if 'degraded_rate_fraction' in entry and not (
            isinstance(entry['degraded_rate_fraction'], (int, float)) and 0 < entry['degraded_rate_fraction'] <= 1
        ):
            raise ValueError(f'device_profiles.{device_id}.degraded_rate_fraction must be in (0, 1]')
        clean[device_id] = entry
    return clean


def profile_for(device, configured):
    """The profile for one device report: configured entry, else the reported model, else generic."""
    entry = configured.get(device.get('device_id'))
    if entry:
        name = entry['profile']
    else:
        model = str((device.get('spec') or {}).get('model') or '')
        name = 'raspberry-pi' if 'raspberry pi' in model.lower() else 'generic'
    profile = copy.deepcopy(PROFILES[name])
    profile['name'] = name
    for key in _OVERRIDABLE:
        if entry and key in entry:
            profile[key] = entry[key]
    profile['readings'] = [
        {'key': key, 'label': READINGS[key][0], 'unit': READINGS[key][1]} for key in profile['readings']
    ]
    return profile
