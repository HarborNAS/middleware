"""Exercise the NVIDIA plugin with OS/framework boundaries isolated from the host."""
import asyncio
import importlib.util
import logging
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class CallError(Exception):
    pass


def load_plugin():
    modules = {name: ModuleType(name) for name in (
        'truenas_pylibvirt', 'truenas_pylibvirt.utils', 'truenas_pylibvirt.utils.gpu',
        'middlewared', 'middlewared.api', 'middlewared.api.current', 'middlewared.service',
    )}
    modules['truenas_pylibvirt.utils.gpu'].get_gpus = lambda: []
    modules['middlewared.api'].api_method = lambda *args, **kwargs: lambda function: function
    for name in ('SystemAdvancedNvidiaPresentArgs', 'SystemAdvancedNvidiaPresentResult',
                 'SystemAdvancedNvidiaStatusArgs', 'SystemAdvancedNvidiaStatusResult'):
        setattr(modules['middlewared.api.current'], name, object)
    modules['middlewared.service'].private = lambda function: function
    modules['middlewared.service'].Service = object
    modules['middlewared.service'].CallError = CallError
    path = Path(__file__).resolve().parents[4] / 'plugins/system_advanced/nvidia.py'
    spec = importlib.util.spec_from_file_location('nvidia_status_under_test', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class NvidiaStatusTests(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin()
        self.service = self.module.SystemAdvancedService()
        self.service.logger = logging.getLogger('nvidia-status-test')
        self.config = {'nvidia': True, 'isolated_gpu_pci_ids': []}
        self.store = {}

        def call_sync(name, *args):
            if name == 'system.advanced.config':
                return self.config
            if name == 'keyvalue.set':
                self.store[args[0]] = args[1]
                return
            if name == 'keyvalue.get':
                return self.store.get(args[0], args[1])
            raise AssertionError('Unexpected middleware call: ' + name)

        self.service.middleware = SimpleNamespace(call_sync=Mock(side_effect=call_sync))
        self.commands = []
        self.command_options = []

    def configure(self, fail=None, present=True, activated=False, image=True, timeout=None):
        def command(args, **kwargs):
            self.commands.append(args)
            self.command_options.append(kwargs)
            if args[0] == timeout:
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            code = 1 if args[0] == fail else 0
            if code and kwargs.get('check'):
                raise subprocess.CalledProcessError(code, args, stderr='driver check failed')
            return subprocess.CompletedProcess(args, code, stdout='', stderr='driver check failed' if code else '')

        with (
            patch.object(self.module.os.path, 'exists', return_value=activated),
            patch.object(self.module.os.path, 'lexists', return_value=activated),
            patch.object(self.module.os.path, 'isfile', return_value=image),
            patch.object(self.module.os.path, 'islink', return_value=True),
            patch.object(self.module.os, 'readlink', return_value='/usr/share/truenas/sysext-extensions/nvidia.raw'),
            patch.object(self.module.os, 'makedirs'), patch.object(self.module.os, 'symlink'),
            patch.object(self.module.os, 'unlink'),
            patch.object(self.module.subprocess, 'run', side_effect=command),
            patch.object(self.service, 'nvidia_present', return_value=present),
            patch.object(self.module, '_boot_id', return_value='current-boot', create=True),
        ):
            return self.service.configure_nvidia()

    def assert_stored(self, result, state, stage):
        self.assertIsInstance(result, dict)
        self.assertEqual(result['state'], state)
        self.assertEqual(result['stage'], stage)
        self.assertEqual(next(iter(self.store.values()))['status'], result)

    def test_module_failure_is_returned_and_persisted(self):
        self.assert_stored(self.configure(fail='modprobe'), 'ERROR', 'module')
        self.assertNotIn(['nvidia-smi'], self.commands)

    def test_device_failure_is_returned_and_persisted(self):
        self.assert_stored(self.configure(fail='nvidia-smi'), 'ERROR', 'device')

    def test_extension_failure_does_not_escape_to_nas_startup(self):
        self.assert_stored(self.configure(fail='systemd-sysext'), 'ERROR', 'extension')
        self.assertFalse(any(command[0] == 'modprobe' for command in self.commands))

    def test_success_is_distinct_from_requested_enablement(self):
        self.assert_stored(self.configure(), 'READY', 'complete')
        with patch.object(self.module, '_boot_id', return_value='current-boot', create=True):
            self.assertEqual(self.service.nvidia_status()['state'], 'READY')

    def test_no_gpu_is_reported_without_module_probe(self):
        self.assert_stored(self.configure(present=False), 'NO_DEVICE', 'device')
        self.assertFalse(any(command[0] in ('modprobe', 'nvidia-smi') for command in self.commands))

    def test_disable_clears_prior_failure(self):
        self.configure(fail='modprobe')
        self.config['nvidia'] = False
        self.assert_stored(self.configure(activated=True), 'DISABLED', 'complete')

    def test_previous_boot_status_does_not_claim_current_readiness(self):
        self.configure()
        with patch.object(self.module, '_boot_id', return_value='another-boot', create=True):
            self.assertEqual(self.service.nvidia_status()['state'], 'UNKNOWN')

    def test_changed_request_does_not_reuse_previous_ready_status(self):
        self.configure()
        self.config['nvidia'] = False
        with patch.object(self.module, '_boot_id', return_value='current-boot', create=True):
            self.assertEqual(self.service.nvidia_status()['state'], 'UNKNOWN')

    def test_missing_extension_is_reported_before_driver_commands(self):
        self.assert_stored(self.configure(image=False), 'ERROR', 'extension')
        self.assertEqual(self.commands, [])

    def test_timed_out_device_check_is_persisted(self):
        self.assert_stored(self.configure(timeout='nvidia-smi'), 'ERROR', 'device')

    def test_all_external_commands_have_bounded_timeouts(self):
        self.configure()
        self.assertEqual(
            dict(zip((command[0] for command in self.commands),
                     (options['timeout'] for options in self.command_options))),
            {'systemd-sysext': 30, 'ldconfig': 30, 'modprobe': 30, 'nvidia-smi': 15},
        )

    def test_startup_does_not_block_nas_when_configuration_raises(self):
        middleware = SimpleNamespace(
            call=AsyncMock(side_effect=RuntimeError('unexpected setup failure')),
            logger=SimpleNamespace(error=Mock()),
        )
        asyncio.run(self.module.setup(middleware))
        middleware.logger.error.assert_called_once_with(
            'Unhandled exception configuring nvidia', exc_info=True,
        )

    def test_failed_manual_toggle_does_not_restart_docker(self):
        async def call(name, *args):
            if name == 'system.advanced.configure_nvidia':
                return {'state': 'ERROR', 'stage': 'module', 'error': 'driver check failed'}
            return SimpleNamespace(pool='tank')

        self.service.middleware.call = AsyncMock(side_effect=call)
        self.service.middleware.create_task = Mock()
        with self.assertRaisesRegex(CallError, 'module'):
            asyncio.run(self.service.handle_nvidia_toggle())
        self.service.middleware.create_task.assert_not_called()


if __name__ == '__main__':
    unittest.main()
