"""Project a convex, prismatic room into an elevation or section proposal.

Coordinates are in Revit internal feet. A footprint is a sequence of unique
vertices without a repeated closing vertex. The first vertex and edge order
come from the source snapshot. A cut line defines a vertical plane and its
explicit look direction determines which half of the room supplies depth.
Native containment, visibility, and crop verification are separate steps.
"""

import math


TOLERANCE = 1e-7
MAX_INTERSECTION_PAIRS = 20_000


def point(values, size):
    if not isinstance(values, (list, tuple)) or len(values) != size:
        raise ValueError("Expected a point with %d coordinates." % size)
    if any(
        type(value) not in (int, float) or abs(value) > 1e9 or not math.isfinite(value)
        for value in values
    ):
        raise ValueError("Expected finite coordinates within the supported range.")
    return tuple(float(value) for value in values)


def difference(a, b):
    return a[0] - b[0], a[1] - b[1]


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def unit(vector):
    length = math.hypot(*vector)
    if length <= TOLERANCE:
        raise ValueError("Room edges and cut lines must have measurable length.")
    return vector[0] / length, vector[1] / length


def left(vector):
    return -vector[1], vector[0]


def along(origin, direction, distance):
    return origin[0] + direction[0] * distance, origin[1] + direction[1] * distance


def footprint(vertices, max_pairs=MAX_INTERSECTION_PAIRS):
    """Return the measured vertices, inward normals, and cost of validation."""
    if not isinstance(vertices, (tuple, list)) or len(vertices) < 3:
        raise ValueError("A room needs at least three footprint vertices.")
    count = len(vertices)
    pairs = count * (count - 2)
    if pairs > max_pairs:
        raise ValueError("Room geometry exceeds the intersection-pair limit.")
    vertices = tuple(point(vertex, 2) for vertex in vertices)
    tangents = [
        unit(difference(vertices[(index + 1) % count], vertex))
        for index, vertex in enumerate(vertices)
    ]
    first_turn = dot(left(tangents[0]), tangents[1])
    orientation = 1.0 if first_turn > 0 else -1.0
    normals = tuple(along((0, 0), left(tangent), orientation) for tangent in tangents)
    for index, (start, inward) in enumerate(zip(vertices, normals)):
        end_index = (index + 1) % count
        for other_index, other in enumerate(vertices):
            if other_index in (index, end_index):
                continue
            if dot(difference(other, start), inward) <= TOLERANCE:
                raise ValueError("Expected one convex footprint with distinct straight sides.")
    return vertices, normals, pairs


def elevation(vertices, normals, side_index, heights, padding):
    """Return a marker and nominal crop facing one selected finish side."""
    if type(side_index) is not int or not 0 <= side_index < len(vertices):
        raise ValueError("The selected side must be a valid footprint edge index.")
    bottom, top = heights
    start = vertices[side_index]
    end = vertices[(side_index + 1) % len(vertices)]
    inward = normals[side_index]
    center = along(start, difference(end, start), 0.5)
    distances = []
    for vertex, normal in zip(vertices, normals):
        rate = dot(inward, normal)
        if rate < -TOLERANCE:
            clearance = dot(difference(center, vertex), normal)
            distances.append(clearance / -rate)
    if not distances or min(distances) <= 2 * TOLERANCE:
        raise ValueError("The selected side has no measurable interior marker clearance.")
    distance = min(distances) / 2
    origin = along(center, inward, distance)
    if any(dot(difference(origin, vertex), normal) <= TOLERANCE
           for vertex, normal in zip(vertices, normals)):
        raise ValueError("The proposed marker is outside the supported room interior.")
    look = (-inward[0], -inward[1])
    return project(origin, look, (start, end), bottom, top, padding, distance)


def section(vertices, cut, look_vector, heights, padding):
    """Return the room slice and forward extent of an explicit section plane."""
    if not isinstance(cut, (tuple, list)) or len(cut) != 2:
        raise ValueError("A section needs two distinct cut line endpoints.")
    start, end = (point(endpoint, 2) for endpoint in cut)
    tangent = unit(difference(end, start))
    requested = unit(point(look_vector, 2))
    if abs(dot(tangent, requested)) > TOLERANCE:
        raise ValueError("The section look direction must be perpendicular to its cut line.")
    normal = left(tangent)
    look = along((0, 0), normal, 1 if dot(normal, requested) > 0 else -1)
    depths = [dot(difference(vertex, start), look) for vertex in vertices]
    if min(depths) >= -TOLERANCE or max(depths) <= TOLERANCE:
        raise ValueError("The section plane must cross the room interior.")

    crossings = []
    for index, depth in enumerate(depths):
        following = (index + 1) % len(vertices)
        next_depth = depths[following]
        if abs(depth) <= TOLERANCE:
            crossings.append(vertices[index])
        elif (depth < -TOLERANCE and next_depth > TOLERANCE
              or depth > TOLERANCE and next_depth < -TOLERANCE):
            fraction = depth / (depth - next_depth)
            crossings.append(along(vertices[index], difference(vertices[following], vertices[index]), fraction))
    if len(crossings) != 2 or math.hypot(*difference(*crossings)) <= TOLERANCE:
        raise ValueError("The section must produce one measurable room slice.")
    line_length = math.hypot(*difference(end, start))
    slice_offsets = [dot(difference(crossing, start), tangent) for crossing in crossings]
    if min(slice_offsets) < -TOLERANCE or max(slice_offsets) > line_length + TOLERANCE:
        raise ValueError("The reviewed cut line must span the complete room slice.")

    origin = along(crossings[0], difference(crossings[1], crossings[0]), 0.5)
    return project(origin, look, crossings, *heights, padding, max(depths))


def project(origin, look, crop_points, bottom, top, padding, depth):
    bottom, top, padding = point((bottom, top, padding), 3)
    if top - bottom <= TOLERANCE or padding < 0:
        raise ValueError("Expected a positive room height and nonnegative crop padding.")
    height = top - bottom
    right = (look[1], -look[0])
    offsets = [dot(difference(vertex, origin), right) for vertex in crop_points]
    return {
        "origin": [*origin, bottom + height / 2],
        "look": [*look, 0.0],
        "right": [*right, 0.0],
        "crop_min": [min(offsets) - padding, -height / 2 - padding],
        "crop_max": [max(offsets) + padding, height / 2 + padding],
        "far_extent": depth + padding,
    }
