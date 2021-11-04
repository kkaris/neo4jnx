import csv
import gzip
import json
import numpy as np
from tqdm import tqdm

# See more at:
# https://neo4j.com/docs/cypher-manual/current/syntax/values/
# Integer, Float, String, Boolean,
# TodO: Look at composite types to store the statements list of dicts
#  https://neo4j.com/docs/cypher-manual/current/syntax/values/#composite-types
DEFAULT_TYPE_MAP = {
    "weight": "Float",
    "corr_weight": "Float",
    "z_score": "Float",
    "belief": "Float",
}


class NumPyEncoder(json.JSONEncoder):
    """Handle NumPy types when json-dumping

    Courtesy of:
    https://stackoverflow.com/a/27050186/10478812
    """
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        else:
            return super(NumPyEncoder, self).default(obj)


def get_data_value(data, key):
    val = data.get(key)
    if val is None or val == '':
        return ""
    elif isinstance(val, (list, dict)):
        return json.dumps(val, cls=NumPyEncoder)
    elif isinstance(val, str):
        return val.replace('\n', ' ')
    else:
        return val


def canonicalize(s):
    return s.replace('\n', ' ')


def graph_to_tsv(g, nodes_path, edges_path):
    metadata = sorted(set(key for node, data in g.nodes(data=True)
                          for key in data))
    header = "name:ID", ':LABEL', *metadata
    node_rows = (
        (canonicalize(node), 'Node',
         *[get_data_value(data, key) for key in metadata])
        for node, data in tqdm(g.nodes(data=True), total=len(g.nodes))
    )

    with gzip.open(nodes_path, mode="wt") as fh:
        node_writer = csv.writer(fh, delimiter="\t")  # type: ignore
        node_writer.writerow(header)
        node_writer.writerows(node_rows)

    metadata = sorted(set(key for u, v, data in g.edges(data=True)
                          for key in data))
    edge_rows = (
        (
            canonicalize(u), canonicalize(v), 'Relation',
            *[get_data_value(data, key) for key in metadata],
        )
        for u, v, data in tqdm(g.edges(data=True), total=len(g.edges))
    )

    with gzip.open(edges_path, "wt") as fh:
        edge_writer = csv.writer(fh, delimiter="\t")  # type: ignore
        header = ":START_ID", ":END_ID", ":TYPE", *metadata
        edge_writer.writerow(header)
        edge_writer.writerows(edge_rows)

