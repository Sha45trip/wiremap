"""Request-body contract checking (ROADMAP-v2 6.2), mirror of 2.4.

Backend request model = a handler param annotated with a bare Pydantic
model. Frontend body object = axios.post/put 2nd arg or fetch
JSON.stringify. Flags: fields sent but not accepted (dropped), and required
fields omitted (422). CERTAIN backend set only; body must be complete.
"""
from wiremap.graph import Graph, NodeType
from wiremap.matcher import match


def _call(graph, url):
    for n in graph.nodes_of(NodeType.API_CALL):
        if n.meta["url"] == url and n.file.endswith("Body.jsx"):
            return n
    raise AssertionError(f"no Body.jsx call for {url}")


class TestBackendRequestModel:
    def test_request_fields_and_required(self, backend_graph):
        ep = backend_graph.nodes["ep:POST /contract/create"]
        assert ep.meta["request_model"] == "ItemCreate"
        assert ep.meta["request_fields"] == ["name", "note", "price", "tag"]
        assert ep.meta["request_required"] == ["name", "price"]

    def test_body_model_found_alongside_path_param(self, backend_graph):
        ep = backend_graph.nodes["ep:PUT /contract/update/{item_id}"]
        assert ep.meta["request_model"] == "ItemCreate"

    def test_non_model_params_do_not_set_request(self, backend_graph):
        # GET handlers with only primitive params have no request model
        assert "request_model" not in backend_graph.nodes[
            "ep:GET /contract/item"].meta


class TestFrontendBody:
    def test_axios_object_keys_captured(self, frontend_graph):
        call = _call(frontend_graph, "/contract/create")
        # the `create` call sends name, price, extra (complete)
        creates = [n for n in frontend_graph.nodes_of(NodeType.API_CALL)
                   if n.meta["url"] == "/contract/create"
                   and n.meta.get("sent_fields") == ["extra", "name", "price"]]
        assert creates and creates[0].meta["sent_complete"] is True

    def test_spread_marks_incomplete(self, frontend_graph):
        spread = [n for n in frontend_graph.nodes_of(NodeType.API_CALL)
                  if n.meta["url"] == "/contract/create"
                  and n.meta.get("sent_complete") is False]
        assert spread
        assert spread[0].meta["sent_fields"] == ["name"]

    def test_fetch_json_stringify_body(self, frontend_graph):
        ghost = [n for n in frontend_graph.nodes_of(NodeType.API_CALL)
                 if n.meta.get("sent_fields") == ["ghost", "name", "price"]]
        assert ghost

    def test_get_calls_have_no_body(self, frontend_graph):
        for n in frontend_graph.nodes_of(NodeType.API_CALL):
            if n.meta["method"] == "GET":
                assert "sent_fields" not in n.meta


class TestRequestContractFlags:
    def _matched(self, frontend_graph, backend_graph):
        g = Graph()
        g.nodes.update(backend_graph.nodes)
        g.nodes.update(frontend_graph.nodes)
        match(g)
        return g

    def _codes_by_url(self, g, url):
        codes = set()
        for n in g.nodes_of(NodeType.API_CALL):
            if n.meta["url"] == url and n.file.endswith("Body.jsx"):
                codes |= {f["code"] for f in n.risk_flags}
        return codes

    def test_extra_field_flags_drop(self, frontend_graph, backend_graph):
        g = self._matched(frontend_graph, backend_graph)
        extra_calls = [n for n in g.nodes_of(NodeType.API_CALL)
                       if n.meta.get("sent_fields") == ["extra", "name",
                                                        "price"]]
        flags = [f for f in extra_calls[0].risk_flags
                 if f["code"] == "request_contract_mismatch"]
        assert flags and "extra" in flags[0]["message"]

    def test_missing_required_flags(self, frontend_graph, backend_graph):
        g = self._matched(frontend_graph, backend_graph)
        # PUT partial sends only name -> price required missing
        put = _call(g, "/contract/update/5")
        flags = [f for f in put.risk_flags
                 if f["code"] == "missing_request_field"]
        assert flags and "price" in flags[0]["message"]

    def test_spread_body_never_flags(self, frontend_graph, backend_graph):
        g = self._matched(frontend_graph, backend_graph)
        spread = [n for n in g.nodes_of(NodeType.API_CALL)
                  if n.meta.get("sent_complete") is False]
        for n in spread:
            codes = {f["code"] for f in n.risk_flags}
            assert "request_contract_mismatch" not in codes
            assert "missing_request_field" not in codes

    def test_exact_body_is_silent(self, frontend_graph, backend_graph):
        g = self._matched(frontend_graph, backend_graph)
        ok = [n for n in g.nodes_of(NodeType.API_CALL)
              if n.meta.get("sent_fields") == ["name", "note", "price"]]
        codes = {f["code"] for f in ok[0].risk_flags}
        assert "request_contract_mismatch" not in codes
        assert "missing_request_field" not in codes

    def test_fetch_extra_field_flags(self, frontend_graph, backend_graph):
        g = self._matched(frontend_graph, backend_graph)
        ghost = [n for n in g.nodes_of(NodeType.API_CALL)
                 if n.meta.get("sent_fields") == ["ghost", "name", "price"]]
        flags = [f for f in ghost[0].risk_flags
                 if f["code"] == "request_contract_mismatch"]
        assert flags and "ghost" in flags[0]["message"]