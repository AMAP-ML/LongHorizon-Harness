"""Portable smoke checks; failures must raise, never return False to pytest."""
from importlib import import_module

import pytest


@pytest.mark.parametrize('module', [
    'supervisor.service', 'supervisor.control_bus', 'manager',
    'environment.remote_files', 'environment.local', 'dashboard.state',
    'utils.process_group', 'trajectory_artifacts', 'adapters.claude_permissions',
])
def test_imports(module):
    import_module('lh_harness.' + module)
