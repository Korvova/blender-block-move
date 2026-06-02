bl_info = {
    "name": "Polar Move",
    "author": "Vladimir (with Claude)",
    "version": (1, 2, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar (N) > Move",
    "description": "Place an object (or vertices) at a distance and angle from a reference vertex, "
                   "showing the current gap, with a viewport preview and live drag.",
    "category": "Object",
}

import bpy
import bmesh
import blf
import gpu
import math
from gpu_extras.batch import batch_for_shader
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty,
                       IntProperty, StringProperty, PointerProperty)
from bpy.types import PropertyGroup, Panel, Operator
from bpy_extras import view3d_utils
from mathutils import Vector, Matrix

# Draw handle kept in the driver namespace so a script reload can drop the old one
# instead of leaking a second handler that double-draws (no public list to scan).
_NS_KEY = "_pm_draw_handle"

# meters-per-unit + suffix per explicit length unit, so the overlay text matches a
# DISTANCE field (to_string() picks its own adaptive unit and would disagree).
_UNIT_TABLE = {
    'METRIC': {'KILOMETERS': (1000.0, "km"), 'METERS': (1.0, "m"),
               'CENTIMETERS': (0.01, "cm"), 'MILLIMETERS': (0.001, "mm"),
               'MICROMETERS': (1e-6, "µm")},
    'IMPERIAL': {'MILES': (1609.344, "mi"), 'FEET': (0.3048, "ft"),
                 'INCHES': (0.0254, "in"), 'THOU': (2.54e-5, "thou")},
}

# overlay colors (r,g,b,a)
_C_MAIN = (1.0, 0.70, 0.10, 1.0)    # the move vector + target
_C_REF = (0.20, 0.80, 1.0, 1.0)     # reference / from point
_C_ANCHOR = (1.0, 1.0, 1.0, 1.0)    # current anchor
_C_HORIZ = (0.30, 0.90, 0.40, 0.8)  # horizontal leg (azimuth)
_C_VERT = (0.45, 0.65, 1.0, 0.85)   # vertical leg (elevation)

# live-drag rest state for Vertices mode (transient, session-only by design — a
# variable-length set of vert positions doesn't fit a fixed-size scene property).
_VERT_REST = {}   # {vert_index: local Vector} of the dragged verts at capture time
_VERT_OBJ = ""    # name of the object whose verts we're live-dragging


# ---------------------------------------------------------------- math helpers
def _polar_vector(dist, az, el):
    """World offset from spherical coords: az in XY from +X (CCW from above),
    el tilts up out of XY."""
    ce = math.cos(el)
    return Vector((dist * ce * math.cos(az),
                   dist * ce * math.sin(az),
                   dist * math.sin(el)))


def _active_vert(bm):
    act = bm.select_history.active
    if isinstance(act, bmesh.types.BMVert) and act.select:
        return act
    for v in bm.verts:
        if v.select:
            return v
    return None


def _vert_world_pos(obj, idx):
    """World position of vertex `idx`, reading live edit-mode data when needed."""
    if obj is None or idx < 0 or obj.type != 'MESH':
        return None
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        if idx < len(bm.verts):
            return obj.matrix_world @ bm.verts[idx].co.copy()
    else:
        vs = obj.data.vertices
        if idx < len(vs):
            return obj.matrix_world @ vs[idx].co.copy()
    return None


def _mat_from_flat(flat):
    return Matrix((flat[0:4], flat[4:8], flat[8:12], flat[12:16]))


def _move_object(obj, delta):
    mw = obj.matrix_world.copy()
    mw.translation = mw.translation + delta
    obj.matrix_world = mw          # setter recomputes local loc through any parent


def _move_selected_verts(obj, delta):
    bm = bmesh.from_edit_mesh(obj.data)
    mw = obj.matrix_world
    mw_inv = mw.inverted()
    sel = [v for v in bm.verts if v.select]
    for v in sel:
        v.co = mw_inv @ ((mw @ v.co) + delta)
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
    return len(sel)


def _format_length(scene, value):
    us = scene.unit_settings
    v = value * us.scale_length
    if us.system == 'NONE':
        return "{:.4g}".format(v)
    unit = us.length_unit
    if unit == 'ADAPTIVE':
        try:
            return bpy.utils.units.to_string(us.system, 'LENGTH', v, precision=4)
        except Exception:
            return "{:.4g}".format(v)
    fac, suf = _UNIT_TABLE.get(us.system, {}).get(unit, (1.0, ""))
    return "{:.4g} {}".format(v / fac, suf)


