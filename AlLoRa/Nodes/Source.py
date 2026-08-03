"""Deprecated name: `Source` is now `Edge` (named by placement, not data direction).

The class survives as an alias so a year of student code, examples and firmware keep
importing and running unchanged; new code and docs say Edge. The whole machinery lives on
`Node`, which an Edge presets to the "source" home role.
"""
from AlLoRa.Nodes.Edge import Edge


class Source(Edge):
    pass
