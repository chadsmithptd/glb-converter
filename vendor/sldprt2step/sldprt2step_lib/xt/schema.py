"""Base schema SCH_13006 node layouts for the Parasolid XT format.

Field kinds (XT Format Reference 2.1.4): u byte, c char, l logical, n short,
w unicode short, d int, f double, i interval (2f), v vector (3f), b box (6f),
h hvec (only the 3f position is written), p pointer index, t tag (int).
A field with n == VAR is the variable-length tail whose element count precedes
the node index in the stream.

Fields added to the schema after V13 (mesh/polyline/lattice chains, the BODY
owner class change, REGION.owner, INTERSECTION.intersection_data,
TRANSFORM.precision, LIMIT.term_use, ...) are deliberately NOT listed: files
carry them as embedded 'I'/'A' edits relative to this base (spec 2.1.2).
"""
from __future__ import annotations

from typing import Dict, List, NamedTuple

VAR = -1


class Field(NamedTuple):
    name: str
    kind: str
    n: int = 1
    ptr_class: int = 0


def F(name: str, kind: str, n: int = 1, ptr_class: int = 0) -> Field:
    return Field(name, kind, n, ptr_class)


def _common() -> List[Field]:
    return [F("node_id", "d"), F("attributes_features", "p"), F("owner", "p"),
            F("next", "p"), F("previous", "p"), F("geometric_owner", "p"), F("sense", "c")]


NODE_NAMES: Dict[int, str] = {
    10: "ASSEMBLY", 11: "INSTANCE", 12: "BODY", 13: "SHELL", 14: "FACE", 15: "LOOP",
    16: "EDGE", 17: "HALFEDGE", 18: "VERTEX", 19: "REGION", 29: "POINT", 30: "LINE",
    31: "CIRCLE", 32: "ELLIPSE", 38: "INTERSECTION", 40: "CHART", 41: "LIMIT",
    45: "BSPLINE_VERTICES", 50: "PLANE", 51: "CYLINDER", 52: "CONE", 53: "SPHERE",
    54: "TORUS", 56: "BLENDED_EDGE", 59: "BLEND_BOUND", 60: "OFFSET_SURF", 67: "SWEPT_SURF",
    68: "SPUN_SURF", 70: "LIST", 74: "POINTER_LIS_BLOCK", 79: "ATT_DEF_ID", 80: "ATTRIB_DEF",
    81: "ATTRIBUTE", 82: "INT_VALUES", 83: "REAL_VALUES", 84: "CHAR_VALUES", 85: "POINT_VALUES",
    86: "VECTOR_VALUES", 87: "AXIS_VALUES", 88: "TAG_VALUES", 89: "DIRECTION_VALUES",
    90: "FEATURE", 91: "MEMBER_OF_FEATURE", 98: "UNICODE_VALUES", 99: "FIELD_NAMES",
    100: "TRANSFORM", 101: "WORLD", 102: "KEY", 120: "PE_SURF", 121: "INT_PE_DATA",
    122: "EXT_PE_DATA", 124: "B_SURFACE", 125: "SURFACE_DATA", 126: "NURBS_SURF",
    127: "KNOT_MULT", 128: "KNOT_SET", 130: "PE_CURVE", 133: "TRIMMED_CURVE", 134: "B_CURVE",
    135: "CURVE_DATA", 136: "NURBS_CURVE", 137: "SP_CURVE", 141: "GEOMETRIC_OWNER",
    163: "HELIX_SU_FORM", 176: "PART_XMT_BLOCK", 184: "HELIX_CU_FORM", 185: "POLYLINE_DATA",
    189: "PSM_MESH", 190: "INTEGER_TOOTH", 191: "INTEGER_COMB", 192: "VECTOR_TOOTH",
    193: "VECTOR_COMB", 200: "POLYLINE", 201: "MESH", 204: "INTERSECTION_DATA",
    205: "OFFSET_VALUES", 206: "MESH_OFFSET_DATA", 207: "SCHEMA_CHAR_VALUES",
    208: "NEW_NODE_MAP", 209: "MOD_NODE_MAP", 210: "NEW_FIELD_MAP", 211: "SCHEMA_DATA",
    212: "OLD_NODE_MAP", 213: "OLD_FIELD_MAP", 220: "REAL_TOOTH", 221: "REAL_COMB",
    222: "LATTICE", 223: "LATTICE_DATA_IRREGULAR", 224: "GRAPH_COMPACT",
    229: "TRANSFORM_PRECISION",
}

