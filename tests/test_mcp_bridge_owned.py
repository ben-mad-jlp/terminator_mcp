"""Unit tests for the owned-pane helpers in the MCP bridge plugin.

These cover the pure logic (argument validation, split choice, exit-status
decoding). Spawning into real panes needs a running Terminator and is a manual
check; see terminator_mcp/doc/planning/owned_panes.md.
"""

import os
import signal

import pytest

from terminatorlib.plugins import mcp_bridge
from terminatorlib.plugins.mcp_bridge import (
    _BridgeError, choose_split, decode_wait_status, validate_spawn_args)


def _args(**overrides):
    args = {'owner': 'jlp-sim', 'name': 'talker', 'argv': ['bash', '-c', 'true'],
            'env': {'HOME': '/home/u', 'PATH': '/usr/bin'}, 'tab': 'sim'}
    args.update(overrides)
    return args


def test_validate_accepts_and_normalises():
    owner, name, argv, env, tab, cwd = validate_spawn_args(_args())
    assert (owner, name, tab) == ('jlp-sim', 'talker', 'sim')
    assert argv == ['bash', '-c', 'true']
    assert cwd == '/home/u'


def test_validate_tab_defaults_to_owner():
    assert validate_spawn_args(_args(tab=None))[4] == 'jlp-sim'


@pytest.mark.parametrize('field,value,code', [
    ('owner', '', 'bad_owner'),
    ('owner', 'Jlp Sim', 'bad_owner'),
    ('name', '../x', 'bad_name'),
    ('name', '', 'bad_name'),
    ('argv', [], 'bad_argv'),
    ('argv', 'bash -c true', 'bad_argv'),
    ('argv', ['bash', 3], 'bad_argv'),
    ('argv', ['X=1', 'bash'], 'bad_argv'),
    ('env', ['HOME=/x'], 'bad_env'),
    ('env', {'1BAD': 'x'}, 'bad_env'),
    ('env', {'A-B': 'x'}, 'bad_env'),
    ('env', {'OK': 5}, 'bad_env'),
])
def test_validate_rejects(field, value, code):
    with pytest.raises(_BridgeError) as info:
        validate_spawn_args(_args(**{field: value}))
    assert str(info.value) == code


def test_choose_split_picks_largest_area():
    key, _vertical = choose_split([('a', 400, 300), ('b', 800, 600),
                                   ('c', 1000, 100)])
    assert key == 'b'


def test_choose_split_wide_pane_goes_side_by_side():
    assert choose_split([('a', 1200, 400)]) == ('a', False)


def test_choose_split_tall_or_square_pane_stacks():
    assert choose_split([('a', 400, 1200)]) == ('a', True)
    assert choose_split([('a', 500, 500)]) == ('a', True)


def test_decode_wait_status_exit_code():
    assert decode_wait_status(3 << 8) == 3
    assert decode_wait_status(0) == 0


def test_decode_wait_status_signal():
    assert decode_wait_status(signal.SIGINT) == -signal.SIGINT


def test_owned_signals_are_limited():
    assert set(mcp_bridge.OWNED_SIGNALS) == {'INT', 'TERM', 'KILL'}
