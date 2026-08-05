"""The agent's judgment layer: the version-controlled system prompt and the
decision core that both the CLI scripts and the deployed Lambda import.

A package (rather than loose scripts) so imports are ordinary and
location-independent -- the earlier path-relative importlib load only worked from
the repo root and resolved to a non-existent path inside a Lambda.
"""
