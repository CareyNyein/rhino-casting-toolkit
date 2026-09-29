# -*- coding: utf-8 -*-
"""Rhino 7/8 STL Mesh Repair Guard. Run with _RunPythonScript.
IronPython 2.7 and Python 3; no extra packages required.
"""

import os
import math
import time
import threading
from array import array
from contextlib import contextmanager

import Rhino
import scriptcontext as sc
import Eto.Forms as forms
import Eto.Drawing as drawing
from System import Guid


# SETTINGS
VERSION = "2.1.2"
HANDLER_KEY = "WATERTIGHT_MESH_GUARD_ATTACHED"
BACKUP_TAG = "WMG_ORIGINAL_BACKUP"
BACKUP_CREATED_TAG = "WMG_BACKUP_CREATED"
BACKUP_SOURCE_TAG = "WMG_BACKUP_SOURCE_ID"
FILL_HOLES = True
HEAL_SEAMS = True
REMOVE_HANGING_FACES = True
MAX_SEAM_SIZE_FRACTION = 0.0001
DEEP_REPAIR = True
ALLOW_REBUILD = True
FAST_REPAIR = True
FAST_REPAIR_SECONDS = 20.0  # Between attempts; not a native-call timeout.
FAST_LOCAL_CLASH_LIMIT = 1000
REBUILD_EDGE_LENGTH = 0.0  # Model units; 0 = automatic.
MAX_CLASH_PAIRS = 20000
MAX_LOCAL_REMOVAL_FRACTION = 0.10
MAX_PATCH_EDGES = 100000
MAX_BOOLEAN_PIECES = 128
REBUILD_FACE_BUDGET = 1500000
MAX_REBUILD_OUTPUT_FACES = 5000000
MAX_REBUILD_POINTS = 2500000
COMPARISON_SAMPLES = 1500
DEFECT_COUNTS = (
    "naked_edges", "non_manifold_edges", "degenerate_faces",
    "duplicate_faces", "inconsistent_normals", "orientation_conflicts",
    "self_intersections",
)
try:
    _range = xrange
except NameError:
    _range = range

_progress = None


class CheckCancelled(Exception):
    pass


@contextmanager
def operation(doc, title):
    global _progress
    if _progress is not None:
        yield
        return
    state = sc.sticky.get(HANDLER_KEY)
    was_busy = state.get("busy", False) if isinstance(state, dict) else False
    if isinstance(state, dict):
        state["busy"] = True
    current = {"cancelled": False, "time": 0.0, "label": None, "ui": False,
               "stage_started": time.time(),
               "label_updates": (rhino_major_version(),
                   int(getattr(getattr(Rhino.RhinoApp, "Version", None), "Minor", 0))) >= (8, 6)}
    def escape(sender, event):
        current["cancelled"] = True
    active = Rhino.RhinoDoc.ActiveDoc
    current["ui"] = (active is not None and doc is not None
                     and active.RuntimeSerialNumber == doc.RuntimeSerialNumber)
    subscribed = False
    shown = False
    _progress = current
    try:
        if current["ui"]:
            Rhino.RhinoApp.EscapeKeyPressed += escape
            subscribed = True
            shown = Rhino.UI.StatusBar.ShowProgressMeter(0, 100, title, True, True) > 0
        checkpoint(title, 0)
        yield
    finally:
        if subscribed:
            Rhino.RhinoApp.EscapeKeyPressed -= escape
        if shown:
            Rhino.UI.StatusBar.HideProgressMeter()
        _progress = None
        if isinstance(state, dict):
            state["busy"] = was_busy


def checkpoint(label, fraction=0.0):
    progress = _progress
    if progress is None:
        return
    if progress["cancelled"]:
        raise CheckCancelled("Check cancelled. Completed changes can be undone.")
    now = time.time()
    if label != progress["label"]:
        log(label + " (Escape to cancel)")
        progress["label"] = label
        progress["stage_started"] = now
    if now - progress["time"] < 0.15:
        return
    progress["time"] = now
    if progress["ui"]:
        if progress["label_updates"]:
            elapsed = now - progress.get("stage_started", now)
            display = "{} ({:.0f}s)".format(label, elapsed) if elapsed >= 1 else label
            Rhino.UI.StatusBar.UpdateProgressMeter(display, int(100 * fraction), True)
        else:
            Rhino.UI.StatusBar.UpdateProgressMeter(int(100 * fraction), True)
        Rhino.RhinoApp.Wait()
        if progress["cancelled"]:
            raise CheckCancelled("Check cancelled. Completed changes can be undone.")


def log(message):
    Rhino.RhinoApp.WriteLine("[Mesh Repair] {}".format(message))


def show_message(message, title="Mesh Repair"):
    Rhino.UI.Dialogs.ShowMessageBox(message, title)


def rhino_major_version():
    try:
        return int(Rhino.RhinoApp.Version.Major)
    except (AttributeError, TypeError, ValueError):
        return int(getattr(Rhino.RhinoApp, "ExeVersion", 7))


def has_shrinkwrap():
    return (rhino_major_version() >= 8
            and hasattr(Rhino.Geometry, "ShrinkWrapParameters")
            and hasattr(Rhino.Geometry.Mesh, "ShrinkWrap"))


def has_vertex_shrinkwrap():
    return has_shrinkwrap() and hasattr(Rhino.Geometry, "PointCloud")


def unresolved_guidance():
    if rhino_major_version() < 8:
        return "Some issues remain. Try this file in Rhino 8, or use Advanced Repair."
    if not has_shrinkwrap():
        return "Rebuilding is unavailable. Update Rhino 8 or use Advanced Repair."
    return "Use Advanced Repair, or try Rebuild Now with a different edge length."


# TOPOLOGY AND FACE ORIENTATION
def _orientation_from_edges(face_count, vertex_count, records):
    adjacent = array("i", [0]) * (4 * face_count)
    degree = bytearray(face_count)
    boundary_parent = None
    boundary_groups = 0
    naked = 0
    non_manifold = 0
    for a, b, owners, directions in records:
        if len(owners) == 1 and a != b:
            naked += 1
            if boundary_parent is None:
                boundary_parent = array("i", [0]) * vertex_count
            for v in (a, b):
                if boundary_parent[v] == 0:
                    boundary_parent[v] = v + 1
                    boundary_groups += 1
            ra, rb = a, b
            while boundary_parent[ra] != ra + 1:
                boundary_parent[ra] = boundary_parent[boundary_parent[ra] - 1]
                ra = boundary_parent[ra] - 1
            while boundary_parent[rb] != rb + 1:
                boundary_parent[rb] = boundary_parent[boundary_parent[rb] - 1]
                rb = boundary_parent[rb] - 1
            if ra != rb:
                boundary_parent[ra] = rb + 1
                boundary_groups -= 1
        elif len(owners) > 2:
            non_manifold += 1
        elif len(owners) == 2:
            fa, fb = int(owners[0]), int(owners[1])
            if fa == fb:
                continue
            if degree[fa] >= 4 or degree[fb] >= 4:
                raise RuntimeError("Invalid face topology; use Advanced Repair.")
            sign = -1 if directions[0] == directions[1] else 1
            adjacent[4 * fa + degree[fa]] = sign * (fb + 1)
            adjacent[4 * fb + degree[fb]] = sign * (fa + 1)
            degree[fa] += 1
            degree[fb] += 1

    parity = bytearray(face_count)
    queue = array("i", [0]) * face_count
    flips = array("i")
    conflicts = 0
    visited = 0
    for seed in _range(face_count):
        if parity[seed]:
            continue
        parity[seed] = 1
        queue[0] = seed
        head, tail, flip_count = 0, 1, 0
        while head < tail:
            a = queue[head]
            head += 1
            visited += 1
            if visited % 16384 == 0:
                checkpoint("Checking face orientation", visited / float(max(1, face_count)))
            for slot in _range(degree[a]):
                encoded = adjacent[4 * a + slot]
                b = abs(encoded) - 1
                expected = 3 - parity[a] if encoded < 0 else parity[a]
                if parity[b] == 0:
                    parity[b] = expected
                    queue[tail] = b
                    tail += 1
                    flip_count += int(expected == 2)
                elif parity[b] != expected and a < b:
                    conflicts += 1
        chosen = 1 if flip_count > tail / 2.0 else 2
        for offset in _range(tail):
            a = queue[offset]
            if parity[a] == chosen:
                flips.append(a)
    return {
        "naked_loops": boundary_groups,
        "naked_edges": naked,
        "non_manifold_edges": non_manifold,
        "inconsistent_normals": len(flips),
        "orientation_conflicts": conflicts,
        "flip_faces": flips,
    }


def _topology_details(mesh):
    import clr
    from System import Array, Boolean
    edges = mesh.TopologyEdges
    edge_count = edges.Count

    # IronPython uses an out-reference; Python 3 returns a tuple.
    out = clr.Reference[Array[Boolean]]() if hasattr(clr, "Reference") else None
    def records():
        for index in _range(edge_count):
            if index % 16384 == 0:
                checkpoint("Checking mesh edges", index / float(max(1, edge_count)))
            if out is not None:
                owners = edges.GetConnectedFaces(index, out)
                directions = out.Value
            else:
                owners, directions = edges.GetConnectedFaces(index, None)
            if len(owners) == 1:
                pair = edges.GetTopologyVertices(index)
                yield pair.I, pair.J, owners, directions
            else:
                yield 0, 0, owners, directions
    return _orientation_from_edges(mesh.Faces.Count, mesh.TopologyVertices.Count, records())


