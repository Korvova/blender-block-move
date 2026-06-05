bl_info = {
    "name": "Snap Cage",
    "author": "Vladimir (with Claude)",
    "version": (2, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar (N) > Snap  ·  hotkey Ctrl+Shift+Space",
    "description": "Drop an NxNxN snapping grid centred on the selected vertex and keep it "
                   "following the active vertex as you extrude, so you model off it; "
                   "tap again to remove it.",
    "category": "3D View",
}

import bpy
import bmesh
from bpy.props import BoolProperty, FloatProperty, IntProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup

GRID_OBJ_NAME = "SnapCage"

# Default hotkey. Rebind by editing these four lines (then re-enable the add-on),
# or in Preferences > Keymap > 3D View > "Snap Cage".
TRIGGER_KEY = 'SPACE'
TRIGGER_CTRL = True
TRIGGER_SHIFT = True
TRIGGER_ALT = False

addon_keymaps = []

# Background follow loop (bpy.app.timers, not a modal — so it never swallows the
# events that extrude/click need). The cage only re-homes once the active vertex
# has held still for a couple of ticks, so it stays put (a stable snap target)
# during a drag and jumps to the new vertex only after you confirm.
_FOLLOW = {"on": False, "obj": "", "last_co": None, "stable": 0}
_FOLLOW_INTERVAL = 0.08      # seconds between polls
_FOLLOW_STABLE = 2           # ticks the vertex must hold still before the cage moves


# ------------------------------------------------------------------ mesh
def _build_cage(mesh, count, size):
    """Fill `mesh` with a count^3 lattice of verts + connecting edges, centred on
    the local origin, spanning `size` along each axis. No faces on purpose, so it
    never occludes or gets in the way and ray-based tools pass straight through it.
    Odd `count` puts a node exactly at the centre (on the anchored vertex)."""
    count = max(2, count)
    step = size / (count - 1)
    half = size / 2.0

    def idx(ix, iy, iz):
        return (ix * count + iy) * count + iz

    verts = [(ix * step - half, iy * step - half, iz * step - half)
             for ix in range(count) for iy in range(count) for iz in range(count)]
    edges = []
    for ix in range(count):
        for iy in range(count):
            for iz in range(count):
                if ix + 1 < count: edges.append((idx(ix, iy, iz), idx(ix + 1, iy, iz)))
                if iy + 1 < count: edges.append((idx(ix, iy, iz), idx(ix, iy + 1, iz)))
                if iz + 1 < count: edges.append((idx(ix, iy, iz), idx(ix, iy, iz + 1)))
    mesh.clear_geometry()
    mesh.from_pydata(verts, edges, [])
    mesh.update()


def _make_cage(context, count, size, location):
    me = bpy.data.meshes.new(GRID_OBJ_NAME)
    _build_cage(me, count, size)
    obj = bpy.data.objects.new(GRID_OBJ_NAME, me)
    obj.hide_render = True          # it's a guide, never rendered
    obj.show_in_front = True        # draw over solid geometry so it stays visible
    obj.location = location
    coll = context.collection or context.scene.collection
    coll.objects.link(obj)
    return obj


def _remove_cage(obj):
    if obj is None:
        return
    me = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if me and me.users == 0:
        bpy.data.meshes.remove(me)


# ------------------------------------------------------------------ anchor
def _active_vert_world(obj):
    """(index, world position) of the active selected vertex of an edited mesh
    object, or (None, None). Falls back to any selected vertex when there is no
    active one in the selection history."""
    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        return None, None
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    v = bm.select_history.active
    if not (isinstance(v, bmesh.types.BMVert) and v.select):
        v = next((vv for vv in bm.verts if vv.select), None)
    if v is None:
        return None, None
    return v.index, (obj.matrix_world @ v.co.copy())


def _anchor_world(context):
    """World point to centre the cage on, plus a short tag describing the source.

    Edit Mesh: the active selected vertex. Object mode: the 3D cursor, so the tool
    still does something useful. Returns (location, tag) where tag is
    'vertex' | 'cursor' | None (Edit Mesh but nothing selected)."""
    obj = context.edit_object
    if context.mode == 'EDIT_MESH' and obj is not None and obj.type == 'MESH':
        _idx, pos = _active_vert_world(obj)
        if pos is None:
            return None, None
        return pos, 'vertex'
    return context.scene.cursor.location.copy(), 'cursor'


def _enable_vertex_snap(context):
    ts = context.scene.tool_settings
    ts.use_snap = True
    if hasattr(ts, "snap_elements"):
        try: ts.snap_elements = {'VERTEX'}
        except Exception: pass
    if hasattr(ts, "snap_element"):
        try: ts.snap_element = 'VERTEX'
        except Exception: pass


# ------------------------------------------------------------------ follow loop
def _follow_tick():
    if not _FOLLOW["on"]:
        return None
    try:
        cage = bpy.data.objects.get(GRID_OBJ_NAME)
        if cage is None:                       # cage gone (deleted) -> stop following
            _FOLLOW["on"] = False
            return None
        obj = bpy.data.objects.get(_FOLLOW["obj"])
        _idx, pos = _active_vert_world(obj)
        if pos is None:                        # left edit mode / no selection: leave cage put
            _FOLLOW["last_co"] = None
            _FOLLOW["stable"] = 0
            return _FOLLOW_INTERVAL
        last = _FOLLOW["last_co"]
        if last is not None and (pos - last).length < 1e-6:
            _FOLLOW["stable"] += 1
        else:
            _FOLLOW["stable"] = 0
            _FOLLOW["last_co"] = pos.copy()
        # once the active vertex has settled, re-home the cage on it
        if _FOLLOW["stable"] >= _FOLLOW_STABLE and (cage.location - pos).length > 1e-6:
            cage.location = pos
            _tag_redraw()
        return _FOLLOW_INTERVAL
    except Exception as e:
        print("[Snap Cage] follow:", e)
        return _FOLLOW_INTERVAL


def _start_follow(obj):
    _FOLLOW["on"] = True
    _FOLLOW["obj"] = obj.name if obj else ""
    _idx, pos = _active_vert_world(obj)
    _FOLLOW["last_co"] = pos.copy() if pos is not None else None
    _FOLLOW["stable"] = _FOLLOW_STABLE
    if not bpy.app.timers.is_registered(_follow_tick):
        bpy.app.timers.register(_follow_tick, first_interval=_FOLLOW_INTERVAL)


def _stop_follow():
    _FOLLOW["on"] = False
    if bpy.app.timers.is_registered(_follow_tick):
        try:
            bpy.app.timers.unregister(_follow_tick)
        except Exception:
            pass


# ------------------------------------------------------------------ operator
class VIEW3D_OT_snap_cage(Operator):
    bl_idname = "view3d.snap_cage"
    bl_label = "Snap Cage at Vertex"
    bl_description = ("Drop a snapping grid on the selected vertex and keep it on the "
                      "active vertex as you model; run again to remove it")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context):
        # toggle: an existing cage means "remove it" (and stop following)
        existing = bpy.data.objects.get(GRID_OBJ_NAME)
        if existing is not None:
            _stop_follow()
            _remove_cage(existing)
            self.report({'INFO'}, "Snap Cage removed")
            return {'FINISHED'}

        loc, tag = _anchor_world(context)
        if loc is None:
            self.report({'WARNING'}, "Select a vertex in Edit Mode first")
            return {'CANCELLED'}

        s = context.scene.sc_settings
        _make_cage(context, s.count, s.size, loc)
        if s.snap_on_lock:
            _enable_vertex_snap(context)

        if s.auto_follow and tag == 'vertex' and context.edit_object is not None:
            _start_follow(context.edit_object)
            self.report({'INFO'}, "Snap Cage follows the active vertex - run again to stop")
        else:
            _stop_follow()
            where = "selected vertex" if tag == 'vertex' else "3D cursor"
            self.report({'INFO'}, "Snap Cage at {} - run again to remove".format(where))
        return {'FINISHED'}


