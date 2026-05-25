import sys
sys.path.insert(0, '/tmp')
from bench_L2_sort import quicksort
r = quicksort([3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5])
assert r == sorted([3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]), f'got {r}'
