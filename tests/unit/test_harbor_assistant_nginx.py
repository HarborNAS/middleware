"""Render the actual appliance template without requiring a running NAS."""
from pathlib import Path
import re
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from mako.template import Template

TEMPLATE = Path(__file__).resolve().parents[2] / "src/middlewared/middlewared/etc_files/local/nginx/nginx.conf.mako"

class MiddlewareFixture:
    api_versions = [SimpleNamespace(version="v26.04.0")]
    logger = SimpleNamespace(exception=lambda *a: None)

    def __init__(self, ssl=False, fips=False):
        self.ssl, self.fips = ssl, fips

    def call_sync(self, method, *args):
        values={
            "system.general.config":{"ui_certificate":1 if self.ssl else None,"ui_x_frame_options":"SAMEORIGIN",
                "ui_address":["0.0.0.0"],"ui_v6address":["::"],"ui_httpsprotocols":["TLSv1.2","TLSv1.3"],
                "ui_httpsredirect":self.ssl,"ui_httpsport":443,"ui_port":80,"ui_allowlist":[]},
            "certificate.query":[{"id":1,"certificate_path":"/test/cert.crt","privatekey_path":"/test/key.pem","extensions":[]}],
            "certificate.dhparam":"/test/dh.pem","datastore.config":{"wg_address":None},
            "certificate.cert_services_validation":False,"system.version":"26.04.0-NEXUS-RC1",
            "system.security.info.fips_enabled":self.fips,"vm.get_vm_display_nginx_route":"/vm/display",
            "vm.device.query":[],"tn_connect.config":{"certificate":None,"tnc_base_url":"https://connect.example.com", "account_service_base_url":"https://account.example.com"},
            "tn_connect.hostname.basename_from_cert":None,
        }
        return values.get(method)


def render(ssl=False, fips=False):
    with tempfile.TemporaryDirectory() as directory:
        certificates=ModuleType("middlewared.alert.source.certificates")
        certificates.WebUiCertificateSetupFailedAlert=type("WebUiCertificateSetupFailedAlert",(),{})
        logger=ModuleType("middlewared.logger");logger.NGINX_LOG_PATH=directory
        with patch.dict("sys.modules", {"middlewared.alert.source.certificates":certificates,"middlewared.logger":logger}):
            return Template(filename=str(TEMPLATE)).render(middleware=MiddlewareFixture(ssl,fips)).replace("\r\n", "\n")


class HarborAssistantNginxTests(unittest.TestCase):
    def test_both_public_prefixes_use_verified_gate_not_private_beacon(self):
        for ssl,fips in [(False,False),(True,False),(True,True)]:
            text=render(ssl,fips)
            for location in ('= /api/harbor-beacon','^~ /api/harbor-beacon/','= /api/harbor-gate','^~ /api/harbor-gate/'):
                block=text.split('location '+location+' {',1)[1].split('}',1)[0]
                self.assertIn('proxy_pass http://127.0.0.1:8787',block)
                self.assertNotIn('4174',block);self.assertNotIn('4176',block)
                self.assertIn('X-HarborOS-Auth-Token $http_x_harboros_auth_token',block)
                self.assertIn('proxy_set_header Authorization ""',block)
                self.assertIn('proxy_set_header X-Harbor-Principal-Roles ""',block)
                self.assertIn('proxy_set_header X-Harbor-Workspace-Id ""',block)
                self.assertIn('limit_req_status 429',block)
                self.assertIn('client_max_body_size 32m',block)
            self.assertEqual(text.count('limit_req_zone $binary_remote_addr zone=harbor_assistant'),1)
            self.assertIn('root /usr/share/truenas/webui',text)
            self.assertIn('proxy_pass http://127.0.0.1:6000/api',text)

    def test_prefix_mapping_keeps_gate_prefix_and_preserves_native_routes(self):
        text=render()
        self.assertIn('location ^~ /api/harbor-beacon/ {\n            proxy_pass http://127.0.0.1:8787/api/beacon/;',text)
        self.assertIn('location = /api/harbor-beacon {\n            proxy_pass http://127.0.0.1:8787/api/beacon;',text)
        self.assertIn('location /api/v2.0',text)
        self.assertIn('location /websocket',text)
        self.assertIn('location = /ui/sw.js',text)
        self.assertNotIn('192.168.3.209',text)

if __name__=='__main__':unittest.main()
