"""Unified graph model for wiremap.

Nodes and edges form the single source of truth. Every extractor writes into
this structure; the matcher, risk engine, and viewer all read from it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class NodeType(str, Enum):
    COMPONENT = "component"        # React component / hook
    API_CALL = "api_call"          # a fetch/axios call site in the frontend
    ENDPOINT = "endpoint"          # a backend route (method + path)
    FUNCTION = "function"          # backend function reachable from a handler
    DB_MODEL = "db_model"          # ORM model / table


class EdgeType(str, Enum):
    RENDERS = "renders"            # component -> component
    MAKES_CALL = "makes_call"      # component -> api_call
    HTTP = "http"                  # api_call -> endpoint  (the cross-stack wire)
    CALLS = "calls"                # endpoint/function -> function
    QUERIES = "queries"            # function -> db_model


class Confidence(str, Enum):
    CERTAIN = "certain"      # statically resolved, literal route
    PROBABLE = "probable"    # resolved through one level of indirection
    INFERRED = "inferred"    # heuristic (dynamic URL fragment, base-url guess)


@dataclass
class Node:
    id: str
    type: NodeType
    label: str
    file: str = ""
    line: int = 0
    meta: dict = field(default_factory=dict)
    risk_score: float = 0.0
    risk_flags: list = field(default_factory=list)


@dataclass
class Edge:
    id: str
    source: str
    target: str
    type: EdgeType
    confidence: Confidence = Confidence.CERTAIN
    meta: dict = field(default_factory=dict)
    risk_score: float = 0.0
    risk_flags: list = field(default_factory=list)


@dataclass
class RiskFlag:
    code: str            # e.g. "no_error_handling"
    severity: str        # low | medium | high | critical
    category: str        # quality | contract | operational | security
    message: str
    evidence: str        # file:line + what was seen
    suggestion: str      # the concrete fix


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing:
            return existing
        self.nodes[node.id] = node
        return node

    def add_edge(self, edge: Edge) -> Edge:
        self.edges[edge.id] = edge
        return edge

    def nodes_of(self, ntype: NodeType) -> list[Node]:
        return [n for n in self.nodes.values() if n.type == ntype]

    def edges_of(self, etype: EdgeType) -> list[Edge]:
        return [e for e in self.edges.values() if e.type == etype]

    @staticmethod
    def _append_unique(flags: list, flag: RiskFlag) -> None:
        d = asdict(flag)
        # same code + evidence = same root cause; never stack duplicates
        if not any(f["code"] == d["code"] and f["evidence"] == d["evidence"]
                   for f in flags):
            flags.append(d)

    def flag_node(self, node_id: str, flag: RiskFlag) -> None:
        if node_id in self.nodes:
            self._append_unique(self.nodes[node_id].risk_flags, flag)

    def flag_edge(self, edge_id: str, flag: RiskFlag) -> None:
        if edge_id in self.edges:
            self._append_unique(self.edges[edge_id].risk_flags, flag)

    def to_dict(self) -> dict:
        return {
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [asdict(e) for e in self.edges.values()],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            f.write(self.to_json())
