from typing import Optional, Union, List, Any
import networkx as nx
import neo4j
from neo4j import GraphDatabase
from networkx import NetworkXError


def extract_properties(properties, property_loaders):
    return {k: extract_value(v, property_loaders.get(k))
            for k, v in properties.items()}


def extract_value(v, loader):
    if loader:
        return loader(v)
    else:
        return v


class Neo4jDiGraph(nx.DiGraph):
    def __init__(self, neo4j_url, neo4j_auth, property_loaders,
                 *args, **kwargs):
        self.driver = GraphDatabase.driver(neo4j_url, auth=neo4j_auth)
        self.session = None
        self.property_loaders = property_loaders
        super().__init__(*args, **kwargs)
        self._succ = SuccView(self, dict_like=True)
        self._adj = AdjacencyView(self, dict_like=True)
        self._pred = PredView(self, dict_like=True)

    def __getitem__(self, n):
        query = """
            MATCH ({name: '%s'})-[r:Relation]-(t)
            RETURN r, t
        """ % _clean_name(n)
        res = self.query_tx(query)
        return {r[1]['name']: extract_properties(r[0], self.property_loaders)
                for r in res}

    @property
    def nodes(self):
        # Lazy View creation, like in networkx
        nodes = NodeView(self)
        self.__dict__["nodes"] = nodes
        return nodes

    @property
    def edges(self):
        edges = EdgeView(self)
        self.__dict__["edges"] = edges
        return edges

    out_edges = edges

    @property
    def in_edges(self):
        in_edges = InEdgeView(self)
        self.__dict__["in_edges"] = in_edges
        return in_edges

    @property
    def pred(self):
        return PredView(self)

    def predecessors(self, n):
        try:
            return iter(self.pred[n])
        except KeyError as e:
            raise NetworkXError(f"The node {n} is not in the digraph.") from e

    @property
    def succ(self):
        return SuccView(self)

    def successors(self, n):
        try:
            return iter(self.succ[n])
        except KeyError as e:
            raise NetworkXError(f"The node {n} is not in the digraph.") from e

    def __iter__(self):
        return iter(self.nodes)

    def __contains__(self, n):
        return n in self.nodes

    def __len__(self):
        return len(self.nodes)

    def get_edge_data(self, u, v, default=None):
        try:
            return self.edges[(u, v)]
        except KeyError:
            return default

    def number_of_nodes(self):
        return len(self.nodes)

    def query_tx(self, query: str) -> Union[List[List[Any]], None]:
        """Run a read-only query and return the results.

        Parameters
        ----------
        query :
            The query string to be executed.

        Returns
        -------
        values :
            A list of results where each result is a list of one or more
            objects (typically neo4j nodes or relations).
        """
        tx = self.get_session().begin_transaction()
        try:
            res = tx.run(query)
        except Exception as e:
            tx.close()
            return
        values = res.values()
        tx.close()
        return values

    def get_session(self, renew: Optional[bool] = False) -> neo4j.work.simple.Session:
        """Return an existing session or create one if needed.

        Parameters
        ----------
        renew :
            If True, a new session is created. Default: False

        Returns
        -------
        session
            A neo4j session.
        """
        if self.session is None or renew:
            sess = self.driver.session()
            self.session = sess
        return self.session


