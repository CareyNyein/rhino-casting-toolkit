# Rhino Casting Toolkit

A modular CAD automation toolkit for Rhino designed to streamline casting preparation workflows and eliminate 3D printing failures.

This repository provides automated quality checks, geometry healing, and casting preparation tools, with additional automation modules planned.

## 🛠️ Current Module: Watertight Mesh Guard

The primary active module in this release is `watertight_mesh_guard.py`, an event-driven utility that:

- **Inspects STLs Automatically**: Catches naked edges, holes, and non-manifold defects upon opening a file to prevent missing wax layers.
- **One-Click Repairs**: Provides fast in-viewport healing via hole-filling and QuadRemesh rebuilds.
- **Parametric Auto-Spruing**: Generates and Booleans a casting sprue directly to the base of the model.

## 🚀 Quick Setup & Auto-Start

### 1. Download the Toolkit

1. Download `watertight_mesh_guard.py` from this repository.
2. Place it in a dedicated scripts directory, for example:
   - `C:\RhinoScripts\watertight_mesh_guard.py`
   - `Documents\RhinoScripts\watertight_mesh_guard.py`

### 2. Test Run (Manual)

1. Open Rhino.
2. In the command bar, type and run:

   ```text
   RunPythonScript "C:\Path\To\watertight_mesh_guard.py"
   ```

   Update the path to match the location of your script.

3. The command history will confirm registration:

   ```text
   [Watertight Mesh Guard] Registered and monitoring STL files.
   ```

4. Open an STL file using **File > Open** to test the automated diagnostic dialog.

### 3. Enable Auto-Run on Rhino Startup

To have the toolkit activate automatically whenever Rhino starts:

1. In Rhino, open **Tools > Options** (or type `Options` in the command line).
2. Select **General** from the left panel.
3. Locate **Run these commands every time Rhino starts**.
4. Paste your run command:

   ```text
   _-RunPythonScript "C:\Path\To\watertight_mesh_guard.py"
   ```

   Replace the path with your actual script location.

5. Click **OK**.

## 📄 License

Distributed under the MIT License.