# -*- coding: utf-8 -*-
"""Derive reviewable floor shape edits from a captured Toposolid surface."""

from __future__ import print_function

import argparse
import json
import math
import sys
import time

from toposolid_grade_check import compare, elevation, finite, load, triangles
from toposolid_profile_check import angle, norm, parse_loops, subtract


MAX_SHAPE_POINTS = 64
MAX_SPLIT_LINES = 128
MAX_SECONDS = 5
GEOMETRY_EPSILON_FT = 1e-7


def position_on_curve(point, curve):
    kind, start, end, center, sweep, radius = curve
    if norm(subtract(point, start)) <= GEOMETRY_EPSILON_FT:
        return 0.0
    if norm(subtract(point, end)) <= GEOMETRY_EPSILON_FT:
        return 1.0
    if kind == "arc":
        radial = subtract(point, center)
        if abs(norm(radial) - radius) > GEOMETRY_EPSILON_FT:
            return None
        rotation = angle(subtract(start, center), radial)
        if sweep > 0 > rotation:
            rotation += 2 * math.pi
        elif sweep < 0 < rotation:
            rotation -= 2 * math.pi
        parameter = rotation / sweep
        return parameter if 0 < parameter < 1 else None
    dx, dy = subtract(end, start)
    px, py = subtract(point, start)
    parameter = (px * dx + py * dy) / (dx * dx + dy * dy)
    gap = norm((px - parameter * dx, py - parameter * dy))
    return parameter if 0 < parameter < 1 and gap <= GEOMETRY_EPSILON_FT else None


def derive(source, offset):
    if not finite(offset) or not finite(source["level_project_elevation_ft"]):
        raise ValueError("Supply a finite reviewed floor offset and project level elevation.")
    compare(source, source, 0, 0)
    now = getattr(time, "monotonic", time.time)
    deadline = now() + MAX_SECONDS
    mesh = triangles(source, "source", deadline, now)
    vertices, edges = {}, {}
    for item in mesh:
        points = item["points"]
        for index, vertex in enumerate(points):
            xy = vertex[:2]
            vertices.setdefault(xy, vertex)
            following = points[(index + 1) % 3]
            edge = tuple(sorted((xy, following[:2])))
            edges.setdefault(edge, []).append((points, points[(index + 2) % 3]))
    boundary, breaks = set(), []
    for edge, neighbors in sorted(edges.items()):
        if now() >= deadline:
            raise ValueError("Terrain shape planning exceeded {} seconds.".format(MAX_SECONDS))
        if len(neighbors) == 1:
            boundary.update(edge)
        elif len(neighbors) == 2:
            (first, first_other), (second, second_other) = neighbors
            if (abs(elevation(first, second_other[:2]) - second_other[2]) > GEOMETRY_EPSILON_FT
                    or abs(elevation(second, first_other[:2]) - first_other[2]) > GEOMETRY_EPSILON_FT):
                breaks.append(edge)
        else:
            raise ValueError("The source contains a mesh edge shared by more than two triangles.")

    curves = [curve for loop in parse_loops(source["profile_loops"]) for curve in loop]
    endpoints = {}
    for curve in curves:
        for point in curve[1:3]:
            if point not in endpoints:
                matches = [candidate for candidate in boundary
                           if norm(subtract(candidate, point)) <= GEOMETRY_EPSILON_FT]
                if len(matches) != 1:
                    raise ValueError("A sketch endpoint has no unique boundary mesh vertex.")
                endpoints[point] = matches[0]
            if now() >= deadline:
                raise ValueError("Terrain shape planning exceeded {} seconds.".format(MAX_SECONDS))

    floor_base = source["level_project_elevation_ft"] + offset
    corner_vertices = set(endpoints.values())
    curve_intervals = {curve: [] for curve in curves}
    for edge, neighbors in sorted(edges.items()):
        if len(neighbors) != 1:
            continue
        if now() >= deadline:
            raise ValueError("Terrain shape planning exceeded {} seconds.".format(MAX_SECONDS))
        matches = [(curve, position_on_curve(edge[0], curve), position_on_curve(edge[1], curve))
                   for curve in curves]
        matches = [(curve, a, b) for curve, a, b in matches if a is not None and b is not None]
        if len(matches) != 1:
            raise ValueError("A mesh boundary edge does not belong to one native sketch segment.")
        curve, first, second = matches[0]
        curve_intervals[curve].append(tuple(sorted((first, second))))
        z_start = vertices[endpoints[curve[1]]][2]
        z_end = vertices[endpoints[curve[2]]][2]
        for point, parameter in zip(edge, (first, second)):
            if point in corner_vertices:
                continue
            z = vertices[point][2]
            if curve[0] == "arc":
                matches_grade = max(abs(z - floor_base), abs(z_start - floor_base),
                                    abs(z_end - floor_base)) <= GEOMETRY_EPSILON_FT
            else:
                matches_grade = abs(z - (z_start + parameter * (z_end - z_start))) <= GEOMETRY_EPSILON_FT
            if not matches_grade:
                raise ValueError("The source grade needs an unsupported vertex inside a native sketch segment.")

    for curve, intervals in curve_intervals.items():
        length = (norm(subtract(curve[2], curve[1])) if curve[0] == "line" else abs(curve[4]) * curve[5])
        limit = 0.0
        for start, end in sorted(intervals):
            if abs(start - limit) * length > GEOMETRY_EPSILON_FT:
                raise ValueError("The source mesh boundary has a gap or overlap along its native sketch.")
            limit = end
        if abs(1.0 - limit) * length > GEOMETRY_EPSILON_FT:
            raise ValueError("The source mesh boundary does not cover its native sketch.")

    break_vertices = {point for edge in breaks for point in edge}
    if (break_vertices & boundary) - corner_vertices:
        raise ValueError("A grade break needs a boundary vertex that the floor sketch does not provide.")
    boundary_edits = [vertices[point] for point in sorted(corner_vertices)
                      if abs(vertices[point][2] - floor_base) > GEOMETRY_EPSILON_FT]
    interior_edits = [vertices[point] for point in sorted(break_vertices - boundary)]
    if len(boundary_edits) + len(interior_edits) > MAX_SHAPE_POINTS or len(breaks) > MAX_SPLIT_LINES:
        raise ValueError("The derived floor shape exceeds its point or split-line limit.")
    return {
        "boundary_ft": [[float(value) for value in point] for point in boundary_edits],
        "interior_ft": [[float(value) for value in point] for point in interior_edits],
        "split_lines_xy_ft": [[[float(value) for value in point] for point in edge] for edge in breaks],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--floor-offset-ft", required=True, type=float)
    args = parser.parse_args()
    try:
        shape = derive(load(args.source), args.floor_offset_ft)
    except (IOError, KeyError, TypeError, ValueError, AttributeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(shape, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