class NodeView:
    def __init__(self, graph: Neo4jDiGraph):
        self.graph = graph

    def __iter__(self):
        return iter(self.__call__())

    def __len__(self):
        with self.graph.driver.session() as session:
            query = """MATCH (:Node) RETURN count(*)"""
            return self.graph.query_tx(query)[0][0]

    def __getitem__(self, index):
        try:
            with self.graph.driver.session() as session:
                query = """MATCH (node:Node {`name`: $value }) RETURN node"""
                n = session.run(query, {"value": index}).single()["node"]
                data = {k: n[k] for k in n.keys() if k != 'name'}
                return data
        except TypeError:
            raise KeyError(index)

    def __call__(self, data=False, default=None):
        with self.graph.driver.session() as session:
            query = """MATCH (node:Node) RETURN node"""
            nodes = [r["node"] for r in session.run(query).data()]
            if not data:
                for n in nodes:
                    yield n['name']
            elif isinstance(data, bool):
                for n in nodes:
                    rdata = {k: n[k] for k in n.keys() if k != 'name'}
                    yield n['name'], rdata
            else:
                for n in nodes:
                    yield n['name'], n.get(data, default)

    def __contains__(self, n):
        with self.graph.driver.session() as session:
            query = """MATCH (node:Node {`name`: $value }) RETURN node"""
            n = session.run(query, {"value": n}).single()
            return True if n else False

    def get(self, index, default=None):
        try:
            return self.__getitem__(index)
        except KeyError:
            return default


class EdgeView:
    def __init__(self, graph):
        self.graph = graph

    def __iter__(self):
        return iter(self.__call__())

    def __len__(self):
        query = """MATCH (u:Node)-[r:Relation]->(v:Node)
                   RETURN COUNT(r)"""
        return self.graph.query_tx(query)[0][0]

    def __getitem__(self, edge):
        s, t = edge
        # return lookup of specific edge; This looks exactly like the
        # AtlasView's __getitem__, but here that method is exposed
        # directly so it can be used for graph.edges[(s, t)]
        # Timing ~100 ms

        query = """MATCH (u:Node)-[r:Relation]->(v:Node)
                   WHERE u.name = '%s' AND v.name = '%s'
                   RETURN r""" % (_clean_name(s), _clean_name(t))
        try:
            return extract_properties(self.graph.query_tx(query)[0][0],
                                      self.graph.property_loaders)
        except IndexError:
            raise KeyError(edge)

    def __call__(self, nbunch=None, data=False, default=None):
        # data is bool or attribute name
        # Get query from helper
        query = _edge_view_call_query(nbunch, data)
        with self.graph.driver.session() as session:
            for tup in session.run(query):
                if not data:
                    yield tup['u'], tup['v']
                else:
                    ed = extract_properties(tup[2],
                                            self.graph.property_loaders)
                    if isinstance(data, bool):
                        yd = ed
                    else:
                        yd = ed.get(data, default)
                    yield tup[0], tup[1], yd


class InEdgeView(EdgeView):
    def __init__(self, graph):
        super().__init__(graph)

    def __getitem__(self, edge):
        s, t = edge
        # Reverse edge and call parent
        return super().__getitem__((t, s))

    def __call__(self, nbunch=None, data=False, default=None):
        # data is bool or attribute name
        # Get query from helper
        query = _edge_view_call_query(nbunch, data, reverse=True)
        with self.graph.driver.session() as session:
            for tup in session.run(query):
                if not data:
                    yield tup['u'], tup['v']
                else:
                    ed = extract_properties(tup[2],
                                            self.graph.property_loaders)
                    if isinstance(data, bool):
                        yd = ed
                    else:
                        yd = ed.get(data, default)
                    yield tup[0], tup[1], yd


class AdjacencyView:
    def __init__(self, graph, dict_like=False):
        self.dict_like = dict_like
        self.graph = graph

    def __getitem__(self, n):
        if self.dict_like:
            return AtlasViewDict(self.graph, n, 'out')
        return AtlasView(self.graph, n, 'out')


class PredView(AdjacencyView):
    def __init__(self, graph, dict_like=False):
        super().__init__(graph, dict_like)

    def __getitem__(self, n):
        if self.dict_like:
            return AtlasViewDict(self.graph, n, 'in')
        return AtlasView(self.graph, n, 'in')


class SuccView(AdjacencyView):
    def __init__(self, graph, dict_like=False):
        super().__init__(graph, dict_like)

    def __getitem__(self, n):
        if self.dict_like:
            return AtlasViewDict(self.graph, n, 'out')
        return AtlasView(self.graph, n, 'out')


