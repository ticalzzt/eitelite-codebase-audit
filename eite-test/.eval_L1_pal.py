import sys
sys.path.insert(0, '/tmp')
from bench_L1_pal import is_palindrome
assert is_palindrome('racecar'), 'palindrome should return True'
assert not is_palindrome('hello'), 'non-palindrome should return False'
