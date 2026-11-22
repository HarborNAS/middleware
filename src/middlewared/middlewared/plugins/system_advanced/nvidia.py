from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

from truenas_pylibvirt.utils.gpu import get_gpus

from middlewared.api import api_method
from middlewared.api.current import (
    SystemAdvancedNvidiaPresentArgs,
    SystemAdvancedNvidiaPresentResult,
    SystemAdvancedNvidiaStatusArgs,
    SystemAdvancedNvidiaStatusResult,
)
from middlewared.service import CallError, private, Service


NVIDIA_STATUS_KEY = 'system.advanced.nvidia_status'
NVIDIA_EXTENSION = '/usr/share/truenas/sysext-extensions/nvidia.raw'


def _boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


class SystemAdvancedService(Service):

    class Config:
        namespace = 'system.advanced'
        cli_namespace = 'system.advanced'

    @api_method(
        SystemAdvancedNvidiaPresentArgs,
        SystemAdvancedNvidiaPresentResult,
        roles=['SYSTEM_ADVANCED_READ'],
    )
    def nvidia_present(self):
        """Returns whether a non-isolated NVIDIA GPU is present in the system."""
        adv_config = self.middleware.call_sync('system.advanced.config')
        for gpu in get_gpus():
            if gpu['addr']['pci_slot'] in adv_config['isolated_gpu_pci_ids']:
                continue
            if gpu['vendor'] == 'NVIDIA':
                return True
        return False

    @api_method(SystemAdvancedNvidiaStatusArgs, SystemAdvancedNvidiaStatusResult, roles=['SYSTEM_ADVANCED_READ'])
    def nvidia_status(self):
        """Return the last observed NVIDIA setup state for this boot and enablement request."""
        enabled = self.middleware.call_sync('system.advanced.config')['nvidia']
        recorded = self.middleware.call_sync('keyvalue.get', NVIDIA_STATUS_KEY, {})
        status = recorded.get('status', {})
        if recorded.get('boot_id') != _boot_id() or status.get('enabled') != enabled:
            return {'enabled': enabled, 'state': 'UNKNOWN', 'stage': 'not_checked', 'error': None, 'checked_at': None}
        return status

    @private
    def record_nvidia_status(self, enabled, state, stage, error=None):
        status = {'enabled': enabled, 'state': state, 'stage': stage, 'error': error,
                  'checked_at': datetime.now(timezone.utc).isoformat()}
        self.middleware.call_sync('keyvalue.set', NVIDIA_STATUS_KEY,
                                  {'boot_id': _boot_id(), 'status': status}, {'ha_sync': False})
        return status

    @private
    async def handle_nvidia_toggle(self):
        # this only gets called if nvidia setting was toggled
        # which means we need to restart apps once we have run our configuration logic
        status = await self.middleware.call('system.advanced.configure_nvidia')
        if status['state'] == 'ERROR':
            raise CallError(f"NVIDIA setup failed during {status['stage']}: {status['error']}")
        if status['state'] == 'NO_DEVICE':
            return status
        if (await self.middleware.call('docker.config')).pool:
            # We explicitly have it non-blocking here as docker restart on HDD based systems can take
            # decent amount of time
            self.middleware.create_task(self.middleware.call('docker.restart_service'))
        return status

    @private
    def configure_nvidia(self):
        config = self.middleware.call_sync('system.advanced.config')
        enabled = config['nvidia']
        nvidia_sysext_path = '/run/extensions/nvidia.raw'
        stage = 'extension'
        try:
            activated = os.path.lexists(nvidia_sysext_path)
            if activated and (not os.path.islink(nvidia_sysext_path)
                              or os.readlink(nvidia_sysext_path) != NVIDIA_EXTENSION):
                raise RuntimeError('NVIDIA extension activation path is not the managed symlink')
            if enabled and not os.path.isfile(NVIDIA_EXTENSION):
                raise FileNotFoundError('Packaged NVIDIA extension is missing')
            refresh = False
            if enabled and not activated:
                os.makedirs('/run/extensions', exist_ok=True)
                os.symlink(NVIDIA_EXTENSION, nvidia_sysext_path)
                refresh = True
            elif not enabled and activated:
                os.unlink(nvidia_sysext_path)
                refresh = True
            if refresh:
                subprocess.run(['systemd-sysext', 'refresh'], capture_output=True, check=True, text=True, timeout=30)
                subprocess.run(['ldconfig'], capture_output=True, check=True, text=True, timeout=30)
            if not enabled:
                return self.record_nvidia_status(False, 'DISABLED', 'complete')
            stage = 'device'
            if not self.nvidia_present():
                return self.record_nvidia_status(True, 'NO_DEVICE', stage)
            stage = 'module'
            subprocess.run(
                ['modprobe', '-a', 'nvidia', 'nvidia_drm', 'nvidia_modeset'],
                capture_output=True, check=True, text=True, timeout=30,
            )
            stage = 'device'
            subprocess.run(['nvidia-smi'], capture_output=True, check=True, text=True, timeout=15)
        except Exception as error:
            detail = getattr(error, 'stderr', None) or str(error)
            if isinstance(detail, bytes):
                detail = detail.decode('utf-8', errors='replace')
            detail = str(detail).replace('\x00', '')[:1024]
            self.logger.error('NVIDIA setup failed during %s: %s', stage, detail)
            return self.record_nvidia_status(enabled, 'ERROR', stage, detail)
        return self.record_nvidia_status(True, 'READY', 'complete')


async def setup(middleware):
    try:
        await middleware.call('system.advanced.configure_nvidia')
    except Exception:
        middleware.logger.error('Unhandled exception configuring nvidia', exc_info=True)