class AtlasView:
    def __init__(self, graph, n, direction):
        self.graph = graph
        self.n = n
        self.direction = direction

    def __iter__(self):
        if self.direction == 'out':
            query = """MATCH (u:Node)-[r:Relation]->(v:Node)
                    WHERE u.name = '%s'
                    RETURN v.name""" % _clean_name(self.n)
        else:
            query = """MATCH (u:Node)-[r:Relation]->(v:Node)
                    WHERE v.name = '%s'
                    RETURN u.name""" % _clean_name(self.n)
        res = self.graph.query_tx(query)
        for r in res:
            yield r[0]

    def __len__(self):
        if self.direction == 'out':
            query = """MATCH (u:Node)-[r:Relation]->(v:Node)
                       WHERE u.name = '%s'
                       RETURN count(v)""" % _clean_name(self.n)
        else:
            query = """MATCH (u:Node)-[r:Relation]->(v:Node)
                       WHERE v.name = '%s'
                       RETURN count(u)""" % _clean_name(self.n)
        return self.graph.query_tx(query)[0][0]

    def __contains__(self, n):
        return True if self[n] else False

    def __getitem__(self, n):
        if self.direction == 'out':
            return self.relation_from_source_target(self.n, n)
        else:
            return self.relation_from_source_target(n, self.n)

    def relation_from_source_target(self, s, t):
        query = """MATCH (u:Node)-[r:Relation]->(v:Node)
                   WHERE u.name = '%s' AND v.name = '%s'
                   RETURN r""" % (_clean_name(s), _clean_name(t))
        return extract_properties(self.graph.query_tx(query)[0][0],
                                  self.graph.property_loaders)


class AtlasViewDict(AtlasView):
    """An AtlasView with dict like methods and attributes

    This class is used to create dict like methods in order to mimic
    networkx's g._succ, g._pred and g._adj.
    """
    def __init__(self, graph, n, direction):
        super().__init__(graph, n, direction)

    def items(self):
        # Mimic dict items
        if self.direction == 'out':
            query = """MATCH (u:Node)-[r:Relation]->(v:Node)
                    WHERE u.name = '%s'
                    RETURN v.name as v, r""" % _clean_name(self.n)
        else:
            query = """MATCH (u:Node)-[r:Relation]->(v:Node)
                    WHERE v.name = '%s'
                    RETURN u.name as u, r""" % _clean_name(self.n)
        res = self.graph.query_tx(query)
        for n, r in res:
            yield n, extract_properties(r, self.graph.property_loaders)


def _edge_view_call_query(nbunch=None, data=False, reverse=False):
    # If nbunch is None, return all edges, otherwise filter to the ones
    # that are in nbunch
    # If data is None, return the edges without edge data otherwise return
    # just the edges
    if nbunch is None:
        if data:
            return """MATCH (u:Node)-[r:Relation]->(v:Node)
                       RETURN u.name AS u, v.name AS v, r"""
        else:
            return """MATCH (u:Node)-[r:Relation]->(v:Node)
                      RETURN u.name AS u, v.name AS v"""
    else:
        nbunch_str = ",".join(
            [f"'{_clean_name(n)}'" for n in
             (nbunch if isinstance(nbunch, list) else [nbunch])])
        if reverse:
            node_str = "v"
        else:
            node_str = "u"
        if data:
            return """MATCH (u:Node)-[r:Relation]->(v:Node)
                      WHERE %s.name IN [%s]
                      RETURN u.name AS u, v.name AS v, r""" % \
                   (node_str, nbunch_str)

        else:
            return """MATCH (u:Node)-[r:Relation]->(v:Node)
                      WHERE %s.name IN [%s]
                      RETURN u.name AS u, v.name AS v""" % \
                   (node_str, nbunch_str)


def _clean_name(name):
    """Escapes strings used in queries"""
    # Escape backslashes first, otherwise they will be escaped again
    if "\\" in name:
        name = name.replace("\\", r"\\")
    # Now escape anything else
    if "'" in name:
        name = name.replace("'", r"\'")

    return name