def orient_faces_consistently(mesh, checked_details=None):
    details = _topology_details(mesh)
    if details["orientation_conflicts"]:
        raise RuntimeError("Face winding has contradictory connections; use Advanced Repair.")
    for index in details["flip_faces"]:
        face = mesh.Faces[index]
        if face.IsTriangle:
            success = mesh.Faces.SetFace(index, face.A, face.C, face.B)
        else:
            success = mesh.Faces.SetFace(index, face.A, face.D, face.C, face.B)
        if not success:
            raise RuntimeError("Could not reverse face {}.".format(index))
    mesh.RebuildNormals()
    flipped = details["inconsistent_normals"]
    if checked_details is not None:
        checked_details.update(details)
        checked_details["inconsistent_normals"] = 0
        checked_details["flip_faces"] = array("i")
    return flipped


# MESH ANALYSIS
def inspect_mesh(mesh, checked_details=None):
    report = {
        "valid": False,
        "closed": False,
        "naked_loops": 0,
        "naked_edges": 0,
        "non_manifold_edges": 0,
        "degenerate_faces": 0,
        "duplicate_faces": 0,
        "inconsistent_normals": 0,
        "orientation_conflicts": 0,
        "self_intersections": 0,
        "intersection_check": "deferred",
        "errors": [],
    }

    try:
        checkpoint("Checking mesh validity")
        report["valid"] = bool(mesh.IsValid) and mesh.Faces.Count > 0
    except CheckCancelled:
        raise
    except Exception as error:
        report["errors"].append("Validity/closure: {}".format(error))

    try:
        details = checked_details if checked_details else _topology_details(mesh)
        for key in ("naked_loops", "naked_edges", "non_manifold_edges", "inconsistent_normals", "orientation_conflicts"):
            report[key] = details[key]

        # Use edge incidences: Rhino closure flags can be stale after edits.
        report["closed"] = mesh.Faces.Count > 0 and details["naked_edges"] == 0
    except CheckCancelled:
        raise
    except Exception as error:
        report["errors"].append("Topology/orientation: {}".format(error))

    probe = None
    try:
        checkpoint("Checking degenerate and duplicate faces")
        probe = mesh.DuplicateMesh()
        report["degenerate_faces"] = int(probe.Faces.CullDegenerateFaces())

        probe.Vertices.CombineIdentical(True, False)
        report["degenerate_faces"] += int(probe.Faces.CullDegenerateFaces())
        checkpoint("Checking duplicate faces")
        duplicate = probe.Faces.ExtractDuplicateFaces()
        if duplicate is not None:
            try:
                report["duplicate_faces"] = duplicate.Faces.Count
            finally:
                duplicate.Dispose()
    except CheckCancelled:
        raise
    except Exception as error:
        report["errors"].append("Degenerate/duplicate faces: {}".format(error))
    finally:
        if probe is not None:
            probe.Dispose()

    if (report["valid"] and not report["errors"]
            and not report["non_manifold_edges"]
            and not report["degenerate_faces"] and not report["duplicate_faces"]):
        try:
            checkpoint("Checking intersecting mesh faces")
            pairs = mesh.Faces.GetClashingFacePairs(MAX_CLASH_PAIRS)
            report["self_intersections"] = len(pairs) if pairs is not None else 0
            report["intersection_check"] = (
                "limit" if report["self_intersections"] >= MAX_CLASH_PAIRS else "complete")
            checkpoint("Intersection check finished")
        except CheckCancelled:
            raise
        except Exception as error:
            report["intersection_check"] = "failed"
            report["errors"].append("Face intersections: {}".format(error))

    return report


def is_clean(report):
    return (
        report["valid"]
        and report["closed"]
        and report["naked_loops"] == 0
        and all(report[key] == 0 for key in DEFECT_COUNTS)
        and report.get("intersection_check") == "complete"
        and not report["errors"]
    )


def mesh_objects(doc):
    result = []
    settings = Rhino.DocObjects.ObjectEnumeratorSettings()
    settings.ObjectTypeFilter = Rhino.DocObjects.ObjectType.Mesh
    settings.NormalObjects = True
    settings.HiddenObjects = True
    settings.LockedObjects = True
    settings.ReferenceObjects = False
    for obj in doc.Objects.GetObjectList(settings):
        if obj.IsDeleted or obj.IsInstanceDefinitionGeometry:
            continue
        if obj.Attributes.GetUserString(BACKUP_TAG) == "1":
            continue
        if isinstance(obj.Geometry, Rhino.Geometry.Mesh):
            result.append(obj)
    return result


def analyze_document(doc):
    with operation(doc, "Checking document meshes"):
        return _analyze_document(doc)


def _analyze_document(doc):
    objects = mesh_objects(doc)
    failing = []
    totals = {
        "objects": len(objects),
        "failing": 0,
        "invalid": 0,
        "open": 0,
        "naked_loops": 0,
        "naked_edges": 0,
        "non_manifold_edges": 0,
        "degenerate_faces": 0,
        "duplicate_faces": 0,
        "inconsistent_normals": 0,
        "orientation_conflicts": 0,
        "self_intersections": 0,
        "intersection_unchecked": 0,
        "intersection_limited": 0,
        "check_errors": 0,
        "_mesh_stamps": {},
    }
    for obj in objects:
        source_crc = int(obj.Geometry.DataCRC(0))
        report = inspect_mesh(obj.Geometry)
        report["_source_crc"] = source_crc
        totals["_mesh_stamps"][str(obj.Id)] = source_crc
        for key in ("naked_loops",) + DEFECT_COUNTS:
            totals[key] += report[key]
        totals["invalid"] += int(not report["valid"])
        totals["open"] += int(not report["closed"])
        totals["check_errors"] += len(report["errors"])
        totals["intersection_unchecked"] += int(report["intersection_check"] in ("deferred", "failed"))
        totals["intersection_limited"] += int(report["intersection_check"] == "limit")
        if not is_clean(report):
            failing.append((obj, report))
        for error in report["errors"]:
            log("Mesh {}: {}".format(obj.Id, error))
    totals["failing"] = len(failing)
    if not _analysis_current(doc, totals):
        raise CheckCancelled("The document changed during checking. Run the guard again.")
    return objects, failing, totals


def _analysis_current(doc, totals):
    stamps = totals.get("_mesh_stamps")
    if stamps is None:
        return True
    current = mesh_objects(doc)
    return (len(current) == len(stamps)
            and all(stamps.get(str(obj.Id)) == int(obj.Geometry.DataCRC(0)) for obj in current))


def _use_analysis(doc, analysis):
    if analysis is None or not _analysis_current(doc, analysis[2]):
        return analyze_document(doc)
    return analysis


def format_report(totals):
    return (
        "Meshes: {objects}\n"
        "Need repair or review: {failing}\n\n"
        "Open meshes: {open}\n"
        "Open boundary groups: {naked_loops}\n"
        "Naked edges: {naked_edges}\n"
        "Non-manifold edges: {non_manifold_edges}\n"
        "Degenerate faces: {degenerate_faces}\n"
        "Duplicate faces: {duplicate_faces}\n"
        "Faces needing normal unification: {inconsistent_normals}\n"
        "Conflicting orientation connections: {orientation_conflicts}\n"
        "Intersecting face pairs found: {self_intersections}\n"
        "Meshes awaiting intersection checks: {intersection_unchecked}\n"
        "Intersection checks reaching the limit: {intersection_limited}\n"
        "Invalid/empty meshes: {invalid}\n"
        "Checks that could not finish: {check_errors}"
    ).format(**dict(dict(self_intersections=0, intersection_unchecked=0,
                        intersection_limited=0), **totals))


REPORT_ROWS = (
    ("open", "Open meshes"),
    ("naked_edges", "Open edges"),
    ("non_manifold_edges", "Non-manifold edges"),
    ("degenerate_faces", "Degenerate faces"),
    ("duplicate_faces", "Duplicate faces"),
    ("inconsistent_normals", "Faces needing normal correction"),
    ("orientation_conflicts", "Orientation conflicts"),
    ("invalid", "Invalid meshes"),
    ("check_errors", "Failed checks"),
)


def intersection_status(totals):
    count = totals.get("self_intersections", 0)
    pending = totals.get("intersection_unchecked", 0)
    limited = totals.get("intersection_limited", 0)
    text = "{}{}".format(count, "+" if limited else "")
    if pending:
        noun = "mesh" if pending == 1 else "meshes"
        if not count:
            text = "Not checked ({} {})".format(pending, noun)
        else:
            text += "; {} {} unchecked".format(pending, noun)
    if limited:
        text += "; check limit reached"
    return text


def format_compact_report(totals):
    lines = ["Meshes: {}    Need repair: {}".format(totals["objects"], totals["failing"])]
    if not totals["failing"]:
        return lines[0] + "\nAll implemented checks passed."
    lines.append("")
    for key, label in REPORT_ROWS:
        if totals.get(key, 0):
            lines.append("{}: {}".format(label, totals[key]))
    lines.append("Intersecting face pairs: " + intersection_status(totals))
    return "\n".join(lines)