def _tag_redraw(context):
    wm = context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ---------------------------------------------------------------- placement core
def _compute(context):
    """(anchor_pos, from_pos, vec, target) in world space for display/placement,
    or None if the anchor (or a set reference) can't be resolved.

    from_pos is the reference vertex; with no reference it's the anchor's current
    spot (or, while live-dragging, the captured rest spot so the guide stays put)."""
    s = context.scene.pm_settings
    obj = bpy.data.objects.get(s.anchor_object)
    anchor_pos = _vert_world_pos(obj, s.anchor_vindex)
    if anchor_pos is None:
        return None
    vec = _polar_vector(s.distance, s.azimuth, s.elevation)
    if s.ref_set:
        from_pos = _vert_world_pos(bpy.data.objects.get(s.ref_object), s.ref_vindex)
        if from_pos is None:
            return None
    elif s.live and s.has_base:
        from_pos = Vector(s.base_anchor)
    else:
        from_pos = anchor_pos
    return anchor_pos, from_pos, vec, from_pos + vec


def _clear_vert_rest():
    global _VERT_REST, _VERT_OBJ
    _VERT_REST = {}
    _VERT_OBJ = ""


def _capture_base(context):
    """Snapshot the rest state as the live baseline. Object mode: the object's
    matrix + anchor world pos. Vertices mode: the selected verts' local positions
    (in the edited object) + anchor world pos. Always measured from this so a drag
    never accumulates."""
    global _VERT_REST, _VERT_OBJ
    s = context.scene.pm_settings
    if s.move_mode == 'OBJECT':
        obj = bpy.data.objects.get(s.anchor_object)
        apos = _vert_world_pos(obj, s.anchor_vindex)
        if obj is None or apos is None:
            s.has_base = False
            return False
        s.base_anchor = apos
        s.base_mat = [c for row in obj.matrix_world for c in row]
        s.has_base = True
        return True
    # Vertices mode
    obj = context.edit_object
    apos = _vert_world_pos(bpy.data.objects.get(s.anchor_object), s.anchor_vindex)
    if obj is None or obj.type != 'MESH' or apos is None:
        s.has_base = False
        return False
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    _VERT_REST = {v.index: v.co.copy() for v in bm.verts if v.select}
    _VERT_OBJ = obj.name
    if not _VERT_REST:
        s.has_base = False
        return False
    s.base_anchor = apos
    s.has_base = True
    return True


def _apply_live(context):
    """Reposition from the captured baseline so the anchor lands on target."""
    s = context.scene.pm_settings
    if not s.has_base:
        return
    vec = _polar_vector(s.distance, s.azimuth, s.elevation)
    if s.ref_set:
        from_pos = _vert_world_pos(bpy.data.objects.get(s.ref_object), s.ref_vindex)
        if from_pos is None:
            return
    else:
        from_pos = Vector(s.base_anchor)
    delta = (from_pos + vec) - Vector(s.base_anchor)

    if s.move_mode == 'OBJECT':
        obj = bpy.data.objects.get(s.anchor_object)
        if obj is None:
            return
        base = _mat_from_flat(s.base_mat)
        m = base.copy()
        m.translation = base.translation + delta
        obj.matrix_world = m
        context.view_layer.update()
    else:  # Vertices: shift each captured vert by the same world delta
        obj = bpy.data.objects.get(_VERT_OBJ)
        if obj is None or obj.mode != 'EDIT':
            return
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        mw = obj.matrix_world
        mw_inv = mw.inverted()
        n = len(bm.verts)
        for vidx, rest in _VERT_REST.items():
            if vidx < n:
                bm.verts[vidx].co = mw_inv @ ((mw @ rest) + delta)
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)


def _both_set(s):
    return (bool(s.anchor_object) and s.anchor_vindex >= 0
            and s.ref_set and bool(s.ref_object) and s.ref_vindex >= 0)


def _measure_current(context):
    """Current distance + angles of the (anchor - reference) vector, or None.

    Lets the panel read the live gap between the two picked vertices and start
    editing from it instead of from zero (zero would snap the anchor onto the
    reference the moment live drag turns on)."""
    s = context.scene.pm_settings
    apos = _vert_world_pos(bpy.data.objects.get(s.anchor_object), s.anchor_vindex)
    rpos = _vert_world_pos(bpy.data.objects.get(s.ref_object), s.ref_vindex)
    if apos is None or rpos is None:
        return None
    d = apos - rpos
    return d.length, math.atan2(d.y, d.x), math.atan2(d.z, math.hypot(d.x, d.y))


