from middlewared.common.ports import PortDelegate
from middlewared.service import Service, private
from middlewared.utils.harborlink_media import (
    HARBORLINK_LOOPBACK_TCP_PORTS,
    HARBORLINK_WEBRTC_UDP_PORT,
    harborlink_media_network_policy,
)


class HarborLinkPortDelegate(PortDelegate):

    name = 'harborlink'
    namespace = 'harborlink'
    title = 'HarborLink Media Service'

    async def get_ports(self):
        return [
            {
                'description': 'HarborLink loopback control/media ports',
                'ports': [('127.0.0.1', port) for port in HARBORLINK_LOOPBACK_TCP_PORTS],
            },
            {
                'description': 'HarborLink LAN-only WebRTC UDP media port',
                'ports': [(wildcard, HARBORLINK_WEBRTC_UDP_PORT) for wildcard in ('0.0.0.0', '::')],
            },
        ]


class HarborLinkService(Service):

    class Config:
        namespace = 'harborlink'
        private = True

    @private
    async def media_network_policy(self, trusted_lan_cidrs=None, candidate_addresses=None):
        """
        Return the HarborOS network contract for HarborLink media.

        Middleware owns the system/network policy: nginx exposes WHEP/HLS on
        80/443, MediaMTX control/data helper ports stay loopback-only, and
        UDP/8189 is allowed only from trusted LAN/management networks.
        """
        return harborlink_media_network_policy(trusted_lan_cidrs, candidate_addresses)


async def setup(middleware):
    await middleware.call('port.register_attachment_delegate', HarborLinkPortDelegate(middleware))
