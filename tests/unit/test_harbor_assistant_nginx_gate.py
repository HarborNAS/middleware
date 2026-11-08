"""Real nginx -> packaged Gate authentication; only middleware/Beacon are fixtures.

Run inside a fresh Linux Docker fixture with nginx and
HARBOR_GATE_TEST_BINARY pointing to the reviewed amd64 Gate executable.
No production service, IM provider or NAS is contacted.
"""
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from test_harbor_assistant_nginx import render

GATE_TOKEN = 'b' * 64
INGRESS_TOKEN = 'a' * 64
WORKSPACE = 'nexus-native-nginx-fixture'


def receive_exact(stream, length):
    value = stream.read(length)
    if len(value) != length:
        raise EOFError('Incomplete local WebSocket fixture frame')
    return value


class NativeAuthFixture(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        if not self.rfile.readline().startswith(b'GET /api/current '):
            return
        headers = {}
        while line := self.rfile.readline():
            if line == b'\r\n':
                break
            name, value = line.decode().split(':', 1)
            headers[name.lower()] = value.strip()
        accept = base64.b64encode(hashlib.sha1(
            (headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()
        ).digest()).decode()
        self.wfile.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                          'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode())
        self.wfile.flush()
        first, second = receive_exact(self.rfile, 2)
        if first & 15 != 1 or not second & 128:
            return
        length = second & 127
        if length == 126:
            length = struct.unpack('>H', receive_exact(self.rfile, 2))[0]
        elif length == 127:
            length = struct.unpack('>Q', receive_exact(self.rfile, 8))[0]
        if length > 16384:
            return
        mask = receive_exact(self.rfile, 4)
        payload = receive_exact(self.rfile, length)
        request = json.loads(bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))
        if request['method'] != 'auth.login_ex' or request['params'][0]['mechanism'] != 'TOKEN_PLAIN':
            return
        token = request['params'][0]['token']
        with self.server.lock:
            repeated = token in self.server.tokens
            self.server.tokens.add(token)
        valid = token.startswith(('admin-', 'readonly-')) and not repeated
        roles = ['FULL_ADMIN', 'SYSTEM_READ'] if token.startswith('admin-') else ['SYSTEM_READ']
        result = {'response_type': 'SUCCESS' if valid else 'EXPIRED', 'user_info': {
            'pw_uid': 42, 'privilege': {'webui_access': True, 'roles': {'$set': roles}}}}
        response = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
        prefix = bytes([129, len(response)]) if len(response) < 126 else b'\x81\x7e' + struct.pack('>H', len(response))
        self.wfile.write(prefix + response)
        self.wfile.flush()


class FixtureAuthServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class BeaconFixture(http.server.BaseHTTPRequestHandler):
    def respond(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        value = {'method': self.command, 'path': self.path,
                 'headers': {key.lower(): val for key, val in self.headers.items()},
                 'body': json.loads(body) if body else None}
        with self.server.lock:
            self.server.requests.append(value)
        payload = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = respond
    do_POST = respond

    def log_message(self, *args):
        pass


@unittest.skipUnless(
    os.environ.get('HARBOR_GATE_TEST_BINARY') and shutil.which('nginx')
    and Path('/.dockerenv').exists(),
    'Requires an isolated Linux Docker fixture with native nginx and reviewed Gate ELF',
)
class AssistantNginxRealGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = Path('/var/lib/harboros-im-gate')
        if cls.state.exists():
            raise unittest.SkipTest('A fresh disposable Gate state directory is required')
        cls.stage = tempfile.TemporaryDirectory(prefix='native-nginx-gate-')
        cls.root = Path(cls.stage.name)
        cls.state.mkdir(parents=True)
        cls.marker = cls.state / '.native-nginx-test-fixture'
        cls.marker.touch()
        cls.auth = FixtureAuthServer(('127.0.0.1', 6000), NativeAuthFixture)
        cls.auth.tokens, cls.auth.lock = set(), threading.Lock()
        cls.beacon = http.server.ThreadingHTTPServer(('127.0.0.1', 4174), BeaconFixture)
        cls.beacon.requests, cls.beacon.lock = [], threading.Lock()
        cls.threads = [threading.Thread(target=server.serve_forever, daemon=True)
                       for server in (cls.auth, cls.beacon)]
        for thread in cls.threads:
            thread.start()
        credentials = cls.root / 'credentials'
        credentials.mkdir(mode=0o700)
        for name, value in [('gate-to-beacon-send', GATE_TOKEN),
                            ('beacon-to-gate-accept-current', INGRESS_TOKEN),
                            ('beacon-to-gate-accept-previous', '')]:
            (credentials / name).write_text(value)
            (credentials / name).chmod(0o400)
        environment = {**os.environ, 'CREDENTIALS_DIRECTORY': str(credentials),
            'HARBORGATE_RUNTIME_PROFILE': 'nexus', 'IM_AGENT_HOST': '127.0.0.1',
            'IM_AGENT_PORT': '8787', 'IM_AGENT_CONTRACT_VERSION': '2.0',
            'IM_AGENT_DATA_DIR': str(cls.state / 'sessions'), 'IM_AGENT_STATE_DIR': str(cls.state),
            'WEIXIN_STATE_DIR': str(cls.state / 'weixin'),
            'FEISHU_MAIL_TOKEN_STATE_PATH': str(cls.state / 'feishu-mail-token.json'),
            'HARBORGATE_DEVICE_SESSION_STATE_DIR': str(cls.state / 'device-sessions'),
            'HARBORBEACON_WEB_API_URL': 'http://127.0.0.1:4174', 'HARBOR_WORKSPACE_ID': WORKSPACE,
            'HARBORGATE_RUST_FEISHU_WEBSOCKET': 'false', 'HARBORGATE_WEIXIN_RUNTIME_ENABLED': 'false'}
        cls.gate_log = (cls.root / 'gate.log').open('wb')
        cls.gate = subprocess.Popen([os.environ['HARBOR_GATE_TEST_BINARY']], env=environment,
                                    stdout=cls.gate_log, stderr=subprocess.STDOUT)
        cls.wait_port(8787)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            cls.port = listener.getsockname()[1]
        text = render()
        locations = re.findall(r'        location (?:= /api/harbor-(?:beacon|gate)|\^~ /api/harbor-(?:beacon|gate)/) \{.*?\n        \}', text, re.S)
        if len(locations) != 4:
            raise AssertionError('Production Assistant proxy locations were not rendered')
        rate = re.search(r'limit_req_zone \$binary_remote_addr zone=harbor_assistant[^;]+;', text).group(0)
        configuration = ('daemon off;\nmaster_process off;\npid ' + str(cls.root / 'nginx.pid') + ';\n'
            'error_log ' + str(cls.root / 'nginx.log') + ';\nevents {worker_connections 64;}\n'
            'http {access_log off;' + rate + 'server {listen 127.0.0.1:' + str(cls.port) + ';'
            + '\n'.join(locations) + '}}')
        config = cls.root / 'nginx.conf'
        config.write_text(configuration)
        subprocess.run(['nginx', '-p', str(cls.root), '-c', str(config), '-t'],
                       check=True, capture_output=True)
        cls.nginx = subprocess.Popen(['nginx', '-p', str(cls.root), '-c', str(config)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.wait_port(cls.port)

    @classmethod
    def wait_port(cls, port):
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    return
            except OSError:
                time.sleep(.02)
        raise AssertionError(f'Isolated service did not start on {port}')

    @classmethod
    def tearDownClass(cls):
        for process in (cls.nginx, cls.gate):
            process.terminate()
            process.wait(timeout=10)
        cls.gate_log.close()
        for server in (cls.auth, cls.beacon):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join(timeout=5)
        if cls.marker.exists():
            shutil.rmtree(cls.state)
        cls.stage.cleanup()

    def request(self, path, token=None, method='GET', body=None):
        headers = {'X-Harbor-Principal-Id': 'forged-admin',
                   'X-Harbor-Principal-Source': 'client', 'X-Harbor-Principal-Roles': 'FULL_ADMIN',
                   'X-Harbor-Workspace-Id': 'wrong-home', 'X-Harbor-User-Id': 'forged-user',
                   'X-Harbor-Edge-Assertion': 'forged-edge', 'Authorization': 'Bearer forged'}
        if token:
            headers['X-HarborOS-Auth-Token'] = token
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}' + path, headers=headers,
            method=method, data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_missing_invalid_and_readonly_tokens_stop_before_beacon(self):
        with self.beacon.lock:
            before = len(self.beacon.requests)
        for prefix in ('/api/harbor-beacon', '/api/harbor-gate/api/beacon'):
            self.assertEqual(self.request(prefix + '/models/endpoints')[0], 401)
            suffix = prefix.replace('/', '-')
            self.assertEqual(self.request(prefix + '/models/endpoints', 'invalid' + suffix)[0], 401)
            self.assertEqual(self.request(prefix + '/models/endpoints', 'readonly-' + suffix)[0], 403)
        with self.beacon.lock:
            self.assertEqual(len(self.beacon.requests), before)

    def test_full_admin_model_selection_replaces_spoofed_identity_and_service_auth(self):
        status, value = self.request('/api/harbor-beacon/models/capabilities/retrieval_answer/selection?workspace_id=attacker',
            'admin-model', 'POST', {'model_id': 'opaque-model'})
        self.assertEqual(status, 200)
        self.assertEqual(value['path'], '/api/models/capabilities/retrieval_answer/selection')
        self.assertEqual(value['body'], {'model_id': 'opaque-model'})
        headers = value['headers']
        self.assertEqual(headers['authorization'], 'Bearer ' + GATE_TOKEN)
        self.assertEqual(headers['x-harbor-principal-id'], 'harboros:uid:42')
        self.assertEqual(headers['x-harbor-principal-source'], 'harboros')
        self.assertEqual(headers['x-harbor-workspace-id'], WORKSPACE)
        self.assertEqual(headers['x-harbor-principal-roles'], 'FULL_ADMIN,SYSTEM_READ')
        self.assertNotIn('x-harboros-auth-token', headers)
        self.assertNotIn('x-harbor-edge-assertion', headers)
        self.assertNotIn('x-harbor-user-id', headers)
        self.assertNotIn('forged-admin', json.dumps(value))

    def test_preview_is_authenticated_and_keeps_encoded_file_path(self):
        status, value = self.request('/api/harbor-beacon/knowledge/preview?path=%2Fmnt%2Fphoto%20one.jpg', 'admin-preview')
        self.assertEqual(status, 200)
        self.assertEqual(value['path'], '/api/knowledge/preview?path=%2Fmnt%2Fphoto+one.jpg')
        self.assertEqual(value['headers']['x-harbor-workspace-id'], WORKSPACE)

    def test_protected_setup_post_uses_real_gate_authentication_and_validation(self):
        path = '/api/harbor-gate/api/setup/feishu/configure'
        self.assertEqual(self.request(path, method='POST', body={})[0], 401)
        self.assertEqual(self.request(path, 'readonly-setup', 'POST', {})[0], 403)
        status, setup = self.request('/api/harbor-gate/api/setup/status', 'admin-setup-status')
        self.assertEqual(status, 200)
        status, result = self.request(path, 'admin-setup-save', 'POST',
            {'session_code': setup['session_code'], 'app_id': '', 'app_secret': ''})
        self.assertEqual(status, 422)
        self.assertFalse(result['success'])


if __name__ == '__main__':
    unittest.main()
