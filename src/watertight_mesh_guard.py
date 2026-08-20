# -*- coding: utf-8 -*-
import Rhino
import scriptcontext as sc
import os
import math
import Eto.Forms as forms
import Eto.Drawing as drawing

HANDLER_KEY = "WATERTIGHT_MESH_GUARD_ATTACHED"

class WatertightMeshGuardDialog(forms.Dialog[forms.DialogResult]):
    def __init__(self, mesh_count, naked_count, is_retry=False):
        self.Title = "Watertight Mesh Guard"
        self.Padding = drawing.Padding(12)
        self.Resizable = False
        self.ResultAction = "Ignore"

        if is_retry:
            status_header = "[REPAIR INCOMPLETE] {} open mesh(es) and {} naked loop(s) remain.\n\n".format(mesh_count, naked_count)
            status_body = "The previous repair could not fully seal the geometry.\nChoose another method below:"
        else:
            status_header = "[WARNING] Detected {} OPEN mesh(es) with {} naked edge loop(s).\n\n".format(mesh_count, naked_count)
            status_body = "This geometry is not watertight and will cause missing wax layers.\nSelect a repair or diagnostic action:"

        msg_label = forms.Label()
        msg_label.Text = status_header + status_body

        btn_remesh = forms.Button(Text="QuadRemesh (Rebuild)")
        btn_remesh.Click += self.on_remesh_click

        btn_fill = forms.Button(Text="Auto-Fill Holes")
        btn_fill.Click += self.on_fill_click

        btn_wizard = forms.Button(Text="Launch MeshRepair")
        btn_wizard.Click += self.on_wizard_click

        btn_show = forms.Button(Text="Show Bad Edges")
        btn_show.Click += self.on_show_click

        btn_ignore = forms.Button(Text="Ignore / Done")
        btn_ignore.Click += self.on_ignore_click

        btn_layout = forms.StackLayout()
        btn_layout.Orientation = forms.Orientation.Horizontal
        btn_layout.Spacing = 6
        btn_layout.Items.Add(forms.StackLayoutItem(btn_fill))
        btn_layout.Items.Add(forms.StackLayoutItem(btn_remesh))
        btn_layout.Items.Add(forms.StackLayoutItem(btn_wizard))
        btn_layout.Items.Add(forms.StackLayoutItem(btn_show))
        btn_layout.Items.Add(forms.StackLayoutItem(btn_ignore))

        main_layout = forms.StackLayout()
        main_layout.Spacing = 12
        main_layout.Items.Add(forms.StackLayoutItem(msg_label))
        main_layout.Items.Add(forms.StackLayoutItem(btn_layout))

        self.Content = main_layout

    def on_remesh_click(self, sender, e):
        self.ResultAction = "Remesh"
        self.Close()

    def on_fill_click(self, sender, e):
        self.ResultAction = "Fill"
        self.Close()

    def on_wizard_click(self, sender, e):
        self.ResultAction = "Wizard"
        self.Close()

    def on_show_click(self, sender, e):
        self.ResultAction = "Show"
        self.Close()

    def on_ignore_click(self, sender, e):
        self.ResultAction = "Ignore"
        self.Close()


def get_open_mesh_objects(doc):
    open_objects = []
    total_naked_loops = 0
    for obj in doc.Objects:
        if isinstance(obj.Geometry, Rhino.Geometry.Mesh):
            mesh = obj.Geometry
            if not mesh.IsClosed:
                open_objects.append(obj)
                naked = mesh.GetNakedEdges()
                if naked:
                    total_naked_loops += len(naked)
    return open_objects, total_naked_loops


def execute_auto_fill(doc):
    open_objects, _ = get_open_mesh_objects(doc)
    for obj in open_objects:
        mesh = obj.Geometry.DuplicateMesh()
        mesh.FillHoles()
        mesh.Weld(math.pi)
        mesh.Faces.CullDegenerateFaces()
        mesh.UnifyNormals()
        mesh.Normals.ComputeNormals()
        mesh.Compact()
        doc.Objects.Replace(obj.Id, mesh)

    doc.Views.Redraw()


