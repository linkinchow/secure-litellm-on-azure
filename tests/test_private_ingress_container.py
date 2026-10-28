import json
import copy
import http.client
import base64
import hashlib
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import yaml
import websocket

from scripts.customer_migration import ROOT, private_write
from scripts.private_ingress import certificate_material, preserve_native_front_door_binding, render_ingress
from scripts.private_ingress_runtime import verify_endpoint
from tests.test_private_ingress_runtime import ingress_config


@unittest.skipUnless(os.environ.get("RUN_PRIVATE_INGRESS_CONTAINER_TESTS") == "1", "Explicit local Docker test opt-in required")
class PrivateIngressContainerTests(unittest.TestCase):
    def docker(self, *arguments):
        result = subprocess.run(["docker", *arguments], check=True, capture_output=True, text=True, timeout=120)
        return result.stdout.strip()

    def test_locked_image_loads_rendered_config_and_isolates_tls_hosts(self):
        config = ingress_config()
        lock = json.loads((ROOT / "deploy/private-ingress-image.json").read_text())
        image = lock["source"] + "@" + lock["digest"]
        network = "llmgw-test-" + uuid4().hex
        self.docker("network", "create", "--internal", network)
        self.addCleanup(self.docker, "network", "rm", network)
        for plane, other in (("api", "admin"), ("admin", "api")):
            with self.subTest(plane=plane), tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
                root = Path(directory)
                root.chmod(0o755)
                (root / "dynamic").mkdir(mode=0o755)
                (root / "certs").mkdir(mode=0o755)
                host = f"llm-{plane}.customer.invalid"
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
                now = datetime.now(timezone.utc)
                certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30)).add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False).sign(key, hashes.SHA256())
                certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
                private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
                material = certificate_material(certificate_pem + private_pem, host)
                objects = {document["kind"]: document for document in render_ingress(config, plane, image, ["10.30.0.0/16"])}
                files = {root / "dynamic/routes.yaml": objects["ConfigMap"]["data"]["routes.yaml"], root / "certs/tls.crt": certificate_pem, root / "certs/tls.key": private_pem}
                for path, value in files.items():
                    private_write(path, value)
                    path.chmod(0o644)
                container = "llmgw-test-" + uuid4().hex
                arguments = objects["Deployment"]["spec"]["template"]["spec"]["containers"][0]["args"]
                self.docker("run", "--detach", "--name", container, "--network", network, "--user", "65532:65532", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--mount", f"type=bind,src={root / 'dynamic'},dst=/dynamic,readonly", "--mount", f"type=bind,src={root / 'certs'},dst=/certs,readonly", image, *arguments)
                try:
                    inspected = json.loads(self.docker("inspect", container))[0]
                    self.assertFalse(inspected["HostConfig"]["PortBindings"])
                    address = (inspected["NetworkSettings"]["Networks"][network]["IPAddress"], 8443)
                    context = ssl.create_default_context(cadata=certificate_pem)
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            with socket.create_connection(address, timeout=1) as connection, context.wrap_socket(connection, server_hostname=host):
                                break
                        except (OSError, ssl.SSLError):
                            if time.monotonic() >= deadline:
                                self.fail("Traefik did not become TLS-ready: " + self.docker("logs", container))
                    original_connection = socket.create_connection
                    with patch("scripts.private_ingress_runtime.socket.create_connection", side_effect=lambda _address, timeout: original_connection(address, timeout=timeout)), patch("scripts.private_ingress_runtime.ssl.create_default_context", return_value=context):
                        verify_endpoint(address[0], host, f"llm-{other}.customer.invalid", material["sha256"])
                    with socket.create_connection(address, timeout=5) as connection, self.assertRaises(ssl.SSLError):
                        context.wrap_socket(connection, server_hostname=f"llm-{other}.customer.invalid")
                    routes = yaml.safe_load(files[root / "dynamic/routes.yaml"])
                    self.assertEqual(set(routes["http"]["routers"]), {plane})
                finally:
                    self.docker("rm", "--force", container)

    def test_native_api_requires_front_door_header_only_on_inference_routes(self):
        config = ingress_config()
        config["application"] = {"authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        lock = json.loads((ROOT / "deploy/private-ingress-image.json").read_text())
        image = lock["source"] + "@" + lock["digest"]
        network = "llmgw-native-" + uuid4().hex
        self.docker("network", "create", "--internal", network)
        self.addCleanup(self.docker, "network", "rm", network)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            root = Path(directory)
            root.chmod(0o755)
            (root / "dynamic").mkdir(mode=0o755)
            (root / "certs").mkdir(mode=0o755)
            host = "llm-api.customer.invalid"
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
            now = datetime.now(timezone.utc)
            certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30)).add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False).sign(key, hashes.SHA256())
            certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
            private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            documents = render_ingress(config, "api", image, ["10.30.0.0/16"])
            current = copy.deepcopy(next(item for item in documents if item["kind"] == "ConfigMap"))
            identifier = "11111111-1111-4111-8111-111111111111"
            dynamic = yaml.safe_load(current["data"]["routes.yaml"])
            dynamic["http"]["routers"]["api"]["rule"] += f" && HeaderRegexp(`X-Azure-FDID`, `(?i)^{identifier}$`)"
            current["data"]["routes.yaml"] = yaml.safe_dump(dynamic, sort_keys=False)
            documents = preserve_native_front_door_binding(config, documents, current)
            objects = {document["kind"]: document for document in documents}
            backend_requests = []
            websocket_frames = []
            class BackendHandler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"
                def respond(self, status):
                    backend_requests.append((self.command, self.path, self.headers.get("Host"), self.headers.get("X-Forwarded-Proto"), self.headers.get("X-Forwarded-Host")))
                    body = f"{self.command} {self.path} host={self.headers.get('Host')}".encode("ascii")
                    self.send_response(status)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    if self.headers.get("Upgrade", "").lower() == "websocket":
                        accept = base64.b64encode(hashlib.sha1(
                            (self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                        ).digest()).decode()
                        self.send_response(101)
                        self.send_header("Upgrade", "websocket")
                        self.send_header("Connection", "Upgrade")
                        self.send_header("Sec-WebSocket-Accept", accept)
                        self.end_headers()
                        self.connection.settimeout(5)
                        for _ in range(2):
                            header = self.rfile.read(2)
                            if len(header) != 2 or header[0] != 0x81 or not header[1] & 0x80:
                                return
                            length = header[1] & 0x7f
                            if length > 125:
                                return
                            mask = self.rfile.read(4)
                            payload = self.rfile.read(length)
                            if len(mask) != 4 or len(payload) != length:
                                return
                            message = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
                            websocket_frames.append(message.decode())
                            self.wfile.write(bytes((0x81, length)) + message)
                            self.wfile.flush()
                        self.close_connection = True
                        return
                    self.respond(200)

                def do_POST(self):
                    self.respond(201)

                def log_message(self, _format, *_arguments):
                    pass

            network_state = json.loads(self.docker("network", "inspect", network))[0]
            gateway = network_state["IPAM"]["Config"][0]["Gateway"]
            backend = ThreadingHTTPServer((gateway, 0), BackendHandler)
            backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
            backend_thread.start()
            routes = yaml.safe_load(objects["ConfigMap"]["data"]["routes.yaml"])
            routes["http"]["services"]["api"]["loadBalancer"]["servers"] = [{"url": f"http://{gateway}:{backend.server_port}"}]
            objects["ConfigMap"]["data"]["routes.yaml"] = yaml.safe_dump(routes, sort_keys=False)
            for path, value in ((root / "dynamic/routes.yaml", objects["ConfigMap"]["data"]["routes.yaml"]), (root / "certs/tls.crt", certificate_pem), (root / "certs/tls.key", private_pem)):
                private_write(path, value)
                path.chmod(0o644)
            container = "llmgw-native-" + uuid4().hex
            arguments = objects["Deployment"]["spec"]["template"]["spec"]["containers"][0]["args"]
            self.docker("run", "--detach", "--name", container, "--network", network, "--user", "65532:65532", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--mount", f"type=bind,src={root / 'dynamic'},dst=/dynamic,readonly", "--mount", f"type=bind,src={root / 'certs'},dst=/certs,readonly", image, *arguments)
            try:
                address = (json.loads(self.docker("inspect", container))[0]["NetworkSettings"]["Networks"][network]["IPAddress"], 8443)
                context = ssl.create_default_context(cadata=certificate_pem)
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with socket.create_connection(address, timeout=1) as connection, context.wrap_socket(connection, server_hostname=host):
                            break
                    except (OSError, ssl.SSLError):
                        self.assertLess(time.monotonic(), deadline, "Native Traefik config did not become ready")

                def response(method, path, headers=None):
                    headers = headers or {}
                    with socket.create_connection(address, timeout=5) as connection, context.wrap_socket(connection, server_hostname=host) as secured:
                        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Content-Length: 0", *[f"{key}: {value}" for key, value in headers.items()], "Connection: close", "", ""]
                        secured.sendall("\r\n".join(lines).encode("ascii"))
                        response = http.client.HTTPResponse(secured, method=method)
                        response.begin()
                        return response.status, response.read().decode("ascii")

                self.assertEqual(response("POST", "/v1/chat/completions")[0], 404)
                self.assertEqual(response("POST", "/v1/chat/completions", {"X-Azure-FDID": identifier, "X-Forwarded-Proto": "http"}), (201, f"POST /v1/chat/completions host={host}"))
                self.assertEqual(response("GET", "/v1/chat/completions", {"X-Azure-FDID": identifier})[0], 404)
                self.assertEqual(response("POST", "/key/generate", {"X-Azure-FDID": identifier})[0], 404)
                self.assertEqual(response("GET", "/readyz"), (200, f"GET /health/readiness host={host}"))
                # Model discovery: GET only, exact paths, query preserved.
                self.assertEqual(response("GET", "/v1/models?client_version=0.165.0", {"X-Azure-FDID": identifier}), (200, f"GET /v1/models?client_version=0.165.0 host={host}"))
                self.assertEqual(response("GET", "/models", {"X-Azure-FDID": identifier}), (200, f"GET /models host={host}"))
                self.assertEqual(response("GET", "/v1/models")[0], 404)
                for method, path in (("POST", "/v1/models"), ("POST", "/models"), ("HEAD", "/v1/models"), ("DELETE", "/v1/models"), ("GET", "/v1/models/coding"), ("GET", "/models/coding"), ("GET", "/v1/model/info")):
                    self.assertEqual(response(method, path, {"X-Azure-FDID": identifier})[0], 404, (method, path))
                self.assertEqual(backend_requests, [("POST", "/v1/chat/completions", host, "https", host), ("GET", "/health/readiness", host, "https", host), ("GET", "/v1/models?client_version=0.165.0", host, "https", host), ("GET", "/models", host, "https", host)])
                handshake = {"Upgrade": "websocket", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                             "Sec-WebSocket-Version": "13", "Connection": "Upgrade", "X-Azure-FDID": identifier}
                # The normal HTTP helper adds Connection: close, so use it only for rejection cases.
                for path, headers in (
                    ("/v1/responses", {"X-Azure-FDID": identifier}),
                    ("/responses", {"X-Azure-FDID": identifier}),
                    ("/v1/chat/completions", handshake),
                    ("/v1/models/coding", handshake),
                    ("/key/generate", handshake),
                    ("/v1/responses", {**handshake, "X-Azure-FDID": "22222222-2222-4222-8222-222222222222"}),
                    ("/v1/responses", {key: value for key, value in handshake.items() if key != "X-Azure-FDID"}),
                    ("/v1/responses", {**handshake, "Upgrade": "not-websocket"}),
                    ("/v1/responses", {**handshake, "Connection": "not-upgrade"}),
                ):
                    self.assertEqual(response("GET", path, headers)[0], 404, (path, headers))
                for path in ("/responses", "/v1/responses"):
                    with socket.create_connection(address, timeout=5) as connection:
                        secured = context.wrap_socket(connection, server_hostname=host)
                        client = websocket.create_connection(
                            f"wss://{host}{path}", socket=secured, timeout=5,
                            header={"X-Azure-FDID": identifier}, suppress_origin=True,
                            connection="Connection: keep-alive,\tUpgrade",
                        )
                        try:
                            self.assertEqual(client.getstatus(), 101)
                            for turn in ("first-frame", "second-frame"):
                                client.send(turn)
                                self.assertEqual(client.recv(), turn)
                        finally:
                            client.close()
                self.assertEqual(websocket_frames, ["first-frame", "second-frame"] * 2)
            finally:
                self.docker("rm", "--force", container)
                backend.shutdown()
                backend.server_close()
                backend_thread.join(timeout=5)