def format_before_after(before, after):
    lines = ["BEFORE -> AFTER", "Meshes needing repair: {} -> {}".format(
        before["failing"], after["failing"])]
    for key, label in REPORT_ROWS:
        if before.get(key, 0) or after.get(key, 0):
            lines.append("{}: {} -> {}".format(label, before.get(key, 0), after.get(key, 0)))
    lines.append("Intersecting face pairs: {} -> {}".format(
        intersection_status(before), intersection_status(after)))
    return "\n".join(lines)


def score_report(report):
    return (
        (0 if report["valid"] else 100000)
        + (0 if report["closed"] else 10000)
        + report["non_manifold_edges"] * 100
        + report["naked_edges"] * 10
        + report["degenerate_faces"]
        + report["duplicate_faces"]
        + report["inconsistent_normals"]
        + report["orientation_conflicts"] * 100
        + (report.get("self_intersections", 0) * 100
           if report.get("intersection_check") == "complete"
           else (MAX_CLASH_PAIRS + 1) * 100)
        + len(report["errors"]) * 1000000
    )


# ORIGINAL MODEL BACKUP
def preserve_original(doc, obj):
    backup_text = obj.Attributes.GetUserString(BACKUP_CREATED_TAG)
    if backup_text and backup_text != "1":
        try:
            backup_id = Guid(backup_text)
        except Exception:
            backup_id = Guid.Empty
        existing = doc.Objects.FindId(backup_id)
        if (
            existing is not None
            and not existing.IsDeleted
            and isinstance(existing.Geometry, Rhino.Geometry.Mesh)
            and existing.Attributes.GetUserString(BACKUP_TAG) == "1"
            and existing.Attributes.GetUserString(BACKUP_SOURCE_TAG) == str(obj.Id)
        ):
            return backup_id

    backup_mesh = obj.Geometry.DuplicateMesh()
    try:
        attributes = obj.Attributes.Duplicate()
        attributes.ObjectId = Guid.Empty
        attributes.Name = (attributes.Name or "Mesh") + " [Mesh Repair Original]"
        attributes.RemoveFromAllGroups()
        attributes.SetUserString(BACKUP_TAG, "1")
        attributes.SetUserString(BACKUP_SOURCE_TAG, str(obj.Id))
        attributes.SetUserString(BACKUP_CREATED_TAG, None)
        attributes.Mode = Rhino.DocObjects.ObjectMode.Hidden
        attributes.Visible = False

        backup_id = doc.Objects.AddMesh(
            backup_mesh, attributes, None, False, False
        )
    finally:
        backup_mesh.Dispose()

    if backup_id == Guid.Empty:
        raise RuntimeError("Rhino could not create the hidden original backup.")
    backup = doc.Objects.FindId(backup_id)
    if backup is None or not backup.IsHidden:

        if not doc.Objects.Hide(backup_id, True):
            raise RuntimeError("Rhino could not hide the original backup.")

    attributes = obj.Attributes.Duplicate()
    attributes.SetUserString(BACKUP_CREATED_TAG, str(backup_id))
    if not doc.Objects.ModifyAttributes(obj.Id, attributes, True):
        raise RuntimeError("Backup saved, but the source could not be tagged.")
    return backup_id


# CONSERVATIVE REPAIR
def improvement_rejection(before, after):
    if after["errors"]:
        return "Candidate checks failed: " + "; ".join(after["errors"])
    if not after["valid"]:
        return "Candidate is still invalid or empty."
    if before["closed"] and not after["closed"]:
        return "Candidate would open a previously closed mesh."
    if (before.get("intersection_check") == "complete"
            and after.get("intersection_check") != "complete"):
        return "Candidate could not complete a previously completed intersection check."
    worse = [key for key in DEFECT_COUNTS if after[key] > before[key]
             and (key != "self_intersections" or before.get("intersection_check") == "complete")]
    if worse:
        return "Candidate would increase: {}.".format(
            ", ".join(key.replace("_", " ") for key in worse)
        )
    if score_report(after) >= score_report(before):
        return "No measured defect improved."
    return ""


def cleanup_mesh(mesh, checked_details=None):
    mesh.DestroyTopology()
    mesh.Vertices.CombineIdentical(True, False)
    mesh.Faces.CullDegenerateFaces()
    duplicate = mesh.Faces.ExtractDuplicateFaces()
    if duplicate is not None:
        duplicate.Dispose()
    removed = 0
    if REMOVE_HANGING_FACES:

        hanging = mesh.ExtractNonManifoldEdges(True)
        if hanging is not None:
            removed = hanging.Faces.Count
            hanging.Dispose()
    mesh.Vertices.CullUnused()
    mesh.DestroyTopology()
    orient_faces_consistently(mesh, checked_details)
    mesh.Compact()
    return removed


def conservative_repair_mesh(source, seam_tolerance=None, source_report=None):
    before = inspect_mesh(source) if source_report is None else source_report
    if before["errors"]:
        after = dict(before)
        after["repair_notes"] = [
            "Source analysis failed; automatic replacement skipped. "
            + "; ".join(before["errors"])
        ]
        return None, before, after

    candidate = None
    trial = None
    filling = None
    accepted = None
    after = before
    notes = []
    try:
        checkpoint("Cleaning mesh copy")
        candidate = source.DuplicateMesh()
        checked = {}
        removed = cleanup_mesh(candidate, checked)
        if removed:
            notes.append("Removed {} dangling non-manifold face(s).".format(removed))
        cleaned_report = inspect_mesh(candidate, checked)
        reason = improvement_rejection(before, cleaned_report)
        if reason:
            notes.append("Cleanup: " + reason)
        else:
            accepted = candidate
            after = cleaned_report
            notes.append("Cleanup improved the measured defects.")

        if cleaned_report["naked_edges"]:
            tolerances = []
            if cleaned_report["non_manifold_edges"]:
                notes.append("Ambiguous junctions remain; small seam movement skipped before deeper repair.")
            elif HEAL_SEAMS and seam_tolerance is not None:
                limit = float(seam_tolerance)
                if limit > 0 and not math.isnan(limit) and not math.isinf(limit):
                    size_limit = candidate.GetBoundingBox(True).Diagonal.Length * MAX_SEAM_SIZE_FRACTION
                    limit = min(limit, size_limit)
                    if limit > 0:
                        tolerances = [limit / 10.0, limit]

            for tolerance in tolerances + ([0.0] if FILL_HOLES else []):
                trial = candidate.DuplicateMesh()
                stage = "Seam limit {:.6g} model units".format(tolerance) if tolerance else "Hole filling"
                try:
                    checkpoint(stage)
                    if tolerance:
                        run_geometry_worker(stage, lambda: trial.HealNakedEdges(tolerance))
                        checked = {}
                        cleanup_mesh(trial, checked)
                        trial_report = inspect_mesh(trial, checked)
                        reason = improvement_rejection(after, trial_report)
                        if not reason:
                            if accepted is not None and accepted is not candidate:
                                accepted.Dispose()
                            accepted = trial
                            after = trial_report
                            notes.append(stage + ": stitching improved the mesh.")

                    else:
                        trial_report = cleaned_report
                    if FILL_HOLES and trial_report["naked_edges"]:

                        filling = trial.DuplicateMesh() if trial is accepted else trial
                        checkpoint("Filling mesh holes")
                        run_geometry_worker("Filling mesh holes", lambda: filling.FillHoles())
                        checked = {}
                        cleanup_mesh(filling, checked)
                        filled_report = inspect_mesh(filling, checked)
                        reason = improvement_rejection(after, filled_report)
                        if not reason:
                            if accepted is not None and accepted is not candidate:
                                if accepted is trial:
                                    trial = None
                                accepted.Dispose()
                            accepted = filling
                            after = filled_report
                            notes.append(stage + ": repair improved the mesh.")
                        else:
                            notes.append(stage + ": " + reason)
                    if is_clean(after):
                        break
                except CheckCancelled:
                    raise
                except Exception as error:
                    notes.append(stage + " failed: {}".format(error))
                finally:
                    if filling is not None and filling is not accepted and filling is not trial:
                        filling.Dispose()
                    if trial is not None and trial is not accepted:
                        trial.Dispose()
                    trial = filling = None
    except CheckCancelled:
        if accepted is not None and accepted is not candidate:
            accepted.Dispose()
        accepted = None
        raise
    except Exception as error:
        notes.append("Cleanup failed: {}".format(error))
    finally:
        if candidate is not None and candidate is not accepted:
            candidate.Dispose()

    after = dict(after)
    after["repair_notes"] = notes
    return accepted, before, after


