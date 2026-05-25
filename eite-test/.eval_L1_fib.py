import sys
sys.path.insert(0, '/tmp')
from bench_L1_fib import fib
assert fib(0) == 0, 'fib(0) should be 0'
assert fib(1) == 1, 'fib(1) should be 1'
assert fib(10) == 55, 'fib(10) should be 55'
