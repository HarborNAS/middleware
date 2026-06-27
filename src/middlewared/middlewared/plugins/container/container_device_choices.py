from __future__ import annotations

from truenas_pylibvirt.utils.usb import get_all_usb_devices

from middlewared.api.current import ContainerDeviceNicAttachChoices, USBPassthroughDevice
from middlewared.plugins.apps.resources_utils import gpu_assignment_blocked, gpu_host_state, gpu_readiness
from middlewared.service import ServiceContext

from .bridge import container_bridge_name


def nic_attach_choices(context: ServiceContext) -> ContainerDeviceNicAttachChoices:
    container_bridge = container_bridge_name(context)
    bridge: list[str] = [container_bridge]
    macvlan: list[str] = []
    for inf in context.middleware.call_sync('interface.choices', {'exclude': ['epair', 'tap', 'vnet']}):
        if inf.startswith('br'):
            bridge.append(inf)
        else:
            macvlan.append(inf)
    return ContainerDeviceNicAttachChoices(BRIDGE=bridge, MACVLAN=macvlan)


def usb_choices() -> dict[str, USBPassthroughDevice]:
    return {
        key: USBPassthroughDevice(**value)
        for key, value in get_all_usb_devices().items()
    }


def gpu_choice_value(gpu: dict, host_state: dict) -> str | dict:
    vendor = gpu['vendor']
    if vendor != 'AMD':
        return vendor

    readiness = gpu_readiness(gpu, host_state)
    if not gpu_assignment_blocked(readiness['failure_reason']):
        return vendor

    return {
        'pci_slot': gpu['addr']['pci_slot'],
        'gpu_type': vendor,
        'description': gpu['description'],
        'available': False,
        'error': readiness['failure_reason'],
        'readiness': readiness,
        'capabilities': readiness['capabilities'],
        'failure_reason': readiness['failure_reason'],
        'recommended_actions': readiness['recommended_actions'],
        'device_nodes': readiness['device_nodes'],
        'container_runtime': readiness['container_runtime'],
        'os_profile': readiness['os_profile'],
    }


async def gpu_choices(context: ServiceContext) -> dict[str, str | dict]:
    host_state = await context.to_thread(gpu_host_state)
    choices = {}
    for gpu in await context.middleware.call('device.get_gpus'):
        if gpu['vendor'] not in ('AMD', 'INTEL', 'NVIDIA'):
            continue
        if not gpu['available_to_host']:
            continue
        choices[gpu['addr']['pci_slot']] = gpu_choice_value(gpu, host_state)

    return choices