def _fill_current(context):
    """Set distance/azimuth/elevation to the current gap with a raw write (no
    update callback), so syncing to the measured value never moves anything."""
    m = _measure_current(context)
    if m is None:
        return False
    s = context.scene.pm_settings
    s["distance"], s["azimuth"], s["elevation"] = m
    return True


# ---------------------------------------------------------------- update callbacks
def _redraw_update(self, context):
    _tag_redraw(context)


def _on_param_update(self, context):
    if self.live and self.has_base:
        _apply_live(context)
    _tag_redraw(context)


def _on_live_toggle(self, context):
    if self.live:
        if _capture_base(context):
            _apply_live(context)
        else:
            self.live = False          # nothing valid to drag; revert the toggle
    else:
        self.has_base = False
        _clear_vert_rest()
    _tag_redraw(context)


def _on_mode_change(self, context):
    if self.live:
        self.live = False              # leaving OBJECT mode ends a live drag
    _tag_redraw(context)


# ---------------------------------------------------------------- overlay
def _dash_2d(a, b, dash=11.0, gap=7.0):
    """Screen-space dashed segment a->b as a flat list of LINES endpoints."""
    seg = b - a
    L = seg.length
    if L < 1.0:
        return [a, b]
    d = seg / L
    out = []
    t = 0.0
    while t < L:
        t2 = min(t + dash, L)
        out.append(a + d * t)
        out.append(a + d * t2)
        t = t2 + gap
    return out


def _draw_overlay():
    ctx = bpy.context
    try:
        s = ctx.scene.pm_settings
    except AttributeError:
        return
    if not s.show_preview:
        return
    region = ctx.region
    rv3d = ctx.region_data
    if region is None or rv3d is None:
        return
    comp = _compute(ctx)
    if comp is None:
        return
    anchor_pos, from_pos, vec, target = comp

    foot = Vector((target.x, target.y, from_pos.z))     # corner of the right triangle

    def p2(co):
        return view3d_utils.location_3d_to_region_2d(region, rv3d, co)
    a2, t2, f2, an2 = p2(from_pos), p2(target), p2(foot), p2(anchor_pos)

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    def emit(pts, color, width=1.5):
        coords = [(p.x, p.y, 0.0) for p in pts if p is not None]
        if len(coords) < 2:
            return
        gpu.state.line_width_set(width)
        batch = batch_for_shader(shader, 'LINES', {"pos": coords})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)

    def dots(pts, color, size):
        coords = [(p.x, p.y, 0.0) for p in pts if p is not None]
        if not coords:
            return
        gpu.state.point_size_set(size)
        batch = batch_for_shader(shader, 'POINTS', {"pos": coords})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)

    horiz = (foot - from_pos).length > 1e-9
    vert = abs(target.z - from_pos.z) > 1e-9
    if horiz and a2 and f2:
        emit([a2, f2], _C_HORIZ, 1.3)
    if vert and f2 and t2:
        emit([f2, t2], _C_VERT, 1.3)
    if a2 and t2:
        emit(_dash_2d(a2, t2), _C_MAIN, 2.0)

    dots([a2], _C_REF, 8.0)
    dots([an2], _C_ANCHOR, 6.0)
    dots([t2], _C_MAIN, 10.0)

    gpu.state.line_width_set(1.0)
    gpu.state.point_size_set(1.0)
    gpu.state.blend_set('NONE')

    # ---- text labels ----
    font = 0
    ui = ctx.preferences.system.ui_scale
    blf.size(font, 13.0 * ui)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(font, 1, -1)

    def label(p2d, text, color, dx=0.0, dy=6.0):
        if p2d is None:
            return
        w, h = blf.dimensions(font, text)
        blf.color(font, *color)
        blf.position(font, p2d.x - w * 0.5 + dx, p2d.y + dy * ui, 0.0)
        blf.draw(font, text)

    if a2 and t2 and vec.length > 1e-9:
        label((a2 + t2) * 0.5, _format_length(ctx.scene, vec.length), _C_MAIN)
    if horiz and a2 and f2:
        label((a2 + f2) * 0.5, "H {:.1f}°".format(math.degrees(s.azimuth)),
              _C_HORIZ, dy=-14.0)
    if vert and f2 and t2:
        label((f2 + t2) * 0.5, "V {:.1f}°".format(math.degrees(s.elevation)),
              _C_VERT, dx=18.0, dy=0.0)

    blf.disable(font, blf.SHADOW)