# VERSION-AWARE DEEP REPAIR
def run_geometry_worker(label, function, cancellation=None):
    """Join the worker before releasing its geometry, including after cancellation."""
    checkpoint(label)
    result = {}
    def worker():
        try:
            result["value"] = function()
        except BaseException as error:
            result["error"] = error
    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()
    interrupted = None
    while thread.is_alive():
        thread.join(0.1)
        try:
            checkpoint(label)
        except BaseException as error:
            if interrupted is None:
                interrupted = error
                if cancellation is not None:
                    try:
                        cancellation.Cancel()
                    except Exception:
                        pass
                log("Cancellation requested; waiting for the active Rhino operation to stop.")
            if _progress is not None and _progress["ui"]:
                try:
                    Rhino.RhinoApp.Wait()
                except Exception:
                    pass
    if interrupted is not None:
        value = result.get("value")
        if value is not None:
            if hasattr(value, "Dispose"):
                value.Dispose()
            else:
                try:
                    items = iter(value)
                except TypeError:
                    items = iter(())
                for item in items:
                    if hasattr(item, "Dispose"):
                        item.Dispose()
        raise interrupted
    if "error" in result:
        raise result["error"]
    return result.get("value")


def patch_simple_boundaries(mesh):
    edges = mesh.TopologyEdges
    neighbours = {}
    count = 0
    for index in _range(edges.Count):
        if index % 16384 == 0:
            checkpoint("Finding repair boundaries", index / float(max(1, edges.Count)))
        if len(edges.GetConnectedFaces(index)) != 1:
            continue
        count += 1
        if count > MAX_PATCH_EDGES:
            raise RuntimeError("Too many boundary edges for local patching; trying the next method.")
        pair = edges.GetTopologyVertices(index)
        neighbours.setdefault(pair.I, []).append(pair.J)
        neighbours.setdefault(pair.J, []).append(pair.I)
    remaining = set(neighbours)
    loops = []
    while remaining:
        start = next(iter(remaining))
        component = set([start])
        stack = [start]
        while stack:
            vertex = stack.pop()
            for other in neighbours[vertex]:
                if other not in component:
                    component.add(other)
                    stack.append(other)
        remaining.difference_update(component)
        if (len(component) < 3 or len(component) > 10000
                or any(len(neighbours[v]) != 2 for v in component)):
            continue
        order = [start]
        previous, current = start, neighbours[start][0]
        while current != start and len(order) <= len(component):
            order.append(current)
            options = neighbours[current]
            previous, current = current, options[1] if options[0] == previous else options[0]
        if current == start and len(order) == len(component):
            points = [Rhino.Geometry.Point3d(mesh.TopologyVertices[v]) for v in order]
            points.append(points[0])
            loops.append(Rhino.Geometry.Polyline(points))
    patched = 0
    for index, polyline in enumerate(loops):
        checkpoint("Patching damaged regions", index / float(max(1, len(loops))))
        patch = Rhino.Geometry.Mesh.CreateFromClosedPolyline(polyline)
        if patch is not None:
            try:
                if patch.IsValid and patch.Faces.Count:
                    mesh.Append(patch)
                    patched += 1
            finally:
                patch.Dispose()
    return patched


def deep_local_trial(source, source_report, tolerance, remove_intersections=False):
    from System import Int32
    from System.Collections.Generic import List
    mesh = source.DuplicateMesh()
    notes = []
    try:
        original_faces = mesh.Faces.Count
        mesh.Vertices.CombineIdentical(True, False)
        mesh.Faces.CullDegenerateFaces()
        duplicate = mesh.Faces.ExtractDuplicateFaces()
        if duplicate is not None:
            duplicate.Dispose()
        checkpoint("Removing damaged edge junctions")
        extracted = mesh.ExtractNonManifoldEdges(False)
        removed = 0
        if extracted is not None:
            removed = extracted.Faces.Count
            extracted.Dispose()
        if remove_intersections:
            checkpoint("Finding intersecting face regions")
            pairs = mesh.Faces.GetClashingFacePairs(MAX_CLASH_PAIRS)
            if pairs is not None and len(pairs) >= MAX_CLASH_PAIRS:
                raise RuntimeError("Intersection list reached its limit; local face removal skipped.")
            indices = set()
            for pair in pairs or []:
                indices.add(pair.I)
                indices.add(pair.J)
            if removed + len(indices) > max(1, original_faces * MAX_LOCAL_REMOVAL_FRACTION):
                raise RuntimeError("Damage covers too many faces for local removal; trying the next method.")
            if indices:
                face_ids = List[Int32]()
                for index in sorted(indices):
                    face_ids.Add(index)
                removed += mesh.Faces.DeleteFaces(face_ids)
        if removed > max(1, original_faces * MAX_LOCAL_REMOVAL_FRACTION):
            raise RuntimeError("Too much geometry would be removed by local repair.")
        if mesh.Faces.Count == 0:
            raise RuntimeError("Local repair would remove the entire mesh.")
        if removed:
            notes.append("Removed {} damaged face(s) before patching.".format(removed))
        mesh.Vertices.CullUnused()
        mesh.DestroyTopology()

        # Healing intersection-cut borders stalled on the 199431 model.
        if remove_intersections:
            notes.append("Intersection cuts were patched without edge healing or matching.")
        elif HEAL_SEAMS and tolerance and tolerance > 0:
            limit = min(tolerance, source.GetBoundingBox(True).Diagonal.Length * MAX_SEAM_SIZE_FRACTION)
            if limit > 0:
                checkpoint("Joining exposed boundaries")
                run_geometry_worker("Healing exposed boundaries", lambda: mesh.HealNakedEdges(limit))
                mesh.DestroyTopology()

                if hasattr(mesh, "MatchEdges"):
                    run_geometry_worker("Matching exposed boundaries", lambda: mesh.MatchEdges(limit, True))
                    mesh.DestroyTopology()
                    notes.append("Matched exposed edges within {:.6g} model units.".format(limit))
        if FILL_HOLES:
            patches = patch_simple_boundaries(mesh)
            notes.append("Patched {} simple boundary loop(s).".format(patches))
        checked = {}
        cleanup_mesh(mesh, checked)
        report = inspect_mesh(mesh, checked)
        return mesh, report, notes
    except BaseException:
        mesh.Dispose()
        raise


def boolean_union_trial(source, tolerance):
    from System.Collections.Generic import List
    pieces = source.SplitDisjointPieces()
    outputs = None
    combined = None
    try:
        if pieces is None or len(pieces) < 2:
            return None, "Only one connected shell; shell union is not applicable."
        if len(pieces) > MAX_BOOLEAN_PIECES:
            return None, "Too many shells for a bounded boolean trial."
        if any(not piece.IsValid or not piece.IsClosed for piece in pieces):
            return None, "Shell union requires valid closed input shells."
        inputs = List[Rhino.Geometry.Mesh]()
        for piece in pieces:

            if piece.SolidOrientation() < 0:
                piece.Flip(True, True, True)
            inputs.Add(piece)
        if rhino_major_version() >= 8:
            outputs = run_geometry_worker("Joining overlapping shells with Rhino 8",
                lambda: Rhino.Geometry.Mesh.CreateBooleanUnion(inputs, max(0.0, tolerance or 0.0)))
        else:
            outputs = run_geometry_worker("Joining overlapping shells with Rhino 7",
                lambda: Rhino.Geometry.Mesh.CreateBooleanUnion(inputs))
        if outputs is None or len(outputs) == 0:
            return None, "Rhino's mesh union did not return geometry."
        combined = Rhino.Geometry.Mesh()
        for piece in outputs:
            combined.Append(piece)
        result = combined
        combined = None
        return result, "Joined overlapping closed shells within this mesh object."
    finally:
        if combined is not None:
            combined.Dispose()
        for collection in (outputs, pieces):
            if collection is not None:
                for piece in collection:
                    piece.Dispose()


def automatic_rebuild_edge(source):
    properties = Rhino.Geometry.AreaMassProperties.Compute(source, True, False, False, False)
    try:
        if properties is None or properties.Area <= 0:
            raise RuntimeError("Cannot determine a rebuild resolution for this mesh.")
        area = properties.Area
    finally:
        if properties is not None:
            properties.Dispose()
    diagonal = source.GetBoundingBox(True).Diagonal.Length
    target_faces = min(REBUILD_FACE_BUDGET, max(100000, source.Faces.Count))

    edge = max(math.sqrt(area / (0.4330127019 * target_faces)), diagonal / 2000.0)
    if edge <= 0 or math.isnan(edge) or math.isinf(edge):
        raise RuntimeError("The mesh has no usable size for rebuilding.")
    return edge


def minimum_rebuild_edge(source):
    properties = Rhino.Geometry.AreaMassProperties.Compute(source, True, False, False, False)
    try:
        if properties is None or properties.Area <= 0:
            raise RuntimeError("Cannot estimate rebuild size for this mesh.")
        area = properties.Area
    finally:
        if properties is not None:
            properties.Dispose()
    diagonal = source.GetBoundingBox(True).Diagonal.Length
    return max(math.sqrt(area / (0.4330127019 * MAX_REBUILD_OUTPUT_FACES)), diagonal / 4000.0)


