import pytest

from middlewared.plugins.apps.resources_utils import get_normalized_gpu_choices, gpu_assignment_blocked


HOST_STATE_READY = {
    'dev_dri_card': True,
    'dev_dri_render': True,
    'dev_kfd': True,
    'dev_nvidia': True,
    'nvidia_container_runtime': True,
    'kernel_messages': '',
}


@pytest.mark.parametrize('all_gpu_info, nvidia_gpus, should_work', [
    (
        [
            {
                'addr': {
                    'pci_slot': '0000:00:02.0',
                    'domain': '0000',
                    'bus': '00',
                    'slot': '02'
                },
                'description': 'Red Hat, Inc. QXL paravirtual graphic card',
                'devices': [
                    {'pci_id': '8086:1237', 'pci_slot': '0000:00:00.0', 'vm_pci_slot': 'pci_0000_00_00_0'},
                    {'pci_id': '8086:7000', 'pci_slot': '0000:00:01.0', 'vm_pci_slot': 'pci_0000_00_01_0'},
                    {'pci_id': '8086:7010', 'pci_slot': '0000:00:01.1', 'vm_pci_slot': 'pci_0000_00_01_1'},
                    {'pci_id': '8086:7113', 'pci_slot': '0000:00:01.3', 'vm_pci_slot': 'pci_0000_00_01_3'},
                ],
                'vendor': None,
                'uses_system_critical_devices': True,
                'critical_reason': 'Critical devices found: 0000:00:01.0',
                'available_to_host': True
            }
        ],
        {},
        True
    ),
    (
        [
            {
                'addr': {
                    'pci_slot': '0000:00:02.0',
                    'domain': '0000',
                    'bus': '00',
                    'slot': '02'
                },
                'description': 'Red Hat, Inc. QXL paravirtual graphic card',
                'devices': [
                    {'pci_id': '8086:1237', 'pci_slot': '0000:00:00.0', 'vm_pci_slot': 'pci_0000_00_00_0'},
                    {'pci_id': '8086:7000', 'pci_slot': '0000:00:01.0', 'vm_pci_slot': 'pci_0000_00_01_0'},
                    {'pci_id': '8086:7010', 'pci_slot': '0000:00:01.1', 'vm_pci_slot': 'pci_0000_00_01_1'},
                    {'pci_id': '8086:7113', 'pci_slot': '0000:00:01.3', 'vm_pci_slot': 'pci_0000_00_01_3'},
                ],
                'vendor': 'NVIDIA',
                'uses_system_critical_devices': True,
                'critical_reason': 'Critical devices found: 0000:00:01.0',
                'available_to_host': True
            }
        ],
        {
            'gpu_uuid': 112,
            'model': 'A6000x2',
            'description': "NVIDIA's A6000 GPU with 2 cores",
            'pci_slot': 11111,
        },
        False
    ),
    (
        [
            {
                'addr': {
                    'pci_slot': '0000:00:02.0',
                    'domain': '0000',
                    'bus': '00',
                    'slot': '02'
                },
                'description': 'Red Hat, Inc. QXL paravirtual graphic card',
                'devices': [
                    {'pci_id': '8086:1237', 'pci_slot': '0000:00:00.0', 'vm_pci_slot': 'pci_0000_00_00_0'},
                    {'pci_id': '8086:7000', 'pci_slot': '0000:00:01.0', 'vm_pci_slot': 'pci_0000_00_01_0'},
                    {'pci_id': '8086:7010', 'pci_slot': '0000:00:01.1', 'vm_pci_slot': 'pci_0000_00_01_1'},
                    {'pci_id': '8086:7113', 'pci_slot': '0000:00:01.3', 'vm_pci_slot': 'pci_0000_00_01_3'},
                ],
                'vendor': 'NVIDIA',
                'uses_system_critical_devices': True,
                'critical_reason': 'Critical devices found: 0000:00:01.0',
                'available_to_host': True
            }
        ],
        {
            'model': 'A6000x2',
            'description': "NVIDIA's A6000 GPU with 2 cores",
            '0000:00:02.0': {
                'gpu_uuid': '112',
            },
        },
        True
    ),
    (
        [
            {
                'addr': {
                    'pci_slot': '0000:00:02.0',
                    'domain': '0000',
                    'bus': '00',
                    'slot': '02'
                },
                'description': 'Red Hat, Inc. QXL paravirtual graphic card',
                'devices': [
                    {'pci_id': '8086:1237', 'pci_slot': '0000:00:00.0', 'vm_pci_slot': 'pci_0000_00_00_0'},
                    {'pci_id': '8086:7000', 'pci_slot': '0000:00:01.0', 'vm_pci_slot': 'pci_0000_00_01_0'},
                    {'pci_id': '8086:7010', 'pci_slot': '0000:00:01.1', 'vm_pci_slot': 'pci_0000_00_01_1'},
                    {'pci_id': '8086:7113', 'pci_slot': '0000:00:01.3', 'vm_pci_slot': 'pci_0000_00_01_3'},
                ],
                'vendor': 'NVIDIA',
                'uses_system_critical_devices': True,
                'critical_reason': 'Critical devices found: 0000:00:01.0',
                'available_to_host': True
            }
        ],
        {
            'model': 'A6000x2',
            'description': "NVIDIA's A6000 GPU with 2 cores",
            '0000:00:02.0': {},
        },
        False
    ),
    (
        [
            {
                'addr': {
                    'pci_slot': '0000:00:02.0',
                    'domain': '0000',
                    'bus': '00',
                    'slot': '02'
                },
                'description': 'Red Hat, Inc. QXL paravirtual graphic card',
                'devices': [
                    {'pci_id': '8086:1237', 'pci_slot': '0000:00:00.0', 'vm_pci_slot': 'pci_0000_00_00_0'},
                    {'pci_id': '8086:7000', 'pci_slot': '0000:00:01.0', 'vm_pci_slot': 'pci_0000_00_01_0'},
                    {'pci_id': '8086:7010', 'pci_slot': '0000:00:01.1', 'vm_pci_slot': 'pci_0000_00_01_1'},
                    {'pci_id': '8086:7113', 'pci_slot': '0000:00:01.3', 'vm_pci_slot': 'pci_0000_00_01_3'},
                ],
                'vendor': 'NVIDIA',
                'uses_system_critical_devices': True,
                'critical_reason': 'Critical devices found: 0000:00:01.0',
                'available_to_host': True
            }
        ],
        {
            'model': 'A6000x2',
            'description': "NVIDIA's A6000 GPU with 2 cores",
            '0000:00:02.0': {
                'gpu_uuid': '1125?as'
            },
        },
        False
    ),
    (
        [
            {
                'addr': {
                    'pci_slot': '0000:00:02.0',
                    'domain': '0000',
                    'bus': '00',
                    'slot': '02'
                },
                'description': 'Red Hat, Inc. QXL paravirtual graphic card',
                'devices': [
                    {'pci_id': '8086:1237', 'pci_slot': '0000:00:00.0', 'vm_pci_slot': 'pci_0000_00_00_0'},
                    {'pci_id': '8086:7000', 'pci_slot': '0000:00:01.0', 'vm_pci_slot': 'pci_0000_00_01_0'},
                    {'pci_id': '8086:7010', 'pci_slot': '0000:00:01.1', 'vm_pci_slot': 'pci_0000_00_01_1'},
                    {'pci_id': '8086:7113', 'pci_slot': '0000:00:01.3', 'vm_pci_slot': 'pci_0000_00_01_3'},
                ],
                'vendor': None,
                'uses_system_critical_devices': True,
                'critical_reason': 'Critical devices found: 0000:00:01.0',
                'available_to_host': False
            }
        ],
        {},
        False
    ),
])
def test_get_normalized_gpus(all_gpu_info, nvidia_gpus, should_work):
    result = get_normalized_gpu_choices(all_gpu_info, nvidia_gpus, HOST_STATE_READY)
    if should_work:
        assert result[0]['error'] is None
    else:
        assert result[0]['error'] is not None


