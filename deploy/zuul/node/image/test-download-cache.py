"""Bazel regression: same SHA-256, new canonical ID, read-only shared download.

Runs the node's actual cache setup and Bazel's downloader in a tiny workspace.
No Swift or build farm is used. The payload comes only from localhost. Local
stubs for unused toolchain modules and an empty file registry keep the isolated
probe offline while Bazel's real downloader and cache remain under test.
"""
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import ssl
import struct
import time
import subprocess
import tempfile
import threading


def bazel_environment(environment):
    # BuildKit forwards HTTP(S)_PROXY build args without sshd's NO_PROXY settings.
    # Bazel prefers lowercase no_proxy: merge both spellings and set them alike.
    bypass = dict.fromkeys(entry.strip() for name in ('no_proxy', 'NO_PROXY')
                           for entry in environment.get(name, '').split(',') if entry.strip())
    bypass.update({'127.0.0.1': None, 'localhost': None})
    return {**environment, 'no_proxy': ','.join(bypass), 'NO_PROXY': ','.join(bypass)}


def generate_tls(tls):
    """Create one ephemeral loopback identity and a certificate-only JKS.

    OpenSSL is already in both pinned images (the worker's ca-certificates
    dependency). JKS trusted-certificate entries need only stdlib serialization;
    no JDK/keytool, private-key store or extra downloaded tool is needed.
    """
    tls.mkdir(mode=0o700)
    subprocess.run([
        '/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(tls / 'loopback.key'), '-out', str(tls / 'loopback.crt'),
        '-days', '1', '-subj', '/CN=127.0.0.1',
        '-addext', 'subjectAltName=IP:127.0.0.1',
    ], check=True, capture_output=True, timeout=30,
       env={**os.environ, 'OPENSSL_CONF': '/dev/null'})
    (tls / 'loopback.key').chmod(0o600)
    certificate = ssl.PEM_cert_to_DER_cert((tls / 'loopback.crt').read_text())

    def utf(value):
        encoded = value.encode('ascii')  # All JKS strings here are ASCII.
        return struct.pack('>H', len(encoded)) + encoded

    # JKS v2: magic, version, count; trusted-cert tag, alias, date, type, DER.
    store = (struct.pack('>III', 0xFEEDFEED, 2, 1) + struct.pack('>I', 2)
             + utf('loopback') + struct.pack('>Q', int(time.time() * 1000))
             + utf('X.509') + struct.pack('>I', len(certificate)) + certificate)
    digest = hashlib.sha1('fixture'.encode('utf-16-be') + b'Mighty Aphrodite' + store).digest()
    (tls / 'truststore.jks').write_bytes(store + digest)