# ---------------------------------------------------------------- data
class PM_Settings(PropertyGroup):
    move_mode: EnumProperty(
        name="Move",
        items=[
            ('OBJECT', "Object", "Move the whole anchor object"),
            ('VERTS', "Vertices", "Move the selected vertices of the edited object"),
        ],
        default='OBJECT',
        update=_on_mode_change,
    )
    distance: FloatProperty(
        name="Distance", subtype='DISTANCE', unit='LENGTH', default=0.0,
        description="How far the anchor lands from the reference",
        update=_on_param_update,
    )
    azimuth: FloatProperty(
        name="Horizontal", subtype='ANGLE', unit='ROTATION', default=0.0,
        description="Angle in the XY plane from +X (0deg=+X, 90deg=+Y, CCW from above)",
        update=_on_param_update,
    )
    elevation: FloatProperty(
        name="Vertical", subtype='ANGLE', unit='ROTATION', default=0.0,
        description="Tilt up out of the XY plane (positive = upward in +Z)",
        update=_on_param_update,
    )
    show_preview: BoolProperty(
        name="Preview", default=True,
        description="Draw the guide line and labels in the viewport",
        update=_redraw_update,
    )
    live: BoolProperty(
        name="Live drag", default=False,
        description="Move in real time as you change the values. Object mode drags the "
                    "object; Vertices mode drags the selected verts (must be in Edit Mode)",
        update=_on_live_toggle,
    )
    anchor_object: StringProperty()
    anchor_vindex: IntProperty(default=-1)
    ref_object: StringProperty()
    ref_vindex: IntProperty(default=-1)
    ref_set: BoolProperty(default=False)
    # live baseline (rest state captured when live drag is switched on)
    base_anchor: FloatVectorProperty(size=3)
    base_mat: FloatVectorProperty(size=16)
    has_base: BoolProperty(default=False)


# ---------------------------------------------------------------- operators
class PM_OT_set_anchor(Operator):
    bl_idname = "object.pm_set_anchor"
    bl_label = "Set anchor"
    bl_description = "Remember the selected vertex as the handle that gets placed"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Enter Edit Mode and select a vertex")
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.index_update()
        v = _active_vert(bm)
        if v is None:
            self.report({'WARNING'}, "Select a vertex first")
            return {'CANCELLED'}
        s = context.scene.pm_settings
        s.anchor_object = obj.name
        s.anchor_vindex = v.index
        if _both_set(s):
            _fill_current(context)         # show real current gap, don't snap
        if s.live and _capture_base(context):
            _apply_live(context)
        _tag_redraw(context)
        self.report({'INFO'}, "Anchor: {} v{}".format(obj.name, v.index))
        return {'FINISHED'}


class PM_OT_set_reference(Operator):
    bl_idname = "object.pm_set_reference"
    bl_label = "Set reference"
    bl_description = "Remember the selected vertex as the point distance is measured from"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Enter Edit Mode and select a vertex")
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.index_update()
        v = _active_vert(bm)
        if v is None:
            self.report({'WARNING'}, "Select a vertex first")
            return {'CANCELLED'}
        s = context.scene.pm_settings
        s.ref_object = obj.name
        s.ref_vindex = v.index
        s.ref_set = True
        if _both_set(s):
            _fill_current(context)         # show real current gap, don't snap
        if s.live and _capture_base(context):
            _apply_live(context)
        _tag_redraw(context)
        self.report({'INFO'}, "Reference: {} v{}".format(obj.name, v.index))
        return {'FINISHED'}


class PM_OT_measure(Operator):
    bl_idname = "object.pm_measure"
    bl_label = "Use current distance"
    bl_description = ("Set distance and angles to the current gap between anchor and "
                      "reference (so you can edit from the real value)")

    def execute(self, context):
        if not _fill_current(context):
            self.report({'WARNING'}, "Set both anchor and reference first")
            return {'CANCELLED'}
        s = context.scene.pm_settings
        if s.live and _capture_base(context):
            _apply_live(context)
        _tag_redraw(context)
        return {'FINISHED'}


