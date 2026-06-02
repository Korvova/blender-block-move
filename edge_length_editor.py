bl_info = {
    "name": "Edge Length Editor",
    "author": "Vladimir (with Claude)",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Edit Mode > Sidebar (N) > Edge",
    "description": "Show and edit the length of the selected edge, CAD-style, with a viewport overlay.",
    "category": "Mesh",
}

import bpy
import bmesh
import blf
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty
from bpy.types import PropertyGroup, Panel, Operator
from bpy_extras import view3d_utils

# Cap on how many length labels we draw at once. With a huge edge selection the
# viewport would turn into an unreadable wall of numbers (and cost a glyph pass
# per label every redraw), so past this we keep only the active edge plus a slice.
_MAX_LABELS = 100

# The viewport draw handle is stashed in the driver namespace rather than a plain
# module global: that dict survives a script reload, so re-running the addon can
# find and remove the previous handler instead of leaking a second one that would
# double-draw every label.
_NS_KEY = "_ele_draw_handle"

# meters-per-unit and suffix for each explicit length unit, so the overlay text
# matches what Blender prints in a DISTANCE field (to_string() would pick its own
# adaptive unit and disagree with the field, e.g. show cm while the field shows mm).
_UNIT_TABLE = {
    'METRIC': {
        'KILOMETERS': (1000.0, "km"),
        'METERS': (1.0, "m"),
        'CENTIMETERS': (0.01, "cm"),
        'MILLIMETERS': (0.001, "mm"),
        'MICROMETERS': (1e-6, "µm"),
    },
    'IMPERIAL': {
        'MILES': (1609.344, "mi"),
        'FEET': (0.3048, "ft"),
        'INCHES': (0.0254, "in"),
        'THOU': (2.54e-5, "thou"),
    },
}


# ---------------------------------------------------------------- helpers
def _active_or_first_edge(bm):
    """The edge whose length we report/edit: the active one if it's a selected
    edge, otherwise the first selected edge (so a plain box-select still works)."""
    act = bm.select_history.active
    if isinstance(act, bmesh.types.BMEdge) and act.select:
        return act
    for e in bm.edges:
        if e.select:
            return e
    return None


def _edge_world_length(obj, edge):
    mw = obj.matrix_world
    a = mw @ edge.verts[0].co
    b = mw @ edge.verts[1].co
    return (b - a).length


def _resize_edge(obj, edge, new_len, anchor, flip):
    """Move the edge's two vertices so its world-space length becomes new_len.

    Work in world space (so the number means real length even on a moved/rotated
    object) and convert the new positions back through the inverse object matrix.
    Note: with a non-uniform object scale the world direction and local direction
    differ, so length editing is only exact once scale is applied (Ctrl+A)."""
    if new_len <= 0.0:
        return
    mw = obj.matrix_world
    mw_inv = mw.inverted()
    v0, v1 = edge.verts[0], edge.verts[1]
    p0 = mw @ v0.co
    p1 = mw @ v1.co
    d = p1 - p0
    cur = d.length
    if cur < 1e-9:                       # zero-length edge has no direction to grow along
        return
    dirv = d / cur
    if anchor == 'CENTER':
        mid = (p0 + p1) * 0.5
        half = dirv * (new_len * 0.5)
        v0.co = mw_inv @ (mid - half)
        v1.co = mw_inv @ (mid + half)
    else:                               # FIXED: one end stays, the other slides
        keep, move = (v1, v0) if flip else (v0, v1)
        pk = mw @ keep.co
        sign = 1.0 if move is v1 else -1.0
        move.co = mw_inv @ (pk + dirv * (new_len * sign))


def _format_length(scene, value):
    """Length text matching the N-panel field's unit (not to_string's adaptive one)."""
    us = scene.unit_settings
    v = value * us.scale_length
    system = us.system
    if system == 'NONE':
        return "{:.4g}".format(v)
    unit = us.length_unit
    if unit == 'ADAPTIVE':
        try:
            return bpy.utils.units.to_string(system, 'LENGTH', v, precision=4)
        except Exception:
            return "{:.4g}".format(v)
    fac, suf = _UNIT_TABLE.get(system, {}).get(unit, (1.0, ""))
    return "{:.4g} {}".format(v / fac, suf)