# Layouts of the base schema (SCH_13006). Types absent from this dict are
# "not in the base schema": the file then carries their full definition.
BASE: Dict[int, List[Field]] = {
    10: [F("highest_node_id", "d"), F("attributes_features", "p"), F("attribute_chains", "p"),
         F("list", "p"), F("surface", "p"), F("curve", "p"), F("point", "p"), F("key", "p"),
         F("res_size", "f"), F("res_linear", "f"), F("ref_instance", "p"), F("next", "p"),
         F("previous", "p"), F("state", "u"), F("owner", "p"), F("type", "u"),
         F("sub_instance", "p")],
    11: [F("node_id", "d"), F("attributes_features", "p"), F("type", "u"), F("part", "p"),
         F("transform", "p"), F("assembly", "p"), F("next_in_part", "p"), F("prev_in_part", "p"),
         F("next_of_part", "p"), F("prev_of_part", "p")],
    12: [F("highest_node_id", "d"), F("attributes_features", "p"), F("attribute_chains", "p"),
         F("surface", "p"), F("curve", "p"), F("point", "p"), F("key", "p"), F("res_size", "f"),
         F("res_linear", "f"), F("ref_instance", "p"), F("next", "p"), F("previous", "p"),
         F("state", "u"), F("owner", "p"), F("body_type", "u"), F("nom_geom_state", "u"),
         F("shell", "p"), F("boundary_surface", "p"), F("boundary_curve", "p"),
         F("boundary_point", "p"), F("region", "p"), F("edge", "p"), F("vertex", "p")],
    13: [F("node_id", "d"), F("attributes_features", "p"), F("body", "p"), F("next", "p"),
         F("face", "p"), F("edge", "p"), F("vertex", "p"), F("region", "p"), F("front_face", "p")],
    14: [F("node_id", "d"), F("attributes_features", "p"), F("tolerance", "f"), F("next", "p"),
         F("previous", "p"), F("loop", "p"), F("shell", "p"), F("surface", "p"), F("sense", "c"),
         F("next_on_surface", "p"), F("previous_on_surface", "p"), F("next_front", "p"),
         F("previous_front", "p"), F("front_shell", "p")],
    15: [F("node_id", "d"), F("attributes_features", "p"), F("halfedge", "p"), F("face", "p"),
         F("next", "p")],
    16: [F("node_id", "d"), F("attributes_features", "p"), F("tolerance", "f"), F("halfedge", "p"),
         F("previous", "p"), F("next", "p"), F("curve", "p"), F("next_on_curve", "p"),
         F("previous_on_curve", "p"), F("owner", "p")],
    17: [F("attributes_features", "p"), F("loop", "p"), F("forward", "p"), F("backward", "p"),
         F("vertex", "p"), F("other", "p"), F("edge", "p"), F("curve", "p"), F("next_at_vx", "p"),
         F("sense", "c")],
    18: [F("node_id", "d"), F("attributes_features", "p"), F("halfedge", "p"), F("previous", "p"),
         F("next", "p"), F("point", "p"), F("tolerance", "f"), F("owner", "p")],
    19: [F("node_id", "d"), F("attributes_features", "p"), F("body", "p"), F("next", "p"),
         F("previous", "p"), F("shell", "p"), F("type", "c")],
    29: [F("node_id", "d"), F("attributes_features", "p"), F("owner", "p"), F("next", "p"),
         F("previous", "p"), F("pvec", "v")],
    30: _common() + [F("pvec", "v"), F("direction", "v")],
    31: _common() + [F("centre", "v"), F("normal", "v"), F("x_axis", "v"), F("radius", "f")],
    32: _common() + [F("centre", "v"), F("normal", "v"), F("x_axis", "v"),
                     F("major_radius", "f"), F("minor_radius", "f")],
    38: _common() + [F("surface", "p", 2), F("chart", "p"), F("start", "p"), F("end", "p")],
    40: [F("base_parameter", "f"), F("base_scale", "f"), F("chart_count", "d"),
         F("chordal_error", "f"), F("angular_error", "f"), F("parameter_error", "f", 2),
         F("hvec", "h", VAR)],
    41: [F("type", "c"), F("hvec", "h", VAR)],
    45: [F("vertices", "f", VAR)],
    50: _common() + [F("pvec", "v"), F("normal", "v"), F("x_axis", "v")],
    51: _common() + [F("pvec", "v"), F("axis", "v"), F("radius", "f"), F("x_axis", "v")],
    52: _common() + [F("pvec", "v"), F("axis", "v"), F("radius", "f"), F("sin_half_angle", "f"),
                     F("cos_half_angle", "f"), F("x_axis", "v")],
    53: _common() + [F("centre", "v"), F("radius", "f"), F("axis", "v"), F("x_axis", "v")],
    54: _common() + [F("centre", "v"), F("axis", "v"), F("major_radius", "f"),
                     F("minor_radius", "f"), F("x_axis", "v")],
    56: _common() + [F("blend_type", "c"), F("surface", "p", 2), F("spine", "p"),
                     F("range", "f", 2), F("thumb_weight", "f", 2), F("boundary", "p", 2),
                     F("start", "p"), F("end", "p")],
    59: _common() + [F("boundary", "n"), F("blend", "p")],
    60: _common() + [F("check", "c"), F("true_offset", "l"), F("surface", "p"),
                     F("offset", "f"), F("scale", "f")],
    67: _common() + [F("section", "p"), F("sweep", "v"), F("scale", "f")],
    68: _common() + [F("profile", "p"), F("base", "v"), F("axis", "v"), F("start", "v"),
                     F("end", "v"), F("start_param", "f"), F("end_param", "f"),
                     F("x_axis", "v"), F("scale", "f")],
    70: [F("node_id", "d"), F("owner", "p"), F("next", "p"), F("previous", "p"), F("list_type", "u"),
         F("list_length", "d"), F("block_length", "d"), F("size_of_entry", "d"), F("list_block", "p")],
    74: [F("n_entries", "d"), F("next_block", "p"), F("entries", "p", VAR)],
    79: [F("string", "c", VAR)],
    80: [F("next", "p"), F("identifier", "p"), F("type_id", "d"), F("actions", "u", 8),
         F("field_names", "p"), F("legal_owners", "l", 14), F("fields", "u", VAR)],
    81: [F("node_id", "d"), F("definition", "p"), F("owner", "p"), F("next", "p"), F("previous", "p"),
         F("next_of_type", "p"), F("previous_of_type", "p"), F("fields", "p", VAR)],
    82: [F("values", "d", VAR)],
    83: [F("values", "f", VAR)],
    84: [F("values", "c", VAR)],
    85: [F("values", "v", VAR)],
    86: [F("values", "v", VAR)],
    87: [F("values", "v", VAR)],
    88: [F("values", "t", VAR)],
    89: [F("values", "v", VAR)],
    90: [F("node_id", "d"), F("attributes_features", "p"), F("owner", "p"), F("next", "p"),
         F("previous", "p"), F("type", "u"), F("first_member", "p")],
    91: [F("dummy_node_id", "d"), F("owning_feature", "p"), F("owner", "p"), F("next", "p"),
         F("previous", "p"), F("next_member", "p"), F("previous_member", "p")],
    98: [F("values", "w", VAR)],
    99: [F("names", "p", VAR)],
    100: [F("node_id", "d"), F("owner", "p"), F("next", "p"), F("previous", "p"),
          F("rotation_matrix", "f", 9), F("translation_vector", "v"), F("scale", "f"),
          F("flag", "d"), F("perspective_vector", "v")],
    101: [F("assembly", "p"), F("attribute", "p"), F("body", "p"), F("transform", "p"),
          F("surface", "p"), F("curve", "p"), F("point", "p"), F("alive", "l"), F("attrib_def", "p"),
          F("highest_id", "d"), F("current_id", "d")],
    102: [F("string", "c", VAR)],
    120: _common() + [F("type", "c"), F("data", "p"), F("tf", "p"), F("internal_geom", "p", VAR)],
    121: [F("geom_type", "d"), F("real_array", "p"), F("int_array", "p")],
    122: [F("key", "p"), F("real_array", "p"), F("int_array", "p")],
    124: _common() + [F("nurbs", "p"), F("data", "p")],
    125: [F("original_uint", "i"), F("original_vint", "i"), F("extended_uint", "i"),
          F("extended_vint", "i"), F("self_int", "u"), F("original_u_start", "c"),
          F("original_u_end", "c"), F("original_v_start", "c"), F("original_v_end", "c"),
          F("extended_u_start", "c"), F("extended_u_end", "c"), F("extended_v_start", "c"),
          F("extended_v_end", "c"), F("analytic_form_type", "c"), F("swept_form_type", "c"),
          F("spun_form_type", "c"), F("blend_form_type", "c"), F("analytic_form", "p"),
          F("swept_form", "p"), F("spun_form", "p"), F("blend_form", "p")],
    126: [F("u_periodic", "l"), F("v_periodic", "l"), F("u_degree", "n"), F("v_degree", "n"),
          F("n_u_vertices", "d"), F("n_v_vertices", "d"), F("u_knot_type", "u"), F("v_knot_type", "u"),
          F("n_u_knots", "d"), F("n_v_knots", "d"), F("rational", "l"), F("u_closed", "l"),
          F("v_closed", "l"), F("surface_form", "u"), F("vertex_dim", "n"),
          F("bspline_vertices", "p"), F("u_knot_mult", "p"), F("v_knot_mult", "p"),
          F("u_knots", "p"), F("v_knots", "p")],
    127: [F("mult", "n", VAR)],
    128: [F("knots", "f", VAR)],
    130: _common() + [F("type", "c"), F("data", "p"), F("tf", "p"), F("internal_geom", "p", VAR)],
    133: _common() + [F("basis_curve", "p"), F("point_1", "v"), F("point_2", "v"),
                      F("parm_1", "f"), F("parm_2", "f")],
    134: _common() + [F("nurbs", "p"), F("data", "p")],
    135: [F("self_int", "u"), F("analytic_form", "p")],
    136: [F("degree", "n"), F("n_vertices", "d"), F("vertex_dim", "n"), F("n_knots", "d"),
          F("knot_type", "u"), F("periodic", "l"), F("closed", "l"), F("rational", "l"),
          F("curve_form", "u"), F("bspline_vertices", "p"), F("knot_mult", "p"), F("knots", "p")],
    137: _common() + [F("surface", "p"), F("b_curve", "p"), F("original", "p"),
                      F("tolerance_to_original", "f")],
    141: [F("owner", "p"), F("next", "p"), F("previous", "p"), F("shared_geometry", "p")],
    163: [F("axis_pt", "v"), F("axis_dir", "v"), F("hand", "c"), F("turns", "i"), F("pitch", "f"),
          F("gap", "f"), F("tol", "f")],
    184: [F("axis_pt", "v"), F("axis_dir", "v"), F("point", "v"), F("hand", "c"), F("turns", "i"),
          F("pitch", "f"), F("tol", "f")],
}

# Bytes per element for fixed-size kinds (neutral binary, big-endian).
KIND_SIZE = {"u": 1, "c": 1, "l": 1, "n": 2, "w": 2, "d": 4, "t": 4, "f": 8, "i": 16, "v": 24,
             "b": 48, "h": 24}

NULL_INT = -32764
NULL_DOUBLE = -3.14158e13


def is_variable(fields: List[Field]) -> bool:
    return bool(fields) and fields[-1].n == VAR
