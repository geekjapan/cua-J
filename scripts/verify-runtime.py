#!/usr/bin/env python3
import hashlib
import json
import math
import os
import pathlib
import signal
import subprocess
import tempfile
import time

root = pathlib.Path(__file__).resolve().parents[1]
binary = root / 'libs/cua-driver/rust/target/debug/cua-driver'
evidence = root / 'artifacts/runtime'
evidence.mkdir(parents=True, exist_ok=True)
assert binary.is_file(), 'Build cua-driver first'
with tempfile.TemporaryDirectory(prefix='cua-j-') as scratch:
    state = pathlib.Path(scratch)
    env = dict(os.environ, CUA_DRIVER_RS_HOME=str(state / 'driver'), CUA_HOME=str(state / 'cua'), CUA_DRIVER_RS_TELEMETRY_ENABLED='0', CUA_TELEMETRY='0', DO_NOT_TRACK='1')
    socket = str(state / 'driver.sock')
    def run(name, *args):
        result = subprocess.run([str(binary), *args], env=env, capture_output=True, text=True, timeout=20)
        (evidence / f'{name}.json').write_text(json.dumps({'argv': [str(binary), *args], 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}, indent=2)+'\n')
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout
    run('version', '--version')
    tools = run('tools', 'list-tools')
    assert 'get_screen_size' in tools
    schema_text = run('schema', 'describe', 'get_screen_size')
    schema = json.loads(schema_text.split('input_schema:\n', 1)[1])
    assert schema['type'] == 'object' and 'properties' in schema
    with (evidence / 'daemon.log').open('w') as log:
        daemon = subprocess.Popen([str(binary), 'serve', '--socket', socket, '--pid-file', str(state / 'daemon.pid'), '--no-overlay', '--no-permissions-gate'], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while not pathlib.Path(socket).exists():
                assert daemon.poll() is None, 'daemon exited before readiness'
                assert time.monotonic() < deadline, 'daemon readiness timeout'
                time.sleep(0.1)
            run('status', 'status', '--socket', socket, '--pid-file', str(state / 'daemon.pid'))
            output = run('screen', 'call', 'get_screen_size', '{}', '--socket', socket)
            response = json.loads(output)
            assert not response.get('isError', False), output
            dimensions = response.get('structuredContent', response)
            if 'content' in response and 'width' not in dimensions:
                dimensions = json.loads(next(item['text'] for item in response['content'] if item.get('type') == 'text'))
            assert dimensions['width'] > 0 and dimensions['height'] > 0, dimensions
            assert math.isfinite(dimensions['scale_factor']) and dimensions['scale_factor'] > 0, dimensions
            (evidence / 'result.json').write_text(json.dumps({'pid': daemon.pid, 'dimensions': dimensions, 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}, indent=2)+'\n')
        finally:
            if daemon.poll() is None:
                os.killpg(daemon.pid, signal.SIGTERM)
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(daemon.pid, signal.SIGKILL)
                    daemon.wait(timeout=5)
            deadline = time.monotonic() + 10
            while True:
                groups = subprocess.check_output(['ps', '-axo', 'pgid='], text=True)
                if str(daemon.pid) not in groups.split():
                    break
                if time.monotonic() >= deadline:
                    os.killpg(daemon.pid, signal.SIGKILL)
                    raise AssertionError('owned process group did not exit before cleanup deadline')
                time.sleep(0.1)
            (evidence / 'cleanup.json').write_text(json.dumps({'owned_pid': daemon.pid, 'returncode': daemon.returncode, 'process_group_absent': True})+'\n')
assert not state.exists(), 'scratch state remains'
assert (evidence / 'result.json').is_file(), 'evidence lost during cleanup'
print('PASS: catalog, schema, isolated daemon, native screen dimensions, owned-process cleanup; evidence in artifacts/runtime')
