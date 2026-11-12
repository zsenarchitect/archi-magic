# -*- coding: utf-8 -*-
"""Match bounded native floor sketch loops in model XY coordinates."""

import math
import time


MAX_CURVES = 512
MAX_SECONDS = 5
GEOMETRY_EPSILON_FT = 1e-7


def finite(value):
    return type(value) in (int, float) and abs(value) <= 1e9


def xy(point):
    if not isinstance(point, list) or len(point) != 3 or not all(finite(value) for value in point):
        raise ValueError("A sketch curve requires finite native XYZ coordinates.")
    return tuple(point[:2])


def subtract(a, b):
    return (a[0] - b[0], a[1] - b[1])


def norm(vector):
    return math.hypot(*vector)


def angle(a, b):
    return math.atan2(a[0] * b[1] - a[1] * b[0], a[0] * b[0] + a[1] * b[1])


def parse_curve(segment):
    kind = segment["kind"]
    points = tuple(xy(point) for point in segment["points_ft"])
    if kind == "line" and len(points) == 2 and points[0] != points[1]:
        return (kind, points[0], points[-1], None, None, None)
    if kind != "arc" or len(points) != 3 or points[0] == points[-1]:
        raise ValueError("A sketch curve is neither a bounded line nor a bounded circular arc.")
    center = xy(segment["center_ft"])
    start, middle, end = (subtract(point, center) for point in points)
    radius = norm(start)
    first, second = angle(start, middle), angle(middle, end)
    if (radius <= GEOMETRY_EPSILON_FT or first * second <= 0
            or abs(first - second) * radius > GEOMETRY_EPSILON_FT
            or max(abs(norm(middle) - radius), abs(norm(end) - radius)) > GEOMETRY_EPSILON_FT):
        raise ValueError("An arc midpoint and endpoints disagree with its native center or sweep.")
    return (kind, points[0], points[-1], center, first + second, radius)


def parse_loops(loops):
    if not isinstance(loops, list) or not loops or len(loops) > MAX_CURVES:
        raise ValueError("Supply a nonempty native sketch profile.")
    result, remaining = [], MAX_CURVES
    for loop in loops:
        if not isinstance(loop, list) or not 2 <= len(loop) <= remaining:
            raise ValueError("A sketch needs closed loops and at most {} total curves.".format(MAX_CURVES))
        remaining -= len(loop)
        curves = tuple(parse_curve(segment) for segment in loop)
        for current, following in zip(curves, curves[1:] + curves[:1]):
            if norm(subtract(current[2], following[1])) > GEOMETRY_EPSILON_FT:
                raise ValueError("A sketch loop is disconnected.")
        result.append(curves)
    return result


def reversed_curve(curve):
    kind, start, end, center, sweep, radius = curve
    return (kind, end, start, center, -sweep if sweep is not None else None, radius)


def curve_matches(source, floor, tolerance):
    kind, start, end, center, sweep, radius = source
    other_kind, other_start, other_end, other_center, other_sweep, other_radius = floor
    if kind != other_kind:
        return False
    if max(norm(subtract(start, other_start)), norm(subtract(end, other_end))) > tolerance:
        return False
    if kind == "line":
        return True
    # At a shared parameter the arc points are the centers plus rotated start
    # vectors. Bound their distance for the entire sweep, including between samples.
    center_gap = norm(subtract(center, other_center))
    radial_gap = norm(subtract(subtract(start, center), subtract(other_start, other_center)))
    rotation_gap = min(radius, other_radius) * abs(sweep - other_sweep)
    return center_gap + radial_gap + rotation_gap <= tolerance


def profiles_match(source, floor, tolerance):
    if not finite(tolerance) or tolerance < 0:
        raise ValueError("Supply a finite nonnegative XY tolerance.")
    source, floor = parse_loops(source), parse_loops(floor)
    if len(source) != len(floor):
        return False
    now = getattr(time, "monotonic", time.time)
    deadline = now() + MAX_SECONDS

    def check_time():
        if now() >= deadline:
            raise ValueError("Sketch comparison exceeded {} seconds.".format(MAX_SECONDS))

    def loop_matches(first, second):
        if len(first) != len(second):
            return False
        for ordered in (second, tuple(reversed_curve(curve) for curve in reversed(second))):
            for shift in range(len(ordered)):
                check_time()
                if all(curve_matches(curve, ordered[(index + shift) % len(ordered)], tolerance)
                       for index, curve in enumerate(first)):
                    return True
        return False

    candidates = [[index for index, loop in enumerate(floor) if loop_matches(expected, loop)]
                  for expected in source]
    assignments = {}

    def assign(source_index, visited):
        check_time()
        for candidate in candidates[source_index]:
            if candidate not in visited:
                visited.add(candidate)
                if candidate not in assignments or assign(assignments[candidate], visited):
                    assignments[candidate] = source_index
                    return True
        return False

    return all(assign(index, set()) for index in range(len(source)))
