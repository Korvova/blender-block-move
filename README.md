# Intersection Highlighter

A Blender add-on that highlights when mesh objects touch or intersect, and can
physically block one object from passing through another while you move it.

## Features

- **Live 3-state highlighting** in the viewport:
  - apart → original color
  - touching → blue
  - intersecting → red
- **Check scope**: selected objects (pairwise), the active object vs. the
  others, or every mesh in the scene.
- **Two detection methods**: *Precise* (exact triangle-level test via BVH) or
  *Fast* (axis-aligned bounding boxes).
- **No-intersection lock**: pick two objects and stop one from penetrating the
  other. As you drag, it slides up to contact and is snapped back if it would
  pass through — a "physical wall".
- **Concave-safe**: works with holes and recesses (e.g. a peg dropping to the
  floor of a pocket), not just convex boxes.
- Each object's original viewport color is stored on the object, so highlights
  always restore cleanly — even after reloading the add-on or reopening the file.

## Requirements

- Blender 3.0 or newer.

## Installation

1. Download [`intersection_highlighter.py`](intersection_highlighter.py)
   (use **Code → Download ZIP**, or clone the repo).
2. In Blender: **Edit → Preferences → Add-ons**.
3. Click **Install…** (Blender 4.2+: the **▾** dropdown → **Install from Disk…**)
   and select `intersection_highlighter.py`.
4. Enable **Object: Intersection Highlighter**.
5. In the 3D Viewport press **N** to open the sidebar and pick the **Intersect** tab.

## Usage

### Highlighting

1. Select the mesh objects you want to check.
2. Set **Check** (scope) and **Method**.
3. Click **Check now** for a one-off test, or enable **Live highlight** to update
   continuously as you move objects.
4. Tune **Hit color**, **Touch color**, and **Touch gap** (the largest gap or
   shallow overlap that still counts as "touching").
5. **Clear** turns everything off and restores the original colors.

> Highlighting paints the object's *viewport color*, which is only visible in
> **Solid** shading with color set to **Object**. Leave **Auto viewport color**
> enabled and the add-on switches that on for you.

### No-intersection lock

1. Select exactly two mesh objects.
2. Under **No-intersection lock**, click **Set pair** (the two objects appear in
   the slots).
3. Enable **Block intersection**.
4. Move either object: it can touch its partner but is blocked from passing
   through.

## Notes & limitations

- The block is a **post-move snap-back**: Blender applies the move and the add-on
  reverts it if it caused penetration. During a drag the object can jitter right
  at the boundary, and a very fast flick that skips over contact in a single
  frame stops at the last safe position (just short of contact) rather than
  exactly at contact.
- Works for **unparented** objects moved by their location (not parented or
  constrained rigs).
- Touching is always allowed; only real solid interpenetration is blocked.