def _tag_redraw(context):
    wm = context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _redraw_update(self, context):
    _tag_redraw(context)


# ---------------------------------------------------------------- length get/set
# `length` is a computed property: get reads the active edge live (so it always
# reflects the current selection without us tracking selection changes), and set
# resizes that edge. Storing nothing avoids the value going stale.
def _get_length(self):
    obj = bpy.context.edit_object
    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        return 0.0
    bm = bmesh.from_edit_mesh(obj.data)
    e = _active_or_first_edge(bm)
    if e is None:
        return 0.0
    return _edge_world_length(obj, e)


def _set_length(self, value):
    obj = bpy.context.edit_object
    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        return
    bm = bmesh.from_edit_mesh(obj.data)
    e = _active_or_first_edge(bm)
    if e is None:
        return
    _resize_edge(obj, e, value, self.anchor, self.flip)
    # only vertex coords changed, never topology -> no destructive rebuild
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
    _tag_redraw(bpy.context)


# ---------------------------------------------------------------- overlay
def _draw_overlay():
    ctx = bpy.context
    try:
        s = ctx.scene.ele_settings
    except AttributeError:
        return
    if not s.overlay:
        return
    obj = ctx.edit_object
    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        return
    region = ctx.region
    rv3d = ctx.region_data
    if region is None or rv3d is None:
        return

    bm = bmesh.from_edit_mesh(obj.data)
    active = _active_or_first_edge(bm)
    if active is None:
        return
    mw = obj.matrix_world
    scene = ctx.scene

    if s.show_all:
        edges = [e for e in bm.edges if e.select]
        if len(edges) > _MAX_LABELS:    # keep the active edge, trim the rest
            edges = [active] + [e for e in edges if e is not active][:_MAX_LABELS - 1]
    else:
        edges = [active]

    font_id = 0
    ui = ctx.preferences.system.ui_scale
    blf.size(font_id, 13.0 * ui)
    blf.enable(font_id, blf.SHADOW)
    blf.shadow(font_id, 3, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(font_id, 1, -1)

    for e in edges:
        a = mw @ e.verts[0].co
        b = mw @ e.verts[1].co
        co = view3d_utils.location_3d_to_region_2d(region, rv3d, (a + b) * 0.5)
        if co is None:                  # midpoint behind the camera / clipped
            continue
        text = _format_length(scene, (b - a).length)
        w, h = blf.dimensions(font_id, text)
        if e is active:
            blf.color(font_id, 1.0, 0.85, 0.1, 1.0)
        else:
            blf.color(font_id, 1.0, 1.0, 1.0, 0.85)
        blf.position(font_id, co.x - w * 0.5, co.y + 6.0 * ui, 0.0)
        blf.draw(font_id, text)

    # mark the pinned endpoint so it's obvious which way a FIXED resize will grow
    if s.anchor == 'FIXED':
        keep = active.verts[1] if s.flip else active.verts[0]
        cok = view3d_utils.location_3d_to_region_2d(region, rv3d, mw @ keep.co)
        if cok is not None:
            mk = "●"               # ● filled dot
            blf.size(font_id, 11.0 * ui)
            blf.color(font_id, 1.0, 0.85, 0.1, 1.0)
            mw_, mh_ = blf.dimensions(font_id, mk)
            blf.position(font_id, cok.x - mw_ * 0.5, cok.y - mh_ * 0.5, 0.0)
            blf.draw(font_id, mk)

    blf.disable(font_id, blf.SHADOW)


# ---------------------------------------------------------------- data
class ELE_Settings(PropertyGroup):
    length: FloatProperty(
        name="Length",
        description="World-space length of the active edge. Type a value to resize it",
        subtype='DISTANCE', unit='LENGTH',
        get=_get_length, set=_set_length,
    )
    anchor: EnumProperty(
        name="Anchor",
        items=[
            ('CENTER', "From center", "Both ends move symmetrically; the edge midpoint stays put"),
            ('FIXED', "Keep one end", "One endpoint stays put; only the other end moves"),
        ],
        default='CENTER',
        update=_redraw_update,
    )
    flip: BoolProperty(
        name="Swap fixed end",
        description="Switch which endpoint stays put when resizing",
        default=False,
        update=_redraw_update,
    )
    overlay: BoolProperty(
        name="Viewport overlay",
        description="Draw each selected edge's length next to it in the viewport",
        default=True,
        update=_redraw_update,
    )
    show_all: BoolProperty(
        name="Label all selected",
        description="Show a length label on every selected edge, not just the active one",
        default=True,
        update=_redraw_update,
    )


# ---------------------------------------------------------------- operators
class ELE_OT_apply_to_selected(Operator):
    bl_idname = "mesh.ele_apply_to_selected"
    bl_label = "Set all selected"
    bl_description = ("Resize every selected edge to the active edge's length. "
                      "On connected edges the result is order-dependent (shared "
                      "vertices move), so it works best on separate edges")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        obj = context.edit_object
        s = context.scene.ele_settings
        target = s.length                       # live length of the active edge
        if target <= 0.0:
            self.report({'WARNING'}, "Select an edge with a positive length first")
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(obj.data)
        sel = [e for e in bm.edges if e.select]
        if not sel:
            self.report({'WARNING'}, "No edges selected")
            return {'CANCELLED'}
        for e in sel:
            _resize_edge(obj, e, target, s.anchor, s.flip)
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
        _tag_redraw(context)
        self.report({'INFO'}, "Resized {} edge(s)".format(len(sel)))
        return {'FINISHED'}


# ---------------------------------------------------------------- panel
class ELE_PT_panel(Panel):
    bl_label = "Edge Length"
    bl_idname = "ELE_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Edge"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def draw(self, context):
        layout = self.layout
        s = context.scene.ele_settings
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data) if obj and obj.type == 'MESH' else None
        active = _active_or_first_edge(bm) if bm else None
        n_sel = sum(1 for e in bm.edges if e.select) if bm else 0

        if active is None:
            layout.label(text="Select an edge", icon='INFO')
        else:
            layout.prop(s, "length", text="Length")
            if n_sel > 1:
                layout.label(text="active of {} selected".format(n_sel), icon='EDGESEL')

        box = layout.box()
        box.label(text="Resize anchor")
        box.prop(s, "anchor", expand=True)
        if s.anchor == 'FIXED':
            box.prop(s, "flip", toggle=True, icon='ARROW_LEFTRIGHT')

        if n_sel > 1:
            layout.operator("mesh.ele_apply_to_selected", icon='CHECKMARK')

        col = layout.column(align=True)
        col.prop(s, "overlay", toggle=True,
                 icon='HIDE_OFF' if s.overlay else 'HIDE_ON')
        if s.overlay:
            col.prop(s, "show_all")


# ---------------------------------------------------------------- registration
classes = (ELE_Settings, ELE_OT_apply_to_selected, ELE_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.ele_settings = PointerProperty(type=ELE_Settings)

    ns = bpy.app.driver_namespace
    old = ns.get(_NS_KEY)
    if old is not None:                  # drop a handle left by a previous load
        try:
            bpy.types.SpaceView3D.draw_handler_remove(old, 'WINDOW')
        except Exception:
            pass
    ns[_NS_KEY] = bpy.types.SpaceView3D.draw_handler_add(
        _draw_overlay, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    ns = bpy.app.driver_namespace
    h = ns.get(_NS_KEY)
    if h is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        except Exception:
            pass
        ns[_NS_KEY] = None

    if hasattr(bpy.types.Scene, "ele_settings"):
        del bpy.types.Scene.ele_settings
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == "__main__":
    register()
