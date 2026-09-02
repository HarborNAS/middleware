import ipaddress


HARBORLINK_WEBRTC_UDP_PORT = 8189
HARBORLINK_LOOPBACK_TCP_PORTS = {
    8790: 'HarborLink Local Contract v1 API',
    9997: 'MediaMTX Control API',
    8554: 'MediaMTX RTSP ingest/relay',
    8888: 'MediaMTX HLS loopback listener',
    8889: 'MediaMTX WHEP loopback listener',
}
HARBORLINK_EXTERNAL_TCP_ENTRYPOINTS = [80, 443]
DEFAULT_TRUSTED_LAN_CIDRS = [
    '10.0.0.0/8',
    '172.16.0.0/12',
    '192.168.0.0/16',
    'fc00::/7',
    'fe80::/10',
]


def _normalize_cidrs(cidrs):
    normalized = []
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr, strict=False)
        normalized.append(str(network))
    return normalized


def harborlink_media_network_policy(trusted_lan_cidrs=None, candidate_addresses=None):
    cidrs = _normalize_cidrs(trusted_lan_cidrs or DEFAULT_TRUSTED_LAN_CIDRS)
    ipv4_cidrs = [cidr for cidr in cidrs if ipaddress.ip_network(cidr, strict=False).version == 4]
    ipv6_cidrs = [cidr for cidr in cidrs if ipaddress.ip_network(cidr, strict=False).version == 6]
    candidates = [
        str(ipaddress.ip_address(address))
        for address in candidate_addresses or []
        if address
    ]
    return {
        'service': 'harborlink',
        'web_api_tcp_entrypoints': HARBORLINK_EXTERNAL_TCP_ENTRYPOINTS,
        'internal_loopback_tcp_ports': [
            {
                'port': port,
                'bind': '127.0.0.1',
                'description': description,
            }
            for port, description in HARBORLINK_LOOPBACK_TCP_PORTS.items()
        ],
        'webrtc_lan_udp': {
            'port': HARBORLINK_WEBRTC_UDP_PORT,
            'protocol': 'udp',
            'scope': 'trusted_lan_only',
            'ipv4_trusted_cidrs': ipv4_cidrs,
            'ipv6_trusted_cidrs': ipv6_cidrs,
            'candidate_addresses': candidates,
            'wan_default': 'drop',
            'container_public_entrypoint': 'drop',
            'fallback': 'hls_over_80_443',
        },
    }
