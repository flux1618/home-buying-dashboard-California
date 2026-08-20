"""Source stations: the only part of the codebase that touches the network.

Per ADR 0002 the scoring core is pure and stdlib-only. Everything that can fail, time
out, change shape, or demand an API key lives here, behind the station contract in
`base.py`, so that failure is a data condition rather than an exception.
"""