def sampled_surface_cloud(mesh, edge_length):
    if mesh.Vertices.Count > MAX_REBUILD_POINTS:
        raise RuntimeError("Too many vertices for sampled-surface reconstruction.")
    cloud = Rhino.Geometry.PointCloud()
    spacing = edge_length * 2.0
    def add_triangle(a, b, c):
        ab, bc, ca = a.DistanceTo(b), b.DistanceTo(c), c.DistanceTo(a)
        if max(ab, bc, ca) <= spacing:
            return
        if bc >= ab and bc >= ca:
            a, b, c, length = b, c, a, bc
        elif ca >= ab:
            a, b, c, length = c, a, b, ca
        else:
            length = ab
        ux, uy, uz = b.X-a.X, b.Y-a.Y, b.Z-a.Z
        vx, vy, vz = c.X-a.X, c.Y-a.Y, c.Z-a.Z
        cx, cy, cz = uy*vz-uz*vy, uz*vx-ux*vz, ux*vy-uy*vx
        height = math.sqrt(cx*cx + cy*cy + cz*cz) / length
        rows = max(1, int(math.ceil(height / spacing)))
        columns = max(1, int(math.ceil(length / spacing)))
        if cloud.Count + (rows + 1) * (columns + 1) > MAX_REBUILD_POINTS:
            raise RuntimeError("Surface sampling would exceed its memory budget; choose a larger rebuild edge length.")
        for row in _range(rows):
            t = row / float(rows)
            steps = max(1, int(math.ceil(length * (1.0-t) / spacing)))
            for column in _range(steps + 1):
                if row == 0 and column in (0, steps):
                    continue
                u = (1.0-t) * column / float(steps)
                cloud.Add(Rhino.Geometry.Point3d(a.X + t*vx + u*ux,
                                               a.Y + t*vy + u*uy,
                                               a.Z + t*vz + u*uz))
    try:

        cloud.AddRange(mesh.Vertices.ToPoint3dArray())
        for index in _range(mesh.Faces.Count):
            if index % 8192 == 0:
                checkpoint("Sampling original surfaces", index / float(max(1, mesh.Faces.Count)))
            face = mesh.Faces[index]
            a, b, c = mesh.Vertices[face.A], mesh.Vertices[face.B], mesh.Vertices[face.C]
            add_triangle(a, b, c)
            if not face.IsTriangle:
                add_triangle(a, c, mesh.Vertices[face.D])
        return cloud
    except BaseException:
        cloud.Dispose()
        raise


def shrinkwrap_trial(source, edge_length, fill_input_holes, inflate_vertices=False):
    if not has_shrinkwrap():
        raise RuntimeError("ShrinkWrap requires Rhino 8 or later.")
    from System.Threading import CancellationTokenSource
    copy = source.DuplicateMesh()
    cloud = None
    token_source = CancellationTokenSource()
    try:
        if inflate_vertices:
            copy.Vertices.CombineIdentical(True, False)
            cloud = sampled_surface_cloud(copy, edge_length)
        parameters = Rhino.Geometry.ShrinkWrapParameters()
        parameters.TargetEdgeLength = edge_length
        parameters.Offset = 0.0
        parameters.SmoothingIterations = 0
        parameters.PolygonOptimization = 0
        parameters.FillHolesInInputObjects = fill_input_holes
        if hasattr(parameters, "InflateVerticesAndPoints"):
            parameters.InflateVerticesAndPoints = inflate_vertices
        if inflate_vertices:

            return run_geometry_worker(
                "Rhino 8 rebuilding sampled surfaces, edge {:.6g}".format(edge_length),
                lambda: Rhino.Geometry.Mesh.ShrinkWrap(cloud, parameters, token_source.Token), token_source)
        return run_geometry_worker(
            "Rhino 8 rebuilding, edge length {:.6g}".format(edge_length),
            lambda: copy.ShrinkWrap(parameters, token_source.Token), token_source)
    finally:

        token_source.Dispose()
        if cloud is not None:
            cloud.Dispose()
        copy.Dispose()


def contains_material(mesh, point, scale):
    tolerance = max(scale * 0.0000001, 1.0e-9)
    if mesh.IsPointInside(point, tolerance, False):
        return True
    step = max(scale * 0.000001, 1.0e-8)
    for sign in (-1, 1):
        probe = Rhino.Geometry.Point3d(point.X + sign * 1.23 * step,
                                       point.Y + sign * 3.45 * step,
                                       point.Z + sign * 5.67 * step)
        if not mesh.IsPointInside(probe, tolerance, False):
            return False
    return True


def compare_geometry(source, candidate, allow_solid_union=False, rebuild_edge=0.0):
    before_box, after_box = source.GetBoundingBox(True), candidate.GetBoundingBox(True)
    before = [before_box.Max.X-before_box.Min.X, before_box.Max.Y-before_box.Min.Y,
              before_box.Max.Z-before_box.Min.Z]
    after = [after_box.Max.X-after_box.Min.X, after_box.Max.Y-after_box.Min.Y,
             after_box.Max.Z-after_box.Min.Z]
    maximum = 0.0
    directional = []
    outside_loss = 0.0
    material_limit = max(before_box.Diagonal.Length * 0.005, rebuild_edge * 4.0)
    count = 0
    for direction, (mesh, target) in enumerate(((source, candidate), (candidate, source))):
        direction_max = 0.0

        for faces in (False, True):
            population = mesh.Faces.Count if faces else mesh.Vertices.Count
            sample_count = min(COMPARISON_SAMPLES, population)
            for sample in _range(sample_count):
                if sample % 128 == 0:
                    checkpoint("Comparing repaired shape", sample / float(max(1, sample_count)))
                index = sample * (population - 1) // max(1, sample_count - 1)
                point = Rhino.Geometry.Point3d(mesh.Faces.GetFaceCenter(index) if faces else mesh.Vertices[index])
                closest = target.ClosestMeshPoint(point, 0.0)
                if closest is None:
                    raise RuntimeError("Could not compare a repaired surface with the original.")
                distance = point.DistanceTo(closest.Point)
                direction_max = max(direction_max, distance)
                maximum = max(maximum, distance)
                if direction == 0 and distance > outside_loss:

                    inside = (allow_solid_union and distance > material_limit
                              and contains_material(candidate, point, before_box.Diagonal.Length))
                    if not inside:
                        outside_loss = distance
                count += 1
        directional.append(direction_max)
    if not count or not candidate.IsValid:
        raise RuntimeError("No valid repaired surface was available for comparison.")
    return {"before_dimensions": before, "after_dimensions": after,
            "sampled_max_change": maximum, "samples": count,
            "source_to_repair_max": directional[0], "repair_to_source_max": directional[1],
            "outside_loss_max": outside_loss}


def bounds_rejection(source, candidate, rebuild_edge=0.0):
    before, after = source.GetBoundingBox(True), candidate.GetBoundingBox(True)
    diagonal = before.Diagonal.Length

    for axis in ("X", "Y", "Z"):
        low, high = getattr(before.Min, axis), getattr(before.Max, axis)
        limit = max((high - low) * 0.03, rebuild_edge * 2.5, diagonal * 0.000001)
        if (abs(low - getattr(after.Min, axis)) > limit
                or abs(high - getattr(after.Max, axis)) > limit):
            return "Repair changed the {} bounds too much.".format(axis)
    return ""


def shape_rejection(source, candidate, comparison, rebuild_edge=0.0):
    reason = bounds_rejection(source, candidate, rebuild_edge)
    if reason:
        return reason
    diagonal = source.GetBoundingBox(True).Diagonal.Length

    loss = comparison.get("outside_loss_max", comparison.get("source_to_repair_max", comparison["sampled_max_change"]))
    if loss > max(diagonal * 0.005, rebuild_edge * 4.0):
        return "Too much of the sampled original surface is missing from the repair."
    return ""