def main(observe=None):
    source = Path(__file__).resolve().parent / 'start.sh'
    setup = source.read_text().split('# Shared cache entries', 1)[1].split('exec /usr/sbin/sshd', 1)[0]
    payload = b'bounded node download regression\n'
    sha256 = hashlib.sha256(payload).hexdigest()
    requests = []
    token_requests = []
    enabled = [True]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/token':
                token_requests.append(self.path)
                self.send_error(429)
                return
            requests.append(self.path)
            if not enabled[0]:
                self.send_error(429)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    fixture = source.parent / 'download-cache-fixture'
    work = Path(tempfile.mkdtemp(prefix='zuul-bazel-download-'))
    share = work / 'share'
    server = thread = None
    try:
        tls = work / 'tls'
        generate_tls(tls)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        # Bazel refuses checksum-free plain HTTP. Trust only this temporary
        # loopback certificate in the isolated probe JVMs.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls / 'loopback.crt', tls / 'loopback.key')
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        workspace = work / 'workspace'
        workspace.mkdir()
        (workspace / '.bazelversion').write_text((source.parents[4] / '.bazelversion').read_text())
        # The fixture has no machine-wide rc. Keep this local download test small.
        (workspace / '.bazelrc').write_text('common:local --remote_cache=\ncommon:local --remote_executor=\n')
        (workspace / 'BUILD.bazel').write_text('')
        (workspace / 'download.bzl').write_text('''def _download(ctx):
    # Model the pinned rules_oci token request before its digest-verified download.
    if ctx.attr.token_url:
        token = ctx.download(ctx.attr.token_url, "token.json",
                             allow_fail=ctx.os.environ.get("OCI_GET_TOKEN_ALLOW_FAIL") != None)
        if token.success:
            fail("fixture token endpoint must return 429")
    ctx.download(ctx.attr.url, "payload", sha256=ctx.attr.sha256, canonical_id=ctx.attr.canonical_id)
    ctx.file("BUILD.bazel", "exports_files([\\\"payload\\\"])\\n")

payload_repo = repository_rule(implementation=_download, attrs={
    "url": attr.string(), "sha256": attr.string(), "canonical_id": attr.string(),
    "token_url": attr.string(),
}, environ=["OCI_GET_TOKEN_ALLOW_FAIL"])
''')
        # Bazel's built-in bazel_tools still contributes dependencies when overridden.
        # This probe loads no toolchain: stub its pinned dependencies locally instead.
        overrides = []
        for name in json.loads((fixture / "modules.json").read_text()):
            module = work / "modules" / name
            module.mkdir(parents=True)
            (module / "MODULE.bazel").write_text(f'module(name="{name}")\n')
            (module / "BUILD.bazel").write_text('')
            overrides.append(f'local_path_override(module_name="{name}", path="{module}")\n')
        url = f'https://127.0.0.1:{server.server_port}/v2/fixture/blobs/sha256:{sha256}'

        def fetch(canonical_id, cache, output, distdir=None):
            token_url = f'https://127.0.0.1:{server.server_port}/token' if distdir is not None else ''
            (workspace / 'MODULE.bazel').write_text(
                'module(name="download_probe")\n'
                + ''.join(overrides) +
                'payload_repo = use_repo_rule("//:download.bzl", "payload_repo")\n'
                f'payload_repo(name="payload", url="{url}", sha256="{sha256}", canonical_id="{canonical_id}", token_url="{token_url}")\n')
            node_rc = workspace / 'node.bazelrc'
            node_rc.write_text((source.parent / 'bazelrc').read_text().replace(
                '/home/zuul/.cache/bazel/repository-cache', str(cache)))
            command = [os.environ.get('BAZEL', 'bazelisk'), '--batch', '--nosystem_rc', '--nohome_rc', f'--bazelrc={node_rc}',
                       f'--output_user_root={work / "bazel"}', f'--output_base={work / output}',
                       '--host_jvm_args=-Xmx256m',
                       f'--host_jvm_args=-Djavax.net.ssl.trustStore={tls / "truststore.jks"}',
                       '--host_jvm_args=-Djavax.net.ssl.trustStoreType=JKS',
                       '--host_jvm_args=-Djavax.net.ssl.trustStorePassword=fixture', 'fetch', '--config=local',
                       f'--registry={fixture.joinpath("registry").as_uri()}',
                       '--incompatible_autoload_externally=',
                       '--repo=@payload']
            if distdir is not None:
                command += ['--repository_cache=', '--repo_contents_cache=', f'--distdir={distdir}', '--repo_env=OCI_GET_TOKEN_ALLOW_FAIL=1']
            result = subprocess.run(command, cwd=workspace, env=bazel_environment(os.environ),
                                    text=True, capture_output=True, timeout=120)
            if result.returncode:
                raise AssertionError(f'Bazel fetch {canonical_id} exited {result.returncode}:\n{result.stdout}\n{result.stderr}')
            if observe:
                observe()

        shared_cache = share / 'repository-cache'
        fetch('old-url', shared_cache, 'seed-output')
        assert len(requests) == 1, requests
        shared_hash = shared_cache / 'content_addressable/sha256' / sha256
        original_files = {p.name: p.read_bytes() for p in shared_hash.iterdir()}
        # The full gate uses no writable cache. Model its symlink-only distdir
        # over the same read-only download share, including rules_oci's URL name.
        distdir = share / 'distdir'
        distdir.mkdir()
        (distdir / ('sha256:' + sha256)).symlink_to(shared_hash / 'file')
        for path in sorted(share.rglob('*'), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        share.chmod(0o555)
        home = work / 'home'
        home.mkdir()
        subprocess.run(['bash', '-euc', '# Shared cache entries' + setup.replace('/var/cache/bazel', str(share))],
                       env={**os.environ, 'HOME': str(home)}, check=True)
        private_cache = home / '.cache/bazel/repository-cache'
        private_hash = private_cache / 'content_addressable/sha256' / sha256
        assert private_hash.is_dir() and not private_hash.is_symlink()
        assert (private_hash / 'file').is_symlink()
        enabled[0] = False
        fetch('old-url', private_cache, 'hit-output')
        assert len(requests) == 1, 'a known canonical ID must read the share without HTTP'
        for output in ('gate-output-one', 'gate-output-two'):
            before = len(token_requests)
            fetch('gate-url', private_cache, output, distdir=distdir)
            assert len(token_requests) > before, 'each fresh gate fetch must exercise the failing token endpoint'
        assert len(requests) == 1, 'fresh gate outputs must read pinned blobs without blob HTTP, even on 429'
        print('Bazel: two fresh gate outputs, repository cache disabled, token endpoint 429 tolerated; pinned OCI blob served from read-only distdir without blob HTTP.')
        enabled[0] = True
        fetch('new-url', private_cache, 'miss-output')
        assert len(requests) == 2, 'a new canonical ID must download into the private hash directory'
        assert {p.name: p.read_bytes() for p in shared_hash.iterdir()} == original_files
        assert len(list(private_hash.glob('id-*'))) > len(list(shared_hash.glob('id-*')))
        print('Bazel: shared cache hit without HTTP; changed canonical ID downloaded successfully; read-only share unchanged.')
    finally:
        if thread is not None:
            server.shutdown()
            thread.join(timeout=5)
        if server is not None:
            server.server_close()
        for parent, dirs, files in os.walk(work):
            os.chmod(parent, 0o755)
        shutil.rmtree(work)


if __name__ == '__main__':
    main()
