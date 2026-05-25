import subprocess, sys
r1 = subprocess.run([sys.executable, '/tmp/bench_L2_grep.py', 'import', '/tmp/bench_L2_grep.py'],
    capture_output=True, text=True, timeout=10)
assert 'import' in r1.stdout, f'grep import failed: stdout={r1.stdout!r}'
r2 = subprocess.run([sys.executable, '/tmp/bench_L2_grep.py', '.', '/tmp/bench_L2_grep.py', '-c'],
    capture_output=True, text=True, timeout=10)
assert r2.stdout.strip().isdigit(), f'grep -c should be number: stdout={r2.stdout!r}'
