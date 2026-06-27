import glob
import os
import subprocess


AMD_W7900_PCI_IDS = {'1002:7448'}
INTEL_ARC_MARKERS = ('arc', 'data center gpu', 'flex', 'max')
W7900_BAR_PROFILE_ID = 'amd_w7900_bar_rebar_minimal'
W7900_RESOURCE_ALIGNMENT_SIZE = 36
KERNEL_GPU_LOG_PATTERNS = (
    'amdgpu', 'kfd', 'bar 0', 'bridge window', 'no space', "can't assign", 'resource'
)
GPU_ASSIGNMENT_BLOCKING_FAILURES = {
    'gpu_not_available_to_host',
    'nvidia_procfs_details_missing',
    'nvidia_device_nodes_missing',
    'nvidia_container_runtime_missing',
    'amd_w7900_bar_rebar_failure',
    'amd_pcie_bar_resource_failure',
    'amd_render_node_missing',
    'intel_arc_render_node_missing',
}


def get_gpu_base_dict() -> dict:
    return {
        'vendor': '',
        'description': '',
        'error': None,
        'vendor_specific_config': {},
        'gpu_details': {},
        'pci_slot': None,
        'readiness': {},
        'capabilities': [],
        'failure_reason': None,
        'recommended_actions': [],
        'device_nodes': {},
        'container_runtime': {},
        'os_profile': None,
    }


def gpu_host_state() -> dict:
    return {
        'dev_dri_card': bool(glob.glob('/dev/dri/card*')),
        'dev_dri_render': bool(glob.glob('/dev/dri/renderD*')),
        'dev_kfd': os.path.exists('/dev/kfd'),
        'dev_nvidia': bool(glob.glob('/dev/nvidia*')),
        'nvidia_container_runtime': os.path.exists('/usr/bin/nvidia-container-runtime'),
        'kernel_messages': read_kernel_gpu_messages(),
    }