def repair_mesh(source, seam_tolerance=None, source_report=None,
                allow_rebuild=None, rebuild_edge=None, fast_repair=None,
                rebuild_only=False):
    if allow_rebuild is None:
        allow_rebuild = ALLOW_REBUILD
    if rebuild_edge is None:
        rebuild_edge = REBUILD_EDGE_LENGTH
    if rebuild_edge < 0 or math.isnan(rebuild_edge) or math.isinf(rebuild_edge):
        raise ValueError("Rebuild edge length must be zero (automatic) or a positive number.")
    if rebuild_only and not has_shrinkwrap():
        raise RuntimeError("Rebuild Now requires Rhino 8 or later. " + unresolved_guidance())
    if rebuild_only:
        allow_rebuild = True
    if fast_repair is None:
        fast_repair = FAST_REPAIR
    fast = bool(fast_repair and allow_rebuild and has_shrinkwrap())
    before = inspect_mesh(source) if source_report is None else source_report
    started = time.time()
    best = None
    after = before
    notes = []
    rebuilt = False
    comparison = None
    try:
        if rebuild_only:
            notes.append("Rebuild Now: skipped local repair attempts at the user's request.")
        else:
            best, _, after = conservative_repair_mesh(source, seam_tolerance, before)
            notes.extend(after.get("repair_notes", []))
        if DEEP_REPAIR and not rebuild_only and not is_clean(after):

            union_stamp = None
            for stage in ("shell union", "local patching", "union after patching",
                          "intersection patching", "final shell union"):
                if is_clean(after):
                    break
                if fast and time.time() - started >= FAST_REPAIR_SECONDS:
                    notes.append("Fast repair: moving to rebuilding after the local repair time budget. "
                                 "The budget is checked between operations.")
                    break
                seed = best if best is not None else source
                trial = None
                try:
                    if "union" in stage:
                        if not after.get("self_intersections") or after.get("intersection_check") == "deferred":
                            continue
                        stamp = int(seed.DataCRC(0))
                        if union_stamp == stamp:
                            continue
                        union_stamp = stamp
                        trial, detail = boolean_union_trial(seed, seam_tolerance)
                        if trial is None:
                            notes.append(stage + ": " + detail)
                            continue
                        checked = {}
                        cleanup_mesh(trial, checked)
                        trial_report = inspect_mesh(trial, checked)
                        trial_notes = [detail]
                    else:
                        remove_clashes = stage == "intersection patching"
                        if remove_clashes and not after.get("self_intersections"):
                            continue
                        if (not remove_clashes and not after["naked_edges"]
                                and not after["non_manifold_edges"]):
                            continue
                        if (remove_clashes and fast
                                and after.get("self_intersections", 0) > FAST_LOCAL_CLASH_LIMIT):
                            notes.append("Fast repair: {} intersecting face pairs remain; "
                                         "skipped extensive face cutting and moved to rebuilding.".format(
                                             after["self_intersections"]))
                            break
                        trial, trial_report, trial_notes = deep_local_trial(
                            seed, after, seam_tolerance, remove_clashes)
                    reason = improvement_rejection(after, trial_report)
                    trial_comparison = None
                    if not reason:
                        trial_comparison = compare_geometry(source, trial, is_clean(trial_report))
                        reason = shape_rejection(source, trial, trial_comparison)
                    if reason:
                        notes.append(stage + ": rejected. " + reason)
                    else:
                        if best is not None:
                            best.Dispose()
                        best, trial = trial, None
                        after = trial_report
                        comparison = trial_comparison
                        notes.append(stage + ": accepted. " + " ".join(trial_notes))
                except CheckCancelled:
                    raise
                except Exception as error:
                    notes.append(stage + ": " + str(error))
                finally:
                    if trial is not None:
                        trial.Dispose()

        if not is_clean(after) and allow_rebuild and has_shrinkwrap():
            seed = best if best is not None else source

            if not seed.IsValid or seed.Faces.Count == 0:
                notes.append("Rebuilding skipped because the input is invalid or empty.")
            else:
                edge = 0.0
                try:
                    edge = rebuild_edge if rebuild_edge > 0 else automatic_rebuild_edge(seed)
                    minimum_edge = minimum_rebuild_edge(seed)
                    if edge < minimum_edge:
                        notes.append("Requested rebuild is too dense for the memory budget. "
                                     "Choose an edge length of at least {:.6g} model units, or 0 for automatic.".format(minimum_edge))
                        edge = 0.0
                except CheckCancelled:
                    raise
                except Exception as error:
                    edge = 0.0
                    notes.append("Rebuild resolution could not be determined: {}".format(error))

                attempts = ([(edge, FILL_HOLES, False),
                             (edge * 0.8 if not rebuild_edge else edge, False, False)]
                            if edge > 0 else [])
                if edge > 0 and has_vertex_shrinkwrap():

                    attempts.append((edge, False, True))
                for attempt_index, (target_edge, fill, inflate) in enumerate(attempts):
                    if target_edge <= 0:
                        continue
                    trial = None
                    try:

                        wrap_source = source if inflate and source.IsValid else seed
                        trial = shrinkwrap_trial(wrap_source, target_edge, fill, inflate)
                        if trial is None or trial.Faces.Count == 0:
                            raise RuntimeError("ShrinkWrap did not return a mesh.")
                        if trial.Faces.Count > MAX_REBUILD_OUTPUT_FACES:
                            if not rebuild_edge and attempt_index == 0:
                                larger_edge = target_edge * max(1.25, math.sqrt(
                                    trial.Faces.Count / (MAX_REBUILD_OUTPUT_FACES * 0.8)))
                                attempts[1] = (larger_edge, fill, inflate)
                            raise RuntimeError("Rebuilt mesh has {} faces, beyond the {}-face "
                                               "validation budget. Choose a larger rebuild edge length.".format(
                                                   trial.Faces.Count, MAX_REBUILD_OUTPUT_FACES))
                        reason = bounds_rejection(source, trial, target_edge)
                        if reason:

                            if fast and attempt_index == 0 and len(attempts) > 2:
                                attempts[1] = (0.0, False, False)
                                notes.append("Fast repair: skipped another surface-wrap trial after bounds loss.")
                            raise RuntimeError(reason)
                        checked = {}
                        cleanup_mesh(trial, checked)
                        trial_report = inspect_mesh(trial, checked)
                        if not is_clean(trial_report):
                            raise RuntimeError("Rebuilt mesh did not pass every implemented check.")
                        trial_comparison = compare_geometry(source, trial, True, target_edge)

                        reference_comparison = (compare_geometry(seed, trial, True, target_edge)
                                                if best is not None else trial_comparison)
                        reason = shape_rejection(source, trial, reference_comparison, target_edge)
                        if reason:
                            raise RuntimeError(reason)
                        trial_comparison["cleaned_source_outside_loss"] = reference_comparison["outside_loss_max"]
                        trial_comparison["material_reference"] = (
                            "earlier accepted cleanup" if best is not None else "original model")
                        if best is not None:
                            best.Dispose()
                        best, trial = trial, None
                        after = trial_report
                        comparison = trial_comparison
                        rebuilt = True
                        notes.append("REBUILT with Rhino 8 ShrinkWrap; target edge {:.6g} model units, "
                                     "zero offset, no smoothing. Review fine details and clearances.".format(target_edge))
                        if inflate:
                            notes.append("Sampled-surface reconstruction was used; thin surfaces can become thicker.")
                        break
                    except CheckCancelled:
                        raise
                    except Exception as error:
                        notes.append("ShrinkWrap at {:.6g}: {}".format(target_edge, error))
                    finally:
                        if trial is not None:
                            trial.Dispose()
        if comparison is not None:
            notes.append("Dimensions before: {}; after: {} model units. "
                         "Largest sampled surface change: {:.6g} across {} samples "
                         "(not a maximum-error guarantee).".format(
                             " x ".join("{:.6g}".format(v) for v in comparison["before_dimensions"]),
                             " x ".join("{:.6g}".format(v) for v in comparison["after_dimensions"]),
                             comparison["sampled_max_change"], comparison["samples"]))
            notes.append("Largest sampled original-to-repair distance: {:.6g} model units.".format(
                comparison.get("source_to_repair_max", comparison["sampled_max_change"])))
            if "cleaned_source_outside_loss" in comparison:
                notes.append("Upper bound on sampled distance outside the repaired solid: {:.6g} "
                             "model units (reference: {}). Interior surfaces can disappear when "
                             "parts merge.".format(comparison["cleaned_source_outside_loss"],
                                                 comparison.get("material_reference", "original model")))
        if not is_clean(after):
            notes.append(unresolved_guidance())
        final = dict(after)
        final["repair_notes"] = notes
        final["rebuilt"] = rebuilt
        final["shape_comparison"] = comparison
        final["elapsed_seconds"] = time.time() - started
        return best, before, final
    except BaseException:
        if best is not None:
            best.Dispose()
        raise


def auto_repair(doc, analysis=None, include_analysis=False, allow_rebuild=None, rebuild_edge=None,
                fast_repair=None, rebuild_only=False):
    with operation(doc, "Repairing meshes"):
        result = _auto_repair(doc, analysis, allow_rebuild, rebuild_edge, fast_repair, rebuild_only)
        return result if include_analysis else result[:3]


def _auto_repair(doc, analysis=None, allow_rebuild=None, rebuild_edge=None,
                 fast_repair=None, rebuild_only=False):
    _, failing, before_totals = _use_analysis(doc, analysis)
    started = time.time()
    changed = 0
    rebuilt_count = 0
    repair_notes = []
    undo_record = doc.BeginUndoRecord("Mesh Guard Auto Repair")
    try:
        for obj, original_report in failing:

            if obj.IsLocked or obj.IsReference:
                note = "Skipped locked/reference mesh {}.".format(obj.Id)
                log(note)
                repair_notes.append(note)
                continue
            repaired = None
            try:
                repaired, before, after = repair_mesh(
                    obj.Geometry, doc.ModelAbsoluteTolerance, original_report,
                    allow_rebuild, rebuild_edge, fast_repair, rebuild_only
                )
                name = obj.Attributes.Name or str(obj.Id)
                notes = after.get("repair_notes", [])
                status = "No replacement" if repaired is None else "Candidate improved"
                note = "{}: {} ({:.1f} seconds). {}".format(
                    name, status, after.get("elapsed_seconds", 0.0), " ".join(notes))
                log(note)
                repair_notes.append(note)
                if repaired is None:
                    continue
                checkpoint("Saving hidden original")
                if "_source_crc" in original_report:
                    current = doc.Objects.FindId(obj.Id)
                    if (current is None or current.IsDeleted or current.IsLocked
                            or int(current.Geometry.DataCRC(0)) != original_report["_source_crc"]):
                        raise RuntimeError("The source changed during repair; replacement was skipped.")
                    obj = current
                preserve_original(doc, obj)
                if doc.Objects.Replace(obj.Id, repaired):
                    changed += 1
                    rebuilt_count += int(after.get("rebuilt", False))
                else:
                    note = "Rhino could not replace mesh {}.".format(obj.Id)
                    log(note)
                    repair_notes.append(note)
            except CheckCancelled:
                raise
            except Exception as error:
                note = "Mesh {} was not replaced: {}".format(obj.Id, error)
                log(note)
                repair_notes.append(note)
            finally:
                if repaired is not None:
                    repaired.Dispose()
    finally:

        if undo_record > 0:
            doc.EndUndoRecord(undo_record)
        doc.Views.Redraw()
    final_analysis = analyze_document(doc)
    after_totals = final_analysis[2]
    after_totals["repair_notes"] = repair_notes
    after_totals["rebuilt_count"] = rebuilt_count
    after_totals["rhino_version"] = rhino_major_version()
    after_totals["elapsed_seconds"] = time.time() - started
    return changed, before_totals, after_totals, final_analysis


