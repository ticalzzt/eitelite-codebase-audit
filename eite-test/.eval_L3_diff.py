import subprocess, sys
r = subprocess.run([sys.executable, '/tmp/bench_L3_diff.py', '/tmp/bench_L3_a.txt', '/tmp/bench_L3_b.txt'],
    capture_output=True, text=True, timeout=10)
assert '-world' in r.stdout or '+python' in r.stdout, f'diff output: {r.stdout[:200]}'