def gpu_info(vendor, description, pci_id, pci_slot='0000:03:00.0'):
    return {
        'addr': {'pci_slot': pci_slot, 'domain': '0000', 'bus': '03', 'slot': '00'},
        'description': description,
        'devices': [{'pci_id': pci_id, 'pci_slot': pci_slot, 'vm_pci_slot': 'pci_0000_03_00_0'}],
        'vendor': vendor,
        'uses_system_critical_devices': False,
        'critical_reason': 'No critical devices.',
        'available_to_host': True,
    }


def test_amd_gpu_requires_kfd_for_rocm_container_passthrough():
    host_state = HOST_STATE_READY | {'dev_kfd': False}

    result = get_normalized_gpu_choices(
        [gpu_info('AMD', 'AMD Radeon RX 7900 XTX', '1002:744c')],
        {},
        host_state,
    )

    assert result[0]['error'] is None
    assert result[0]['failure_reason'] == 'amd_kfd_missing'
    assert result[0]['readiness']['status'] == 'degraded'
    assert result[0]['readiness']['container_runtime']['amd_rocm_host_devices'] is False


def test_amd_gpu_without_kfd_remains_assignable_for_non_rocm_containers():
    assert gpu_assignment_blocked('amd_kfd_missing') is False


def test_w7900_bar_failure_is_reported_as_pcie_resource_issue():
    host_state = HOST_STATE_READY | {
        'dev_dri_render': False,
        'dev_kfd': False,
        'kernel_messages': "amdgpu 0000:03:00.0: BAR 0 [mem size 0x1000000000 64bit pref]: can't assign; no space",
        'upstream_bridge_pci_slot': '0000:00:01.1',
    }

    result = get_normalized_gpu_choices(
        [gpu_info('AMD', 'AMD Radeon PRO W7900', '1002:7448')],
        {},
        host_state,
    )

    assert result[0]['error'] == 'amd_w7900_bar_rebar_failure'
    assert result[0]['readiness']['failure_reason'] == 'amd_w7900_bar_rebar_failure'
    assert result[0]['os_profile']['profile_id'] == 'amd_w7900_bar_rebar_minimal'
    assert result[0]['os_profile']['kernel_extra_options'] == (
        'pci=realloc=on,big_root_window,resource_alignment=36@0000:00:01.1'
    )
    assert result[0]['os_profile']['append_to'] == 'system.advanced.kernel_extra_options'
    assert result[0]['os_profile']['append_only'] is True
    assert result[0]['os_profile']['auto_apply'] is False
    assert result[0]['os_profile']['requires_reboot'] is True