# RHINO INTERACTIVE TOOLS
def require_active_document(doc):
    active = Rhino.RhinoDoc.ActiveDoc
    if active is None or active.RuntimeSerialNumber != doc.RuntimeSerialNumber:
        raise RuntimeError("Activate the document being checked before continuing.")


def show_problems(doc, analysis=None):
    require_active_document(doc)
    _, failing, _ = _use_analysis(doc, analysis)
    doc.Objects.UnselectAll()
    selected = 0
    for obj, report in failing:
        if doc.Objects.Select(obj.Id, True):
            selected += 1
    if not selected:
        show_message("No failing mesh could be selected. Check visibility and locks.")
        return
    view = doc.Views.ActiveView
    if view is not None:
        view.ActiveViewport.ZoomExtentsSelected()
    doc.Views.Redraw()
    log("In Edge Analysis, choose Naked edges or Non-manifold edges.")
    log("Run watertight_mesh_guard.py again after inspection to recheck.")
    if not Rhino.RhinoApp.RunScript("_ShowEdges _Enter", False):
        log("Rhino could not start ShowEdges. Failing meshes remain selected.")


def advanced_repair(doc, analysis=None):
    require_active_document(doc)
    _, failing, _ = _use_analysis(doc, analysis)
    if not failing:
        return
    doc.Objects.UnselectAll()
    selected_ids = []
    undo_record = doc.BeginUndoRecord("Mesh Guard Advanced Repair Backups")
    try:
        for obj, report in failing:
            if not doc.Objects.Select(obj.Id, True):
                continue
            try:
                preserve_original(doc, obj)
                selected_ids.append(obj.Id)
            except Exception as error:
                doc.Objects.Select(obj.Id, False)
                log("Skipped mesh {}: {}".format(obj.Id, error))
    finally:
        if undo_record > 0:
            doc.EndUndoRecord(undo_record)
    if not selected_ids:
        show_message(
            "No failing mesh could be selected and backed up.\n"
            "Check visibility, locks and the Rhino command history."
        )
        return
    doc.Views.Redraw()
    log("Originals preserved. Use Check Mesh and the native repair controls.")
    log("Run watertight_mesh_guard.py again after manual repair to recheck.")

    if not Rhino.RhinoApp.RunScript("_MeshRepair", True):
        log("Rhino could not open MeshRepair.")


# MAIN REPAIR DIALOG
class RepairDialog(forms.Dialog[forms.DialogResult]):
    def __init__(self, totals, retry=False, seam_tolerance=None,
                 allow_rebuild=None, rebuild_edge=None, fast_repair=None):
        super(RepairDialog, self).__init__()
        self.Title = "Mesh Repair"
        self.Padding = drawing.Padding(12)
        self.Resizable = False
        self.ResultAction = "Done"
        self.AllowRebuild = ALLOW_REBUILD if allow_rebuild is None else allow_rebuild
        self.RebuildEdge = REBUILD_EDGE_LENGTH if rebuild_edge is None else rebuild_edge
        self.FastRepair = FAST_REPAIR if fast_repair is None else fast_repair
        self.rebuild_toggle = None
        self.rebuild_value = None
        self.fast_toggle = None
        self.DetailsText = format_report(totals)

        if not totals["failing"]:
            heading = "MESH CHECK PASSED"
        elif retry:
            heading = "REPAIR INCOMPLETE"
        else:
            heading = "MESH PROBLEMS DETECTED"
        message = heading + " - RHINO {}\n\n".format(rhino_major_version()) + format_compact_report(totals)
        if totals["failing"]:
            message += "\n\nOriginals are backed up before replacement. Repair may close openings or change details."
            if has_shrinkwrap():
                message += "\nRebuild Now skips repairs and changes the surface."
            if not FILL_HOLES:
                message += "\nAutomatic hole filling is off."
            self.DetailsText += "\n\nAdvanced Repair opens Rhino MeshRepair. Rerun this script after manual edits."
            if HEAL_SEAMS and seam_tolerance is not None:
                self.DetailsText += "\nMaximum seam tolerance: {:.6g} model units; also limited by mesh size.".format(seam_tolerance)

        label = forms.Label()
        label.Text = message
        label.Wrap = forms.WrapMode.Word
        label.Width = 620
        buttons = forms.StackLayout()
        buttons.Orientation = forms.Orientation.Horizontal
        buttons.Spacing = 6
        if totals["failing"]:
            for caption, action in (
                ("Show Problems", "Show"),
                ("Auto Repair", "Auto"),
                ("Recheck", "Recheck"),
                ("Advanced Repair", "Advanced"),
            ):
                self.add_button(buttons, caption, action)
            if has_shrinkwrap():
                self.add_button(buttons, "Rebuild Now", "Rebuild")
        self.add_button(buttons, "Details", "Details")
        done = self.add_button(buttons, "Done", "Done")
        self.AbortButton = done
        self.DefaultButton = done
        layout = forms.StackLayout()
        layout.Spacing = 12
        layout.Items.Add(forms.StackLayoutItem(label))
        if totals["failing"] and has_shrinkwrap():
            self.fast_toggle = forms.CheckBox()
            self.fast_toggle.Text = "Fast repair (fewer repair attempts)"
            self.fast_toggle.ToolTip = "When rebuilding is allowed, try fewer local repairs first."
            self.fast_toggle.Checked = self.FastRepair
            layout.Items.Add(forms.StackLayoutItem(self.fast_toggle))
            self.rebuild_toggle = forms.CheckBox()
            self.rebuild_toggle.Text = "Allow rebuilding during Auto Repair"
            self.rebuild_toggle.Checked = self.AllowRebuild
            layout.Items.Add(forms.StackLayoutItem(self.rebuild_toggle))
            row = forms.StackLayout()
            row.Orientation = forms.Orientation.Horizontal
            row.Spacing = 8
            caption = forms.Label()
            caption.Text = "Rebuild edge length (model units; 0 = auto):"
            self.rebuild_value = forms.NumericStepper()
            self.rebuild_value.MinValue = 0
            self.rebuild_value.MaxValue = 1000000000
            self.rebuild_value.DecimalPlaces = 6
            self.rebuild_value.Increment = 0.01
            self.rebuild_value.Value = self.RebuildEdge
            self.rebuild_value.Width = 140
            self.rebuild_value.ToolTip = "Smaller values preserve finer details but take longer and use more memory."
            row.Items.Add(forms.StackLayoutItem(caption))
            row.Items.Add(forms.StackLayoutItem(self.rebuild_value))
            layout.Items.Add(forms.StackLayoutItem(row))
        if retry and totals["failing"]:
            guidance = forms.Label()
            guidance.Text = unresolved_guidance()
            guidance.Wrap = forms.WrapMode.Word
            guidance.Width = 650
            layout.Items.Add(forms.StackLayoutItem(guidance))
        layout.Items.Add(forms.StackLayoutItem(buttons))
        self.Content = layout

    def add_button(self, layout, caption, action):
        button = forms.Button()
        button.Text = caption

        def click(sender, event):
            if action == "Details":
                dialog = ReportDialog("Mesh Check Details", self.DetailsText)
                try:
                    dialog.ShowModal(self)
                finally:
                    dialog.Dispose()
                return
            if self.rebuild_toggle is not None:
                self.AllowRebuild = bool(self.rebuild_toggle.Checked)
                self.RebuildEdge = float(self.rebuild_value.Value)
            if self.fast_toggle is not None:
                self.FastRepair = bool(self.fast_toggle.Checked)
            self.ResultAction = action
            self.Close(forms.DialogResult.Ok)

        button.Click += click
        layout.Items.Add(forms.StackLayoutItem(button))
        return button


