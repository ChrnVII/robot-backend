from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

import networkx as nx


class Topology:
    """Versioned RFID topology planner. It never sends motion commands."""

    def __init__(self, version: str, nodes: Iterable[Mapping[str, Any]], edges: Iterable[Mapping[str, Any]]):
        self.version = version
        self.graph = nx.DiGraph()
        for node in nodes:
            self.graph.add_node(str(node["id"]), **{key: value for key, value in node.items() if key != "id"})
        for edge in edges:
            if edge.get("blocked", False):
                continue
            self.graph.add_edge(str(edge["from"]), str(edge["to"]), distance=float(edge.get("distance", 1.0)),
                                speed=float(edge.get("speed", 0.1)), conditions=dict(edge.get("conditions", {})))

    def plan(self, start: str, destination: str) -> List[str]:
        return list(nx.shortest_path(self.graph, start, destination, weight="distance"))

