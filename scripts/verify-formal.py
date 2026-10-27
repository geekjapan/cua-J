#!/usr/bin/env python3
import pathlib
import signal
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[1]
version = subprocess.check_output(['z3', '--version'], text=True).strip()
assert version.startswith('Z3 version 5.1.0 '), version
print(version, flush=True)
for broken, expected in [(False, 'unsat'), (True, 'sat')]:
    clauses = ['(set-logic QF_LIA)', '(declare-const live0 Bool)', '(declare-const count0 Int)', '(assert live0)', '(assert (= count0 0))']
    for i in range(4):
        j = i + 1
        clauses += [f'(declare-const op{i} Int)', f'(assert (and (<= 0 op{i}) (< op{i} 10)))', f'(declare-const live{j} Bool)', f'(declare-const count{j} Int)']
        remove = f'(or (= op{i} 5) (= op{i} 6)' + (')' if broken else f' (= op{i} 0))')
        clauses += [f'(assert (= live{j} (and live{i} (not {remove}))))', f'(assert (= count{j} (+ count{i} (ite (and live{i} (= op{i} 0)) 1 0))))']
    clauses += ['(assert (> count4 1))', '(check-sat)']
    result = subprocess.run(['z3', '-in'], input='\n'.join(clauses), text=True, capture_output=True, check=True)
    assert result.stdout.strip() == expected, result.stdout + result.stderr
    print(f'model remove-on-consume={not broken}: {expected}', flush=True)
subprocess.run(['cargo', '+1.95.0', 'test', '--locked', '--manifest-path', str(root / 'libs/cua-driver/rust/Cargo.toml'), '-p', 'cua-driver-core', '--test', 'capture_consumption_bounded'], check=True)
if '--mutation' in sys.argv:
    source = root / 'libs/cua-driver/rust/crates/cua-driver-core/src/capture_registry.rs'
    original = source.read_bytes()
    old = b'self.remove_capture(&mut inner, request.capture_id);\n        Ok(admission)'
    assert original.count(old) == 1
    backup = root / 'artifacts/capture_registry.rs.before-mutation'
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(original)
    def interrupted(signum, frame):
        raise KeyboardInterrupt('mutation interrupted; restoring source')
    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    mutant = original.replace(old, b'Ok(admission)')
    try:
        source.write_bytes(mutant)
        result = subprocess.run(['cargo', '+1.95.0', 'test', '--locked', '--manifest-path', str(root / 'libs/cua-driver/rust/Cargo.toml'), '-p', 'cua-driver-core', '--test', 'capture_consumption_bounded'], capture_output=True, text=True)
        assert result.returncode != 0 and 'trace ' in result.stdout and 'FAILED' in result.stdout, result.stdout + result.stderr
        print(result.stdout)
        print('real-code remove-on-consume mutation detected', flush=True)
    finally:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            assert source.read_bytes() == mutant, f'concurrent source edit detected; recover using {backup}'
            source.write_bytes(original)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    subprocess.run(['cargo', '+1.95.0', 'test', '--locked', '--manifest-path', str(root / 'libs/cua-driver/rust/Cargo.toml'), '-p', 'cua-driver-core', '--test', 'capture_consumption_bounded'], check=True)