class ReportDialog(forms.Dialog[forms.DialogResult]):
    def __init__(self, title, message, details=None):
        super(ReportDialog, self).__init__()
        self.Title = title
        self.Padding = drawing.Padding(12)
        self.Resizable = True
        self.SummaryText = message
        self.DetailsText = details
        self.ShowingDetails = False
        self.ReportText = forms.TextArea()
        self.ReportText.Text = message
        self.ReportText.ReadOnly = True
        self.ReportText.Wrap = True
        self.ReportText.Size = drawing.Size(640, 340)
        buttons = forms.StackLayout()
        buttons.Orientation = forms.Orientation.Horizontal
        buttons.Spacing = 8
        self.DetailsButton = None
        if details:
            self.DetailsButton = forms.Button()
            self.DetailsButton.Text = "Details"
            self.DetailsButton.Click += self.toggle_details
            buttons.Items.Add(forms.StackLayoutItem(self.DetailsButton))
        done = forms.Button()
        done.Text = "Done"
        def close(sender, event):
            self.Close(forms.DialogResult.Ok)
        done.Click += close
        buttons.Items.Add(forms.StackLayoutItem(done))
        layout = forms.StackLayout()
        layout.Spacing = 8
        layout.Items.Add(forms.StackLayoutItem(self.ReportText, True))
        layout.Items.Add(forms.StackLayoutItem(buttons))
        self.Content = layout
        self.DefaultButton = self.AbortButton = done

    def toggle_details(self, sender, event):
        self.ShowingDetails = not self.ShowingDetails
        self.ReportText.Text = self.DetailsText if self.ShowingDetails else self.SummaryText
        self.DetailsButton.Text = "Summary" if self.ShowingDetails else "Details"


def show_repair_result(before, after, changed):
    if after["failing"] == 0:
        heading = "REPAIR COMPLETE"
    elif changed > 0:
        heading = "REPAIR PARTIALLY COMPLETE"
    else:
        heading = "REPAIR INCOMPLETE"
    message = "{}\n\nUpdated: {}    Rebuilt: {}".format(heading, changed, after.get("rebuilt_count", 0))
    if "elapsed_seconds" in after:
        message += "    Time: {:.1f}s".format(after["elapsed_seconds"])
    message += "\n\n" + format_before_after(before, after)
    if changed:
        message += "\n\nHidden originals saved. Save as .3dm to keep backups."
    else:
        message += "\n\nNo meshes replaced."
    if after["failing"]:
        message += "\n" + unresolved_guidance() + "\nSee Details for failed or skipped repairs."
    else:
        message += "\nAll implemented checks passed."
    if after.get("rebuilt_count", 0):
        message += "\nRebuilt geometry changed. Review fine details and dimensions."
    details = "{}\n\nBEFORE\n{}\n\nAFTER\n{}".format(heading, format_report(before), format_report(after))
    notes = after.get("repair_notes", [])
    if notes:
        details += "\n\nREPAIR DETAILS\n" + "\n\n".join(notes)
    log(details)
    dialog = ReportDialog("Mesh Repair Results", message, details)
    try:
        dialog.ShowModal(Rhino.UI.RhinoEtoApp.MainWindow)
    finally:
        dialog.Dispose()


def show_clean_confirmation(file_path, totals):
    source = os.path.basename(file_path) if file_path else "Current document"
    message = (
        "MESH CHECK PASSED\n\n{}\nMeshes checked: {}\n\nAll implemented checks passed."
    ).format(source, totals["objects"])
    log(format_report(totals))
    show_message(message)


# REPAIR WORKFLOW
def process_repair_workflow(doc, analysis=None):
    retry = False
    allow_rebuild, rebuild_edge = ALLOW_REBUILD, REBUILD_EDGE_LENGTH
    fast_repair = FAST_REPAIR
    while True:
        require_active_document(doc)
        analysis = _use_analysis(doc, analysis)
        objects, failing, totals = analysis
        if not objects:
            show_message("There are no user mesh objects to check.")
            return
        dialog = RepairDialog(totals, retry, doc.ModelAbsoluteTolerance, allow_rebuild, rebuild_edge,
                              fast_repair)
        errors = ["{}: {}".format(obj.Attributes.Name or str(obj.Id), error)
                  for obj, report in failing for error in report["errors"]]
        if errors:
            dialog.DetailsText += "\n\nCHECK ERRORS\n" + "\n".join(errors)
        try:
            dialog.ShowModal(Rhino.UI.RhinoEtoApp.MainWindow)
            action = dialog.ResultAction
            allow_rebuild, rebuild_edge = dialog.AllowRebuild, dialog.RebuildEdge
            fast_repair = dialog.FastRepair
        finally:
            dialog.Dispose()
        if action == "Done":
            return
        if action == "Show":
            show_problems(doc, analysis)
            return
        if action == "Advanced":
            advanced_repair(doc, analysis)
            return
        if action in ("Auto", "Rebuild"):
            changed, before, after, analysis = auto_repair(
                doc, analysis, True, allow_rebuild, rebuild_edge, fast_repair, action == "Rebuild")
            show_repair_result(before, after, changed)
            retry = after["failing"] > 0
        elif action == "Recheck":

            analysis = None
            continue
        else:
            return


# STL EVENTS AND DEFERRED CHECKS
def _show_check_error(error):
    if isinstance(error, CheckCancelled):
        log(str(error))
        show_message(str(error), "Mesh Check Cancelled")
    else:
        log("Mesh check stopped: {}".format(error))
        show_message("Mesh check could not finish.\n\n{}".format(error))


def _check_document(doc, file_path=None, notify_empty=False):
    state = sc.sticky[HANDLER_KEY]
    state["busy"] = True
    try:
        objects, failing, totals = analyze_document(doc)
        if not objects:
            log("No user mesh objects found in the checked document.")
            if notify_empty:
                show_message("No meshes to check.\n\nOpen or import an STL; Mesh Guard will check it automatically.",
                             "Mesh Guard Ready")
        elif failing:
            process_repair_workflow(doc, (objects, failing, totals))
        else:
            show_clean_confirmation(file_path, totals)
        return True
    except Exception as error:
        _show_check_error(error)
        return False
    finally:
        state["busy"] = False


def queue_check(doc, file_path=None):
    state = sc.sticky.get(HANDLER_KEY)
    if doc is not None and isinstance(state, dict):

        state["pending"][doc.RuntimeSerialNumber] = file_path


def on_end_open_document(sender, event):
    try:
        file_path = event.FileName
        if not file_path or os.path.splitext(file_path)[1].lower() != ".stl":
            return
        if event.Reference:
            return
        queue_check(event.Document, file_path)
    except Exception as error:
        log("Could not queue STL analysis: {}".format(error))


def on_idle(sender, event):
    state = sc.sticky.get(HANDLER_KEY)
    if not isinstance(state, dict) or state["busy"] or not state["pending"]:
        return
    for serial, file_path in list(state["pending"].items()):
        try:
            doc = Rhino.RhinoDoc.FromRuntimeSerialNumber(serial)
            if doc is None:
                state["pending"].pop(serial, None)
                continue
            active = Rhino.RhinoDoc.ActiveDoc
            if active is None or active.RuntimeSerialNumber != serial:
                continue
            if Rhino.Commands.Command.InCommand() or doc.Views.ActiveView is None:
                continue
            if doc.UndoActive or doc.RedoActive:
                continue
            state["pending"].pop(serial, None)
            _check_document(doc, file_path)
        except Exception as error:
            state["pending"].pop(serial, None)
            state["busy"] = True
            try:
                _show_check_error(error)
            finally:
                state["busy"] = False
        return


def check_now():
    try:
        if not register():
            return False
        doc = Rhino.RhinoDoc.ActiveDoc
        if doc is None:
            show_message("Open a document, then import an STL to check it.", "Mesh Guard Ready")
            return False
        state = sc.sticky[HANDLER_KEY]
        state["pending"].pop(doc.RuntimeSerialNumber, None)
        return _check_document(doc, doc.Path, True)
    except Exception as error:
        _show_check_error(error)
        return False


# EVENT REGISTRATION
def unregister():
    state = sc.sticky.get(HANDLER_KEY)
    if state is None:
        return
    if not isinstance(state, dict):

        log("An older guard is active. Restart Rhino to clear that registration.")
        return

    Rhino.RhinoDoc.EndOpenDocument -= state["end_open_handler"]
    Rhino.RhinoApp.Idle -= state["idle_handler"]
    state["pending"].clear()
    sc.sticky.pop(HANDLER_KEY, None)
    log("Unregistered.")


def register():
    pending = {}
    if HANDLER_KEY in sc.sticky:
        previous = sc.sticky[HANDLER_KEY]
        if not isinstance(previous, dict):
            message = "An older Mesh Guard is still loaded.\nRestart Rhino, then run this file again."
            log(message)
            show_message(message, "Mesh Guard Startup")
            return False
        if previous.get("busy"):
            message = "Mesh Guard is already checking or repairing.\nFinish that operation and close its dialog first."
            log(message)
            show_message(message, "Mesh Guard Busy")
            return False
        if (previous.get("version") == VERSION
                and previous.get("end_open_handler") is on_end_open_document
                and previous.get("idle_handler") is on_idle):
            return True
        pending = dict(previous.get("pending", {}))
        unregister()
    state = {
        "version": VERSION,
        "end_open_handler": on_end_open_document,
        "idle_handler": on_idle,
        "pending": pending,
        "busy": False,
    }
    Rhino.RhinoDoc.EndOpenDocument += state["end_open_handler"]
    try:
        Rhino.RhinoApp.Idle += state["idle_handler"]
    except Exception:
        Rhino.RhinoDoc.EndOpenDocument -= state["end_open_handler"]
        raise
    sc.sticky[HANDLER_KEY] = state
    log("Version {} registered. STL files will be checked after Open/Import.".format(VERSION))
    return True


if __name__ == "__main__":
    check_now()