def test_w7900_bar_profile_requires_manual_bridge_confirmation_when_bridge_unknown():
    host_state = HOST_STATE_READY | {
        'dev_dri_render': False,
        'dev_kfd': False,
        'kernel_messages': "amdgpu 0000:03:00.0: BAR 0 [mem size 0x1000000000 64bit pref]: can't assign; no space",
    }

    result = get_normalized_gpu_choices(
        [gpu_info('AMD', 'AMD Radeon PRO W7900', '1002:7448')],
        {},
        host_state,
    )

    assert result[0]['os_profile']['kernel_extra_options'] == (
        'pci=realloc=on,big_root_window,resource_alignment=36@<upstream_bridge_pci_slot>'
    )
    assert result[0]['os_profile']['manual_bridge_confirmation_required'] is True


def test_w7900_without_bar_log_does_not_get_w7900_profile():
    host_state = HOST_STATE_READY | {
        'dev_dri_render': False,
        'dev_kfd': False,
        'kernel_messages': 'amdgpu 0000:03:00.0: device initialized',
    }

    result = get_normalized_gpu_choices(
        [gpu_info('AMD', 'AMD Radeon PRO W7900', '1002:7448')],
        {},
        host_state,
    )

    assert result[0]['failure_reason'] == 'amd_render_node_missing'
    assert result[0]['os_profile'] is None


def test_w7900_ignores_bar_failure_logged_for_another_pci_device():
    host_state = HOST_STATE_READY | {
        'kernel_messages': "amdgpu 0000:04:00.0: BAR 0 [mem size 0x1000000000 64bit pref]: can't assign; no space",
    }

    result = get_normalized_gpu_choices(
        [gpu_info('AMD', 'AMD Radeon PRO W7900', '1002:7448')],
        {},
        host_state,
    )

    assert result[0]['failure_reason'] is None
    assert result[0]['os_profile'] is None


def test_non_w7900_amd_bar_failure_does_not_get_w7900_profile():
    host_state = HOST_STATE_READY | {
        'dev_dri_render': False,
        'dev_kfd': False,
        'kernel_messages': "amdgpu 0000:03:00.0: BAR 0 [mem size 0x1000000000 64bit pref]: can't assign; no space",
    }

    result = get_normalized_gpu_choices(
        [gpu_info('AMD', 'AMD Radeon RX 7900 XTX', '1002:744c')],
        {},
        host_state,
    )

    assert result[0]['failure_reason'] == 'amd_pcie_bar_resource_failure'
    assert result[0]['os_profile'] is None


def test_intel_arc_is_target_gpu_when_render_node_exists():
    result = get_normalized_gpu_choices(
        [gpu_info('INTEL', 'Intel Arc A770 Graphics', '8086:56a0')],
        {},
        HOST_STATE_READY,
    )

    assert result[0]['error'] is None
    assert result[0]['readiness']['target_accelerator'] is True
    assert 'container-intel-arc' in result[0]['capabilities']


def test_intel_igpu_is_not_target_llm_accelerator():
    result = get_normalized_gpu_choices(
        [gpu_info('INTEL', 'Intel UHD Graphics 770', '8086:4680')],
        {},
        HOST_STATE_READY,
    )

    assert result[0]['error'] is None
    assert result[0]['readiness']['status'] == 'non_target'
    assert result[0]['readiness']['target_accelerator'] is False