def execute_quad_remesh(doc):
    open_objects, _ = get_open_mesh_objects(doc)
    meshes_to_remesh = [obj.Geometry for obj in open_objects if isinstance(obj.Geometry, Rhino.Geometry.Mesh)]
    if not meshes_to_remesh:
        return

    if len(meshes_to_remesh) > 1:
        joined = Rhino.Geometry.Mesh()
        for m in meshes_to_remesh:
            joined.Append(m)
        target_mesh = joined
    else:
        target_mesh = meshes_to_remesh[0]

    target_mesh.Weld(math.pi)
    target_mesh.UnifyNormals()
    target_mesh.Normals.ComputeNormals()

    params = Rhino.Geometry.QuadRemeshParameters()
    params.TargetQuadCount = 15000
    params.AdaptiveSize = 50.0
    params.DetectHardEdges = True

    new_mesh = target_mesh.QuadRemesh(params)

    if new_mesh and new_mesh.IsValid:
        new_mesh.FillHoles()
        new_mesh.Weld(math.pi)
        new_mesh.UnifyNormals()
        new_mesh.Normals.ComputeNormals()
        new_mesh.Compact()

        for obj in open_objects:
            doc.Objects.Delete(obj.Id, True)
        
        doc.Objects.AddMesh(new_mesh)
        doc.Views.Redraw()
    else:
        Rhino.UI.Dialogs.ShowMessageBox("QuadRemesh could not automatically solve this geometry.", "Watertight Mesh Guard")


def process_repair_workflow(doc):
    is_retry = False

    while True:
        open_objects, total_naked = get_open_mesh_objects(doc)

        if not open_objects:
            Rhino.UI.Dialogs.ShowMessageBox(
                "[SUCCESS] All meshes are closed and watertight!\nModel is ready for slicing.",
                "Watertight Mesh Guard"
            )
            break

        dialog = WatertightMeshGuardDialog(len(open_objects), total_naked, is_retry=is_retry)
        dialog.ShowModal(Rhino.UI.RhinoEtoApp.MainWindow)

        if dialog.ResultAction == "Ignore":
            break

        elif dialog.ResultAction == "Show":
            doc.Objects.UnselectAll()
            for obj in open_objects:
                doc.Objects.Select(obj.Id, True)
            Rhino.RhinoApp.RunScript("_ShowEdges _Enter", False)
            doc.Views.ActiveView.ActiveViewport.ZoomExtentsSelected()
            doc.Views.Redraw()
            break

        elif dialog.ResultAction == "Fill":
            execute_auto_fill(doc)
            is_retry = True

        elif dialog.ResultAction == "Remesh":
            execute_quad_remesh(doc)
            is_retry = True

        elif dialog.ResultAction == "Wizard":
            doc.Objects.UnselectAll()
            for obj in open_objects:
                doc.Objects.Select(obj.Id, True)
            Rhino.RhinoApp.RunScript("! _MeshRepair", True)
            is_retry = True


def on_end_open_document(sender, e):
    file_path = e.FileName
    if not file_path:
        return

    _, ext = os.path.splitext(file_path.lower())
    if ext != ".stl":
        return

    doc = e.Document
    open_objects, _ = get_open_mesh_objects(doc)

    if not open_objects:
        Rhino.RhinoApp.WriteLine("[Watertight Mesh Guard] STL '{}' is verified watertight.".format(os.path.basename(file_path)))
        return

    process_repair_workflow(doc)


def register():
    if HANDLER_KEY in sc.sticky:
        Rhino.RhinoApp.WriteLine("[Watertight Mesh Guard] Already active.")
        return

    Rhino.RhinoDoc.EndOpenDocument += on_end_open_document
    sc.sticky[HANDLER_KEY] = True
    Rhino.RhinoApp.WriteLine("[Watertight Mesh Guard] Registered and monitoring STL files.")

if __name__ == "__main__":
    register()