# ------------------------------------------------------------------ data
def _on_auto_follow(self, context):
    cage = bpy.data.objects.get(GRID_OBJ_NAME)
    if self.auto_follow:
        if cage is not None and context.edit_object is not None:
            _start_follow(context.edit_object)
    else:
        _stop_follow()


class SC_Settings(PropertyGroup):
    count: IntProperty(
        name="Points / axis", default=3, min=2, max=21,
        description="Grid resolution: vertices along each edge of the cage "
                    "(odd values keep a node on the anchored vertex)")
    size: FloatProperty(
        name="Size", default=1.0, min=1e-4, subtype='DISTANCE', unit='LENGTH',
        description="Full width of the cage along each axis")
    snap_on_lock: BoolProperty(
        name="Vertex snap on place", default=True,
        description="Turn on Vertex snapping when the cage is placed")
    auto_follow: BoolProperty(
        name="Follow active vertex", default=True, update=_on_auto_follow,
        description="Keep re-centring the cage on the active vertex as you extrude or "
                    "select, so it is always around the point you build from")


# ------------------------------------------------------------------ panel
def _hotkey_text():
    parts = []
    if TRIGGER_CTRL: parts.append("Ctrl")
    if TRIGGER_SHIFT: parts.append("Shift")
    if TRIGGER_ALT: parts.append("Alt")
    parts.append(TRIGGER_KEY.replace('_', ' ').title())
    return "+".join(parts)


