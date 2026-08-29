"""Infrared: learning codes from a remote and sending them back out.

Split from the `ir` backend module because the pieces have very different
dependencies. `normalise.py` and `codes.py` are pure Python -- no hardware,
no daemon -- and are exercised directly in tests. `gateway.py` is the only
module in the whole project that imports `pigpio`, and it is built so that
importing it (or calling into it with no daemon reachable) never raises;
see its docstring for why that matters as much as it does here.
"""
