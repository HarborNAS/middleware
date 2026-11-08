"""Exercise rendered Assistant nginx locations against a loopback test backend."""
import http.server
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

from test_harbor_assistant_nginx import render


class CapturingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        value=json.dumps({'path':self.path,'headers':dict(self.headers)}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(value)
    def log_message(self,*args):pass


@unittest.skipUnless(shutil.which('nginx'),'Requires the Linux nginx executable')
class AssistantNginxProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage=tempfile.TemporaryDirectory(prefix='harbor-nginx-auth-')
        cls.root=Path(cls.stage.name)
        cls.backend=http.server.ThreadingHTTPServer(('127.0.0.1',0),CapturingHandler)
        cls.thread=threading.Thread(target=cls.backend.serve_forever,daemon=True);cls.thread.start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));cls.port=sock.getsockname()[1]
        text=render()
        locations=re.findall(r'        location (?:= /api/harbor-(?:beacon|gate)|\^~ /api/harbor-(?:beacon|gate)/) \{.*?\n        \}',text,re.S)
        assert len(locations)==4
        blocks='\n'.join(locations).replace('127.0.0.1:8787',f'127.0.0.1:{cls.backend.server_port}')
        configuration=f"daemon off;\nmaster_process off;\npid {cls.root}/nginx.pid;\nerror_log {cls.root}/error.log;\nevents {{worker_connections 64;}}\nhttp {{limit_req_zone $binary_remote_addr zone=harbor_assistant:10m rate=20r/s; access_log off;server {{listen 127.0.0.1:{cls.port};{blocks}}}}}"
        cls.config=cls.root/'nginx.conf';cls.config.write_text(configuration)
        subprocess.run(['nginx','-p',str(cls.root),'-c',str(cls.config),'-t'],check=True,capture_output=True)
        cls.process=subprocess.Popen(['nginx','-p',str(cls.root),'-c',str(cls.config)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        for _ in range(50):
            try:
                with socket.create_connection(('127.0.0.1',cls.port),timeout=.2):break
            except OSError:time.sleep(.02)
        else:raise AssertionError('Isolated nginx did not start')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.communicate(timeout=10)
        cls.backend.shutdown();cls.backend.server_close();cls.thread.join(timeout=5);cls.stage.cleanup()

    def request(self,path):
        req=urllib.request.Request(f'http://127.0.0.1:{self.port}'+path,headers={
            'Host':'customer-nas.example:8080','X-HarborOS-Auth-Token':'one-use-native-user-token',
            'Authorization':'Bearer forged-service-token','X-Harbor-Principal-Id':'forged-admin',
            'X-Harbor-Principal-Roles':'FULL_ADMIN','X-Harbor-Workspace-Id':'another-workspace'})
        with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)

    def test_legacy_prefix_is_mapped_without_leaking_trusted_credentials(self):
        value=self.request('/api/harbor-beacon/models/capabilities?revision=2')
        self.assertEqual(value['path'],'/api/beacon/models/capabilities?revision=2')
        headers={k.lower():v for k,v in value['headers'].items()}
        self.assertEqual(headers['x-harboros-auth-token'],'one-use-native-user-token')
        self.assertEqual(headers['host'],'customer-nas.example:8080')
        for key in ('authorization','x-harbor-principal-id','x-harbor-principal-roles','x-harbor-workspace-id'):
            self.assertNotIn(key,headers)

    def test_explicit_gate_route_and_escaped_preview_path_reach_same_gateway(self):
        path='/api/harbor-gate/api/beacon/knowledge/preview?path=%2Fmnt%2Fphotos%2Fone%20two.jpg'
        self.assertEqual(self.request(path)['path'],path)
        self.assertEqual(self.request('/api/harbor-beacon')['path'],'/api/beacon')

if __name__=='__main__':unittest.main()
