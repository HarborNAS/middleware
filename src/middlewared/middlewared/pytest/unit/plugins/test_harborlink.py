import importlib.util
from pathlib import Path


def load_harborlink_media_module():
    module_path = Path(__file__).parents[3] / 'utils' / 'harborlink_media.py'
    spec = importlib.util.spec_from_file_location('harborlink_media_policy', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harborlink_media = load_harborlink_media_module()
HARBORLINK_WEBRTC_UDP_PORT = harborlink_media.HARBORLINK_WEBRTC_UDP_PORT
harborlink_media_network_policy = harborlink_media.harborlink_media_network_policy


def test_harborlink_media_network_policy_separates_web_tcp_from_lan_udp():
    policy = harborlink_media_network_policy(
        trusted_lan_cidrs=['192.168.3.0/24', 'fd00::/64'],
        candidate_addresses=['192.168.3.10', 'fd00::10'],
    )

    assert policy['web_api_tcp_entrypoints'] == [80, 443]
    assert policy['webrtc_lan_udp']['port'] == HARBORLINK_WEBRTC_UDP_PORT
    assert policy['webrtc_lan_udp']['protocol'] == 'udp'
    assert policy['webrtc_lan_udp']['scope'] == 'trusted_lan_only'
    assert policy['webrtc_lan_udp']['ipv4_trusted_cidrs'] == ['192.168.3.0/24']
    assert policy['webrtc_lan_udp']['ipv6_trusted_cidrs'] == ['fd00::/64']
    assert policy['webrtc_lan_udp']['candidate_addresses'] == ['192.168.3.10', 'fd00::10']
    assert policy['webrtc_lan_udp']['fallback'] == 'hls_over_80_443'


def test_harborlink_internal_media_ports_are_loopback_only():
    policy = harborlink_media_network_policy()
    internal_ports = {
        entry['port']: entry['bind']
        for entry in policy['internal_loopback_tcp_ports']
    }

    assert internal_ports == {
        8790: '127.0.0.1',
        9997: '127.0.0.1',
        8554: '127.0.0.1',
        8888: '127.0.0.1',
        8889: '127.0.0.1',
    }
