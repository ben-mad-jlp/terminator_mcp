"""Unit tests for the pure parts of the restore_layout plugin.

Building tabs and typing into panes needs a running Terminator and is a
manual check; see terminator_mcp/doc/planning/restore_layout.md.
"""

import copy

from terminatorlib.plugins.restore_layout import (
    busy_label, layout_panes, match_panes, restorable_root, strip_layout)

# The shape Terminator saves: a flat dict, linked by 'parent'.
PNP = {
    'child0': {'type': 'Window', 'parent': '', 'order': '0'},
    'child1': {'type': 'HPaned', 'parent': 'child0', 'order': '0',
               'ratio': '0.5'},
    'terminal2': {'type': 'Terminal', 'parent': 'child1', 'order': '0',
                  'title': 'pnp', 'uuid': 'u2',
                  'command': "ssh -t pnp.jlp \"bash -ic 'devtach launch'\""},
    'child3': {'type': 'VPaned', 'parent': 'child1', 'order': '1',
               'ratio': '0.5'},
    'terminal4': {'type': 'Terminal', 'parent': 'child3', 'order': '0',
                  'title': 'pnpElevator', 'uuid': 'u4'},
    'terminal5': {'type': 'Terminal', 'parent': 'child3', 'order': '1',
                  'title': 'control', 'uuid': 'u5', 'command': 'devtach control'},
}


def test_restorable_root_is_the_windows_only_child():
    root = restorable_root(copy.deepcopy(PNP))
    assert root['type'] == 'HPaned'
    assert sorted(root['children']) == ['child3', 'terminal2']


def test_single_terminal_layout_is_restorable():
    flat = {'w': {'type': 'Window', 'parent': ''},
            't': {'type': 'Terminal', 'parent': 'w', 'title': 'solo'}}
    assert restorable_root(flat)['type'] == 'Terminal'


def test_multi_tab_layout_is_not_restorable():
    flat = {'w': {'type': 'Window', 'parent': ''},
            'n': {'type': 'Notebook', 'parent': 'w'},
            't1': {'type': 'Terminal', 'parent': 'n', 'order': '0'},
            't2': {'type': 'Terminal', 'parent': 'n', 'order': '1'}}
    assert restorable_root(flat) is None


def test_multi_window_layout_is_not_restorable():
    flat = {'w1': {'type': 'Window', 'parent': ''},
            't1': {'type': 'Terminal', 'parent': 'w1'},
            'w2': {'type': 'Window', 'parent': ''},
            't2': {'type': 'Terminal', 'parent': 'w2'}}
    assert restorable_root(flat) is None


def test_default_layout_shape_is_restorable_but_has_no_commands():
    flat = {'window0': {'type': 'Window', 'parent': ''},
            'child1': {'type': 'Terminal', 'parent': 'window0'}}
    assert layout_panes(restorable_root(flat)) == [(None, None)]


def test_layout_panes_in_order():
    root = restorable_root(copy.deepcopy(PNP))
    assert layout_panes(root) == [
        ('pnp', "ssh -t pnp.jlp \"bash -ic 'devtach launch'\""),
        ('pnpElevator', None),
        ('control', 'devtach control'),
    ]


def test_strip_layout_drops_command_and_uuid_only():
    root = restorable_root(copy.deepcopy(PNP))
    stripped = strip_layout(root)
    terms = [stripped['children']['terminal2'],
             stripped['children']['child3']['children']['terminal5']]
    for term in terms:
        assert 'command' not in term and 'uuid' not in term
        assert term['title']
    assert 'command' in root['children']['terminal2']    # original untouched


def test_match_panes_by_title_and_report_missing():
    panes = [('pnp', 'cmd-a'), ('pnpElevator', None), ('control', 'cmd-c')]
    existing = [('control', 'T-control'), ('pnp', 'T-pnp'), ('other', 'T-x')]
    matched, missing = match_panes(panes, existing)
    assert matched == [('T-pnp', 'pnp', 'cmd-a'),
                       ('T-control', 'control', 'cmd-c')]
    assert missing == ['pnpElevator']


def test_untitled_pane_with_command_is_reported_missing():
    matched, missing = match_panes([(None, 'cmd'), (None, None)],
                                   [(None, 'T')])
    assert matched == []
    assert missing == ['(untitled)']


def test_busy_label():
    assert busy_label(fg_pgrp=100, shell_pid=100, comm='bash') is None
    assert busy_label(fg_pgrp=200, shell_pid=100, comm='ssh') == 'busy: ssh'
    assert busy_label(fg_pgrp=200, shell_pid=100, comm=None) == 'busy'
