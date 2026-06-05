# Blender Modeling Add-ons

A small collection of Blender add-ons (and one helper script) for precise,
CAD-style modeling and 3D-print prep. Each add-on adds its own tab to the 3D
Viewport sidebar (press **N**).

## Add-ons

### Intersection Highlighter — `intersection_highlighter.py`
Sidebar tab: **Intersect** · Blender 3.0+

Highlights when mesh objects touch or intersect, live or on demand:
- 3-state coloring: apart → original, touching → blue, intersecting → red
- Scope: selected (pairwise), active vs. others, or all meshes
- Precise (triangle/BVH) or Fast (bounding-box) detection
- **No-intersection lock**: pick two objects and block one from passing through
  the other while you drag it (a "physical wall"); concave-safe (holes/recesses)

### Solid Collision — `solid_collision.py`
Sidebar tab: **Solid** · Blender 3.0+

Mark objects as *solid*; moving one pushes the others out of the way so they
never overlap.
- Live mode resolves overlaps as you drag, or **Separate now** on demand
- Adjustable clearance gap and solver iterations
- Bounding-box (AABB) minimum-translation resolution

### Edge Length Editor — `edge_length_editor.py`
Sidebar tab: **Edge** (Edit Mode) · Blender 4.0+

See and edit the length of the selected edge, CAD-style.
- Type a new length to resize the active edge (respects scene units, e.g. mm)
- Resize anchor: from the center, or keep one end (with a flip)
- Viewport overlay drawing each selected edge's length next to it
- **Type in the viewport**: hit *Type in viewport* (panel or right-click menu),
  click a length label, type a value, **Enter** applies and exits
- **Set all selected** resizes every selected edge to the active one's length

### Polar Move — `polar_move.py`
Sidebar tab: **Move** · Blender 4.0+

Place an object — or selected vertices — at a distance and angle from a
reference vertex.
- Pick an **anchor** vertex (the handle) and a **reference** vertex
- Dial **distance + horizontal/vertical angle** (world axes); the anchor lands
  at that polar offset from the reference
- Shows the **current gap** between the two vertices, so it doubles as a measure
  tool and never snaps when you start
- Viewport overlay: a dashed guide to the target with distance/angle labels
- **Live drag** moves the object/vertices in real time as you change the values
- **Type in the viewport**: hit *Type in viewport* (panel or right-click menu),
  click the distance / H / V label, type a value, **Enter** applies and exits
- Object and Vertices modes

### Ruler Mark — `ruler_mark.py`
Sidebar tab: **Ruler** · Blender 4.0+ · use alongside the built-in **Measure** tool

CAD-style helpers that ride on top of Blender's native Measure (ruler) tool — draw
rulers as usual, then edit and stamp them:
- **Pick any ruler** to work on: click it in the viewport (an armed *Pick* mode with a
  viewport hint), or step ◀ ▶; the selected ruler is highlighted
- **Edit its length and angle** (horizontal / vertical) from the panel — the on-screen
  ruler updates live (it re-activates Measure to refresh the cached gizmo)
- **Drop a vertex or Empty** at the ruler's end (or both ends), into a `Marks` mesh /
  `Mark` objects
- **Convert a ruler to a mesh line** (`RulerLine` — edges through all its points)
- **Clear all rulers** in one click

> Rulers are only visible while the Measure tool is the active tool (Blender behaviour).
> In Blender 5.x the ruler data lives in `bpy.data.annotations` → `RulerData3D`.

## Scripts

### View Montage — `view_montage.py`
Not an add-on — run it from Blender's **Text editor** (*Run Script*).

Renders 6 orthographic views (front / back / top / left / right / bottom) of the
target via a fast OpenGL viewport render and tiles them into a single PNG for
quick modeling review. Edit the `PROJECT` / `OUT` paths and `TARGET` at the top
of the file before running.

## Installing an add-on

1. Download the `.py` file (use **Code → Download ZIP**, or clone the repo).
2. In Blender: **Edit → Preferences → Add-ons**.
3. Click **Install…** (Blender 4.2+: the **▾** dropdown → **Install from Disk…**)
   and pick the file.
4. Enable the add-on in the list.
5. In the 3D Viewport press **N** and open the add-on's tab.

## Requirements

- Blender 3.0+ for Intersection Highlighter and Solid Collision
- Blender 4.0+ for Edge Length Editor, Polar Move, and Ruler Mark (they use the GPU /
  `blf` overlay API)