class PM_OT_place(Operator):
    bl_idname = "object.pm_place"
    bl_label = "Place"
    bl_description = ("Move so the anchor vertex sits at the set distance/angle from "
                      "the reference (or from its current spot if no reference)")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.pm_settings
        comp = _compute(context)
        if comp is None:
            self.report({'WARNING'}, "Set a valid anchor (and reference) first")
            return {'CANCELLED'}
        anchor_pos, from_pos, vec, target = comp
        delta = target - anchor_pos

        if s.move_mode == 'OBJECT':
            obj = bpy.data.objects.get(s.anchor_object)
            _move_object(obj, delta)
            context.view_layer.update()
            self.report({'INFO'}, "Moved {}".format(obj.name))
        else:
            obj = context.edit_object
            if obj is None or obj.type != 'MESH':
                self.report({'WARNING'}, "Enter Edit Mode to move vertices")
                return {'CANCELLED'}
            n = _move_selected_verts(obj, delta)
            if n == 0:
                self.report({'WARNING'}, "No vertices selected")
                return {'CANCELLED'}
            self.report({'INFO'}, "Moved {} vertex(es)".format(n))
        _tag_redraw(context)
        return {'FINISHED'}


class PM_OT_clear(Operator):
    bl_idname = "object.pm_clear"
    bl_label = "Clear"
    bl_description = "Forget the anchor and reference vertices and stop live drag"

    def execute(self, context):
        s = context.scene.pm_settings
        s.live = False
        s.anchor_object = ""
        s.anchor_vindex = -1
        s.ref_object = ""
        s.ref_vindex = -1
        s.ref_set = False
        s.has_base = False
        _tag_redraw(context)
        return {'FINISHED'}


# ---------------------------------------------------------------- panel
class PM_PT_panel(Panel):
    bl_label = "Polar Move"
    bl_idname = "PM_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Move"

    def draw(self, context):
        layout = self.layout
        s = context.scene.pm_settings

        layout.prop(s, "move_mode", expand=True)

        box = layout.box()
        box.operator("object.pm_set_anchor", icon='VERTEXSEL')
        if s.anchor_object and s.anchor_vindex >= 0:
            box.label(text="{}  v{}".format(s.anchor_object, s.anchor_vindex),
                      icon='OBJECT_DATA')
        else:
            box.label(text="not set", icon='ERROR')

        box = layout.box()
        box.operator("object.pm_set_reference", icon='VERTEXSEL')
        if s.ref_set and s.ref_object and s.ref_vindex >= 0:
            box.label(text="{}  v{}".format(s.ref_object, s.ref_vindex),
                      icon='OBJECT_DATA')
        else:
            box.label(text="not set - moves from current", icon='INFO')

        col = layout.column(align=True)
        col.prop(s, "distance")
        col.prop(s, "azimuth")
        col.prop(s, "elevation")

        m = _measure_current(context)
        if m is not None:
            box = layout.box()
            box.label(text="Current: {}".format(_format_length(context.scene, m[0])),
                      icon='ARROW_LEFTRIGHT')
            box.label(text="H {:.1f}°    V {:.1f}°".format(
                math.degrees(m[1]), math.degrees(m[2])))
            box.operator("object.pm_measure", icon='FILE_REFRESH')

        col = layout.column(align=True)
        col.prop(s, "show_preview", toggle=True,
                 icon='HIDE_OFF' if s.show_preview else 'HIDE_ON')
        col.prop(s, "live", toggle=True,
                 icon='PAUSE' if s.live else 'PLAY')

        if s.live:
            what = "object" if s.move_mode == 'OBJECT' else "selected verts"
            layout.label(text="Live: {} follow the values".format(what), icon='INFO')
        else:
            row = layout.row(align=True)
            row.scale_y = 1.3
            row.operator("object.pm_place", icon='CHECKMARK')
        layout.operator("object.pm_clear", icon='X')


# ---------------------------------------------------------------- registration
classes = (PM_Settings, PM_OT_set_anchor, PM_OT_set_reference, PM_OT_measure,
           PM_OT_place, PM_OT_clear, PM_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pm_settings = PointerProperty(type=PM_Settings)

    ns = bpy.app.driver_namespace
    old = ns.get(_NS_KEY)
    if old is not None:
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

    if hasattr(bpy.types.Scene, "pm_settings"):
        del bpy.types.Scene.pm_settings
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == "__main__":
    register()