class SC_PT_panel(Panel):
    bl_label = "Snap Cage"
    bl_idname = "SC_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Snap"

    def draw(self, context):
        layout = self.layout
        s = context.scene.sc_settings

        col = layout.column(align=True)
        col.prop(s, "count")
        col.prop(s, "size")
        layout.prop(s, "snap_on_lock", toggle=True,
                    icon='SNAP_ON' if s.snap_on_lock else 'SNAP_OFF')
        layout.prop(s, "auto_follow", toggle=True,
                    icon='TRACKING' if s.auto_follow else 'TRACKING_CLEAR_FORWARDS')

        existing = bpy.data.objects.get(GRID_OBJ_NAME)
        if existing is not None:
            box = layout.box()
            if _FOLLOW["on"]:
                box.label(text="Following active vertex", icon='TRACKING')
            else:
                box.label(text="Cage placed", icon='PINNED')
            box.operator("view3d.snap_cage", text="Remove cage", icon='X')
        else:
            layout.operator("view3d.snap_cage", text="Cage at selected vertex",
                            icon='MESH_GRID')
            layout.label(text="Edit Mode: select a vertex first", icon='INFO')

        layout.separator()
        layout.label(text="Hotkey: {}".format(_hotkey_text()), icon='INFO')
        layout.label(text="select vertex · tap = place · tap = remove")


# ------------------------------------------------------------------ misc
def _tag_redraw():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ------------------------------------------------------------------ registration
classes = (SC_Settings, VIEW3D_OT_snap_cage, SC_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.sc_settings = PointerProperty(type=SC_Settings)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon if wm else None
    if kc:
        km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
        kmi = km.keymap_items.new(
            VIEW3D_OT_snap_cage.bl_idname, type=TRIGGER_KEY, value='PRESS',
            ctrl=TRIGGER_CTRL, shift=TRIGGER_SHIFT, alt=TRIGGER_ALT)
        addon_keymaps.append((km, kmi))


def unregister():
    _stop_follow()

    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()

    if hasattr(bpy.types.Scene, "sc_settings"):
        del bpy.types.Scene.sc_settings
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == "__main__":
    register()