def read_kernel_gpu_messages() -> str:
    try:
        result = subprocess.run(
            ['journalctl', '-k', '-b', '--no-pager'],
            check=False,
            capture_output=True,
            encoding='utf-8',
            errors='ignore',
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ''

    output = result.stdout or ''
    return '\n'.join(
        line for line in output.splitlines()
        if any(pattern in line.lower() for pattern in KERNEL_GPU_LOG_PATTERNS)
    )


def gpu_pci_ids(gpu_info: dict) -> set[str]:
    return {
        str(device.get('pci_id', '')).lower()
        for device in gpu_info.get('devices') or []
        if device.get('pci_id')
    }


def gpu_pci_slot(gpu_info: dict) -> str | None:
    return (gpu_info.get('addr') or {}).get('pci_slot') or gpu_info.get('pci_slot')


def gpu_upstream_bridge_pci_slot(gpu_info: dict, host_state: dict) -> str | None:
    return (
        host_state.get('upstream_bridge_pci_slot')
        or gpu_info.get('upstream_bridge_pci_slot')
        or (gpu_info.get('addr') or {}).get('upstream_bridge_pci_slot')
    )


def amd_bar_resource_failure(kernel_messages: str) -> bool:
    return 'bar 0' in kernel_messages and ('no space' in kernel_messages or "can't assign" in kernel_messages)


def gpu_kernel_messages(gpu_info: dict, kernel_messages: str) -> str:
    pci_slot = gpu_pci_slot(gpu_info)
    if not pci_slot:
        return ''

    pci_slot = pci_slot.lower()
    return '\n'.join(line for line in kernel_messages.splitlines() if pci_slot in line.lower())


def gpu_assignment_blocked(failure_reason: str | None) -> bool:
    return failure_reason in GPU_ASSIGNMENT_BLOCKING_FAILURES


def w7900_bar_os_profile(gpu_info: dict, host_state: dict) -> dict:
    bridge_pci_slot = gpu_upstream_bridge_pci_slot(gpu_info, host_state)
    bridge_value = bridge_pci_slot or '<upstream_bridge_pci_slot>'
    kernel_extra_options = (
        f'pci=realloc=on,big_root_window,'
        f'resource_alignment={W7900_RESOURCE_ALIGNMENT_SIZE}@{bridge_value}'
    )

    return {
        'profile_id': W7900_BAR_PROFILE_ID,
        'gpu_pci_slot': gpu_pci_slot(gpu_info),
        'upstream_bridge_pci_slot': bridge_pci_slot,
        'kernel_extra_options': kernel_extra_options,
        'append_to': 'system.advanced.kernel_extra_options',
        'apply_via': 'system.advanced.update',
        'append_only': True,
        'requires_reboot': True,
        'auto_apply': False,
        'manual_confirmation_required': True,
        'manual_bridge_confirmation_required': bridge_pci_slot is None,
    }


def is_intel_arc_gpu(gpu_info: dict) -> bool:
    description = (gpu_info.get('description') or '').lower()
    return any(marker in description for marker in INTEL_ARC_MARKERS)


def gpu_vendor(gpu_info: dict) -> str:
    return str(gpu_info.get('vendor') or '').upper()


def gpu_device_nodes(vendor: str, host_state: dict) -> dict:
    nodes = {
        'dri_card': bool(host_state.get('dev_dri_card')),
        'dri_render': bool(host_state.get('dev_dri_render')),
    }
    if vendor == 'AMD':
        nodes['kfd'] = bool(host_state.get('dev_kfd'))
    if vendor == 'NVIDIA':
        nodes['nvidia'] = bool(host_state.get('dev_nvidia'))
    return nodes


def gpu_failure_and_actions(gpu_info: dict, host_state: dict, nvidia_details: dict | None = None) -> tuple:
    vendor = gpu_vendor(gpu_info)
    pci_ids = gpu_pci_ids(gpu_info)
    kernel_messages = gpu_kernel_messages(gpu_info, host_state.get('kernel_messages') or '').lower()

    if not gpu_info.get('available_to_host'):
        return (
            'gpu_not_available_to_host',
            ['Release this GPU from isolation or other host consumers before assigning it to Apps or containers.'],
            None,
        )

    if vendor == 'NVIDIA':
        if not nvidia_details:
            return (
                'nvidia_procfs_details_missing',
                ['Enable NVIDIA support in System Settings and confirm nvidia-smi can see this GPU.'],
                None,
            )
        if not host_state.get('dev_nvidia'):
            return (
                'nvidia_device_nodes_missing',
                ['Enable the NVIDIA sysext/driver and restart Apps after driver setup completes.'],
                None,
            )
        if not host_state.get('nvidia_container_runtime'):
            return (
                'nvidia_container_runtime_missing',
                ['Install or enable nvidia-container-runtime before running CUDA containers.'],
                None,
            )

    if vendor == 'AMD':
        bar_resource_failure = amd_bar_resource_failure(kernel_messages)
        w7900_bar_failure = pci_ids & AMD_W7900_PCI_IDS and bar_resource_failure
        if w7900_bar_failure:
            os_profile = w7900_bar_os_profile(gpu_info, host_state)
            return (
                'amd_w7900_bar_rebar_failure',
                [
                    'W7900 BAR allocation failed. After IT confirmation, append the recommended kernel options via '
                    'system.advanced.update and reboot once. Do not apply this profile globally.',
                ],
                os_profile,
            )
        if bar_resource_failure:
            return (
                'amd_pcie_bar_resource_failure',
                [
                    'AMD BAR allocation failed, but this is not a matched W7900 profile. Confirm the upstream bridge '
                    'and hardware profile manually before changing kernel options.',
                ],
                None,
            )
        if not host_state.get('dev_dri_render'):
            return (
                'amd_render_node_missing',
                ['Confirm amdgpu is bound and /dev/dri/renderD* exists on the host.'],
                None,
            )
        if not host_state.get('dev_kfd'):
            return (
                'amd_kfd_missing',
                ['Confirm /dev/kfd exists and pass it through to ROCm/Ollama containers.'],
                None,
            )

    if vendor == 'INTEL':
        if not is_intel_arc_gpu(gpu_info):
            return (
                'intel_igpu_non_target',
                ['Intel integrated graphics is not a target local LLM accelerator for HarborNAS GPU Apps.'],
                None,
            )
        if not host_state.get('dev_dri_render'):
            return (
                'intel_arc_render_node_missing',
                ['Confirm i915/xe is bound and /dev/dri/renderD* exists before assigning Intel Arc to containers.'],
                None,
            )

    return None, [], None


def gpu_capabilities(gpu_info: dict, host_state: dict, failure_reason: str | None) -> list[str]:
    vendor = gpu_vendor(gpu_info)
    if failure_reason:
        return []
    if vendor == 'NVIDIA':
        return ['container-cuda', 'local-llm']
    if vendor == 'AMD':
        return ['container-rocm', 'local-llm']
    if vendor == 'INTEL' and is_intel_arc_gpu(gpu_info):
        return ['container-intel-arc', 'level-zero-or-openvino']
    return []


def gpu_readiness(gpu_info: dict, host_state: dict, nvidia_details: dict | None = None) -> dict:
    vendor = gpu_vendor(gpu_info)
    failure_reason, recommended_actions, os_profile = gpu_failure_and_actions(gpu_info, host_state, nvidia_details)
    if gpu_assignment_blocked(failure_reason):
        status = 'blocked'
    elif failure_reason == 'intel_igpu_non_target':
        status = 'non_target'
    elif failure_reason:
        status = 'degraded'
    else:
        status = 'ready'

    return {
        'status': status,
        'vendor': vendor,
        'target_accelerator': failure_reason != 'intel_igpu_non_target',
        'device_nodes': gpu_device_nodes(vendor, host_state),
        'container_runtime': {
            'nvidia': bool(host_state.get('nvidia_container_runtime')) if vendor == 'NVIDIA' else None,
            'amd_rocm_host_devices': (
                bool(host_state.get('dev_dri_render')) and bool(host_state.get('dev_kfd'))
            ) if vendor == 'AMD' else None,
            'intel_render_device': bool(host_state.get('dev_dri_render')) if vendor == 'INTEL' else None,
        },
        'failure_reason': failure_reason,
        'recommended_actions': recommended_actions,
        'os_profile': os_profile,
        'capabilities': gpu_capabilities(gpu_info, host_state, failure_reason),
    }


def get_normalized_gpu_choices(
    all_gpus_info: list[dict],
    nvidia_gpus: dict,
    host_state: dict | None = None,
) -> list[dict]:
    host_state = host_state or gpu_host_state()
    all_gpus_info = {gpu['addr']['pci_slot']: gpu for gpu in all_gpus_info}
    gpus = []
    for pci_slot, gpu_info in all_gpus_info.items():
        gpu_config = get_gpu_base_dict() | {
            'vendor': gpu_info['vendor'],
            'description': gpu_info['description'],
            'gpu_details': gpu_info,
            'pci_slot': pci_slot,
        }
        gpus.append(gpu_config)

        nvidia_gpu = None
        if gpu_info['vendor'] == 'NVIDIA':
            if pci_slot not in nvidia_gpus:
                gpu_config.update({
                    'error': 'Unable to locate GPU details from procfs',
                })
                continue

            nvidia_gpu = nvidia_gpus[pci_slot]
            error = None
            if not nvidia_gpu.get('gpu_uuid'):
                error = 'GPU UUID not found'
            elif '?' in nvidia_gpu['gpu_uuid']:
                error = 'Malformed GPU UUID found'
            if error:
                gpu_config.update({
                    'error': error,
                    'nvidia_gpu_details': nvidia_gpu,
                })
                continue

            gpu_config.update({
                'vendor_specific_config': {
                    'uuid': nvidia_gpu['gpu_uuid'],
                },
                'description': nvidia_gpu.get('model') or gpu_config['description'],
            })

        readiness = gpu_readiness(gpu_info, host_state, nvidia_gpu)
        gpu_config.update({
            'readiness': readiness,
            'capabilities': readiness['capabilities'],
            'failure_reason': readiness['failure_reason'],
            'recommended_actions': readiness['recommended_actions'],
            'device_nodes': readiness['device_nodes'],
            'container_runtime': readiness['container_runtime'],
            'os_profile': readiness['os_profile'],
        })
        if gpu_assignment_blocked(readiness['failure_reason']):
            gpu_config['error'] = readiness['failure_reason']

        if not gpu_info['available_to_host']:
            gpu_config.update({
                'error': 'GPU not available to host',
            })

    return gpus
