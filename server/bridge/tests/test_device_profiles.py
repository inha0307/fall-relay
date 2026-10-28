#!/usr/bin/env python3
"""Device profiles: choice by configuration or reported model, overrides, validation and the command catalogue."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
from device_commands import COMMANDS
from device_profiles import COMMAND_CATALOG, PROFILES, profile_for, validate_config


def expect_error(entries):
    try:
        validate_config(entries)
    except ValueError:
        return
    raise AssertionError(f'accepted invalid device_profiles {entries!r}')


def main():
    pi = {'device_id': 'cam1-pi', 'spec': {'model': 'Raspberry Pi 5 Model B Rev 1.0'}}
    other = {'device_id': 'jetson', 'spec': {'model': 'NVIDIA Jetson Orin Nano'}}
    unknown = {'device_id': 'x', 'spec': {}}
    assert profile_for(pi, {})['name'] == 'raspberry-pi'
    assert profile_for(other, {})['name'] == 'generic' and profile_for(unknown, {})['name'] == 'generic'
    assert profile_for(other, {})['limit_flags'] == {}, 'no Raspberry Pi flags on other devices'

    configured = validate_config(
        {'jetson': {'profile': 'generic', 'design_rate_hz': 15, 'label': 'Jetson'}, 'cam1-pi': 'generic'}
    )
    jetson = profile_for(other, configured)
    assert jetson['design_rate_hz'] == 15 and jetson['label'] == 'Jetson'
    assert profile_for(pi, configured)['name'] == 'generic', 'configuration wins over the reported model'
    assert PROFILES['generic']['design_rate_hz'] == 10.0, 'overrides do not leak into the shared profile'
    readings = profile_for(pi, {})['readings']
    assert readings[0] == {'key': 'cpu_pct', 'label': 'CPU', 'unit': '%'}

    assert validate_config(None) == {}
    for bad in (
        [],
        {'a': 'nope'},
        {'a': {'profile': 'generic', 'color': 'red'}},
        {'a': {'profile': 'generic', 'design_rate_hz': 0}},
        {'a': {'profile': 'generic', 'degraded_rate_fraction': 2}},
    ):
        expect_error(bad)

    assert set(COMMAND_CATALOG) == set(COMMANDS)
    assert all(
        entry['label'] and entry['group'] in ('service', 'device', 'decision_log') for entry in COMMAND_CATALOG.values()
    )
    assert all('inference' not in entry['label'].lower() for entry in COMMAND_CATALOG.values()), 'UI says detection'
    print('All device profile checks passed.')


if __name__ == '__main__':
    main()
