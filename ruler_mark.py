bl_info = {
    "name": "Ruler Mark",
    "author": "Vladimir (with Claude)",
    "version": (1, 4, 1),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar (N) > Ruler   (use with the Measure tool)",
    "description": "Rides on the native Measure tool: pick any ruler, edit its length/angle "
                   "from a panel (live), and drop a vertex or Empty at its end.",
    "category": "Object",
}

import bpy
import bmesh
import blf
import gpu
import math
from gpu_extras.batch import batch_for_shader
from bpy.props import FloatProperty, IntProperty, EnumProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras import view3d_utils
from mathutils import Vector

RULER_LAYER = "RulerData3D"     # annotation layer the Measure tool stores rulers in
MARKS_OBJ = "Marks"
_NS_KEY = "_rm_draw_handle"     # highlight draw handle, reload-safe in the driver namespace
_HI = (1.0, 0.5, 0.05, 1.0)     # highlight colour


# ------------------------------------------------------------------ ruler data
def _find_rulers(context):
    """Real ruler strokes from the RulerData3D layer, wherever it lives. In 5.x the
    Measure tool stores rulers in ``bpy.data.annotations`` (legacy-style layer with
    ``frames[].strokes[].points[].co``); older builds used ``scene.grease_pencil`` /
    ``bpy.data.grease_pencils``. Degenerate strokes (a point on itself, left by stray
    clicks) are skipped. Returns a list of strokes or None."""
    seen = []
    agp = getattr(context.scene, "grease_pencil", None)
    if agp is not None:
        seen.append(agp)
    for coll_name in ("annotations", "grease_pencils"):
        coll = getattr(bpy.data, coll_name, None)
        if coll is None:
            continue
        for g in coll:
            if g not in seen:
                seen.append(g)

    for gp in seen:
        layers = getattr(gp, "layers", None)
        if not layers:
            continue
        layer = layers.get(RULER_LAYER)
        if layer is None:
            continue
        frame = layer.active_frame or (layer.frames[0] if len(layer.frames) else None)
        if frame is None or not hasattr(frame, "strokes"):
            continue
        good = []
        for s in frame.strokes:
            if len(s.points) >= 2:
                a = Vector(s.points[0].co)
                b = Vector(s.points[-1].co)
                if (b - a).length > 1e-4:        # skip zero-length stray clicks
                    good.append(s)
        if good:
            return good
    return None


def _effective_index(context, strokes):
    """Resolve the stored index to a valid one; a stored value < 0 means 'the last
    ruler' (the default, so it keeps following freshly drawn rulers)."""
    n = len(strokes)
    i = context.scene.rm_settings.ruler_index
    if i < 0 or i >= n:
        return n - 1
    return i


def _selected_ruler(context):
    strokes = _find_rulers(context)
    if not strokes:
        return None
    return strokes[_effective_index(context, strokes)]


def _ruler_layer(context):
    """The grease-pencil datablock + RulerData3D layer holding the rulers, or (None, None)."""
    seen = []
    agp = getattr(context.scene, "grease_pencil", None)
    if agp is not None:
        seen.append(agp)
    for coll_name in ("annotations", "grease_pencils"):
        coll = getattr(bpy.data, coll_name, None)
        if coll is None:
            continue
        for g in coll:
            if g not in seen:
                seen.append(g)
    for gp in seen:
        layers = getattr(gp, "layers", None)
        if layers:
            layer = layers.get(RULER_LAYER)
            if layer is not None:
                return gp, layer
    return None, None


def _ends(stroke):
    """(start, end) world-space Vectors of a ruler stroke."""
    return Vector(stroke.points[0].co), Vector(stroke.points[-1].co)


def _polar(dist, az, el):
    ce = math.cos(el)
    return Vector((dist * ce * math.cos(az), dist * ce * math.sin(az), dist * math.sin(el)))


def _redraw():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ------------------------------------------------------------------ gizmo refresh
_refresh_pending = {"on": False}
_PICK = {"on": False}            # True while the "Pick in viewport" modal is armed


def _do_refresh():
    _refresh_pending["on"] = False
    try:
        win = bpy.context.window
        area = next((a for a in win.screen.areas if a.type == 'VIEW_3D'), None) if win else None
        if area is not None:
            with bpy.context.temp_override(window=win, area=area):
                bpy.ops.wm.tool_set_by_id(name='builtin.measure')   # reloads ruler gizmo from data
    except Exception as e:
        print("[Ruler Mark] viewport refresh:", e)
    return None


def _schedule_refresh():
    """Push edited ruler data back into the on-screen gizmo. The Measure gizmo caches
    its display and ignores tag_redraw, so we re-activate the tool on the next timer
    tick (deferred to avoid 'operator in invalid context'). Coalesced to once/tick."""
    if not _refresh_pending["on"]:
        _refresh_pending["on"] = True
        try:
            bpy.app.timers.register(_do_refresh, first_interval=0.0)
        except Exception:
            _refresh_pending["on"] = False


# ------------------------------------------------------------------ computed length/angle
# Read/write the SELECTED ruler's end relative to its start, so the panel mirrors the
# chosen ruler and editing a value moves that ruler's end.
def _get_length(self):
    st = _selected_ruler(bpy.context)
    if st is None:
        return 0.0
    a, b = _ends(st)
    return (b - a).length


def _set_length(self, value):
    st = _selected_ruler(bpy.context)
    if st is None:
        return
    a, b = _ends(st)
    d = b - a
    nb = a + (d.normalized() * value if d.length > 1e-9 else Vector((value, 0.0, 0.0)))
    st.points[-1].co = nb
    _redraw()
    _schedule_refresh()


def _get_az(self):
    st = _selected_ruler(bpy.context)
    if st is None:
        return 0.0
    a, b = _ends(st)
    d = b - a
    return math.atan2(d.y, d.x)


def _set_az(self, value):
    st = _selected_ruler(bpy.context)
    if st is None:
        return
    a, b = _ends(st)
    d = b - a
    st.points[-1].co = a + _polar(d.length, value, math.atan2(d.z, math.hypot(d.x, d.y)))
    _redraw()
    _schedule_refresh()


def _get_el(self):
    st = _selected_ruler(bpy.context)
    if st is None:
        return 0.0
    a, b = _ends(st)
    d = b - a
    return math.atan2(d.z, math.hypot(d.x, d.y))


def _set_el(self, value):
    st = _selected_ruler(bpy.context)
    if st is None:
        return
    a, b = _ends(st)
    d = b - a
    st.points[-1].co = a + _polar(d.length, math.atan2(d.y, d.x), value)
    _redraw()
    _schedule_refresh()


# ------------------------------------------------------------------ marks
def _add_vert(context, p):
    obj = bpy.data.objects.get(MARKS_OBJ)
    if obj is None or obj.type != 'MESH':
        me = bpy.data.meshes.new(MARKS_OBJ)
        obj = bpy.data.objects.new(MARKS_OBJ, me)
        (context.collection or context.scene.collection).objects.link(obj)
    me = obj.data
    inv = obj.matrix_world.inverted()
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.new(inv @ p)
    bm.to_mesh(me)
    bm.free()
    me.update()


def _add_empty(context, p, size):
    e = bpy.data.objects.new("Mark", None)
    e.empty_display_type = 'PLAIN_AXES'
    e.empty_display_size = size
    e.location = p
    (context.collection or context.scene.collection).objects.link(e)


# ------------------------------------------------------------------ picking
def _view3d_region(context, event):
    for area in context.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        for region in area.regions:
            if region.type == 'WINDOW' and \
                    region.x <= event.mouse_x <= region.x + region.width and \
                    region.y <= event.mouse_y <= region.y + region.height:
                return region, area.spaces.active.region_3d
    return None, None


def _seg_dist_2d(p, a, b):
    ab = b - a
    l2 = ab.length_squared
    t = 0.0 if l2 < 1e-12 else max(0.0, min(1.0, (p - a).dot(ab) / l2))
    return (p - (a + ab * t)).length


def _nearest_ruler_index(context, event):
    strokes = _find_rulers(context)
    if not strokes:
        return None
    region, rv3d = _view3d_region(context, event)
    if region is None:
        return None
    mouse = Vector((event.mouse_x - region.x, event.mouse_y - region.y))
    best = None
    for i, s in enumerate(strokes):
        a3, b3 = _ends(s)
        a2 = view3d_utils.location_3d_to_region_2d(region, rv3d, a3)
        b2 = view3d_utils.location_3d_to_region_2d(region, rv3d, b3)
        if a2 is None or b2 is None:
            continue
        d = _seg_dist_2d(mouse, a2, b2)
        if best is None or d < best[0]:
            best = (d, i)
    if best is None or best[0] > 40.0:           # must click within ~40 px of a line
        return None
    return best[1]


# ------------------------------------------------------------------ highlight overlay
def _draw_highlight():
    ctx = bpy.context
    try:
        _ = ctx.scene.rm_settings
    except AttributeError:
        return
    region = ctx.region
    if _PICK["on"] and region is not None:                 # armed-state hint in the viewport
        font = 0
        ui = ctx.preferences.system.ui_scale
        blf.size(font, 13.0 * ui)
        blf.enable(font, blf.SHADOW)
        blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.9)
        blf.shadow_offset(font, 1, -1)
        blf.color(font, *_HI)
        blf.position(font, 18.0 * ui, 18.0 * ui, 0.0)
        blf.draw(font, "Pick: click a ruler line to select  ·  Esc to cancel")
        blf.disable(font, blf.SHADOW)

    st = _selected_ruler(ctx)
    if st is None:
        return
    rv3d = ctx.region_data
    if region is None or rv3d is None:
        return
    a3, b3 = _ends(st)
    a2 = view3d_utils.location_3d_to_region_2d(region, rv3d, a3)
    b2 = view3d_utils.location_3d_to_region_2d(region, rv3d, b3)
    dots = [(p.x, p.y, 0.0) for p in (a2, b2) if p is not None]
    if not dots:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    if a2 is not None and b2 is not None:
        gpu.state.line_width_set(2.0)
        lb = batch_for_shader(shader, 'LINES', {"pos": [(a2.x, a2.y, 0.0), (b2.x, b2.y, 0.0)]})
        shader.bind(); shader.uniform_float("color", (_HI[0], _HI[1], _HI[2], 0.5)); lb.draw(shader)
    gpu.state.point_size_set(12.0)
    pb = batch_for_shader(shader, 'POINTS', {"pos": dots})
    shader.bind(); shader.uniform_float("color", _HI); pb.draw(shader)
    gpu.state.point_size_set(1.0)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')


# ------------------------------------------------------------------ data
class RM_Settings(PropertyGroup):
    ruler_index: IntProperty(default=-1)        # < 0 => follow the last ruler
    length: FloatProperty(name="Length", subtype='DISTANCE', unit='LENGTH',
                          get=_get_length, set=_set_length,
                          description="Length of the selected ruler (edit to move its end)")
    azimuth: FloatProperty(name="Horizontal", subtype='ANGLE', unit='ROTATION',
                           get=_get_az, set=_set_az,
                           description="Angle of the ruler in the XY plane from +X")
    elevation: FloatProperty(name="Vertical", subtype='ANGLE', unit='ROTATION',
                             get=_get_el, set=_set_el,
                             description="Tilt of the ruler up out of the XY plane")
    mark_at: EnumProperty(
        name="At", default='END',
        items=[('END', "End", "Only the ruler's end point"),
               ('BOTH', "Both ends", "Both the start and end points")])
    empty_size: FloatProperty(name="Empty size", subtype='DISTANCE', unit='LENGTH',
                              default=0.1, min=1e-5)


# ------------------------------------------------------------------ operators
class RM_OT_vertex(Operator):
    bl_idname = "object.rm_vertex_at_ruler"
    bl_label = "Vertex at ruler"
    bl_description = "Add a vertex (to a 'Marks' mesh) at the selected ruler's end"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        st = _selected_ruler(context)
        if st is None:
            self.report({'WARNING'}, "Draw a ruler with the Measure tool first")
            return {'CANCELLED'}
        a, b = _ends(st)
        _add_vert(context, b)
        if context.scene.rm_settings.mark_at == 'BOTH':
            _add_vert(context, a)
        _redraw()
        return {'FINISHED'}


class RM_OT_empty(Operator):
    bl_idname = "object.rm_empty_at_ruler"
    bl_label = "Empty at ruler"
    bl_description = "Drop an Empty at the selected ruler's end"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        st = _selected_ruler(context)
        if st is None:
            self.report({'WARNING'}, "Draw a ruler with the Measure tool first")
            return {'CANCELLED'}
        a, b = _ends(st)
        s = context.scene.rm_settings
        _add_empty(context, b, s.empty_size)
        if s.mark_at == 'BOTH':
            _add_empty(context, a, s.empty_size)
        _redraw()
        return {'FINISHED'}


class RM_OT_refresh(Operator):
    bl_idname = "object.rm_refresh_ruler"
    bl_label = "Update viewport"
    bl_description = ("Push the edited ruler back to the on-screen Measure gizmo "
                      "(re-activates the Measure tool so it reloads the data)")

    def execute(self, context):
        try:
            bpy.ops.wm.tool_set_by_id(name='builtin.measure')
        except Exception as e:
            self.report({'WARNING'}, "Couldn't refresh: {}".format(e))
            return {'CANCELLED'}
        _redraw()
        return {'FINISHED'}


class RM_OT_step(Operator):
    bl_idname = "object.rm_step_ruler"
    bl_label = "Step ruler"
    bl_description = "Select the previous / next ruler"
    delta: IntProperty(default=1)

    def execute(self, context):
        strokes = _find_rulers(context)
        if not strokes:
            return {'CANCELLED'}
        n = len(strokes)
        cur = _effective_index(context, strokes)
        context.scene.rm_settings.ruler_index = (cur + self.delta) % n
        _redraw()
        return {'FINISHED'}


class RM_OT_last(Operator):
    bl_idname = "object.rm_last_ruler"
    bl_label = "Follow last"
    bl_description = "Select the most recently drawn ruler (and keep following new ones)"

    def execute(self, context):
        context.scene.rm_settings.ruler_index = -1
        _redraw()
        return {'FINISHED'}


class RM_OT_pick(Operator):
    bl_idname = "object.rm_pick_ruler"
    bl_label = "Pick ruler"
    bl_description = "Click a ruler line in the viewport to select it for editing"

    def invoke(self, context, event):
        if _find_rulers(context) is None:
            self.report({'WARNING'}, "Draw a ruler first")
            return {'CANCELLED'}
        context.window_manager.modal_handler_add(self)
        _PICK["on"] = True
        _redraw()
        self.report({'INFO'}, "Click a ruler line  (Esc to cancel)")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                          'TRACKPADPAN', 'TRACKPADZOOM', 'NDOF_MOTION'}:
            return {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            idx = _nearest_ruler_index(context, event)
            if idx is not None:
                context.scene.rm_settings.ruler_index = idx
                self.report({'INFO'}, "Ruler {} selected".format(idx + 1))
                _PICK["on"] = False
                _redraw()
                return {'FINISHED'}
            self.report({'WARNING'}, "No ruler there - click closer to a line")
            return {'RUNNING_MODAL'}        # a miss keeps pick mode armed
        if event.value == 'PRESS' and event.type in {'RIGHTMOUSE', 'ESC'}:
            _PICK["on"] = False
            _redraw()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


class RM_OT_convert(Operator):
    bl_idname = "object.rm_convert_ruler"
    bl_label = "Ruler to mesh line"
    bl_description = ("Turn the selected ruler into a real mesh line (edges through all its "
                      "points - 2 for a straight ruler, 3 for a protractor) and remove the ruler")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        st = _selected_ruler(context)
        if st is None:
            self.report({'WARNING'}, "No ruler selected")
            return {'CANCELLED'}
        pts = [tuple(Vector(p.co)) for p in st.points]
        if len(pts) < 2:
            return {'CANCELLED'}
        me = bpy.data.meshes.new("RulerLine")
        me.from_pydata(pts, [(i, i + 1) for i in range(len(pts) - 1)], [])
        me.update()
        obj = bpy.data.objects.new("RulerLine", me)
        (context.collection or context.scene.collection).objects.link(obj)

        # single annotation strokes can't be deleted (the strokes collection has no
        # .remove in 5.x), so collapse the source ruler to a point: it drops out of
        # the addon's degenerate filter and the gizmo stops drawing it.
        try:
            base = st.points[0].co.copy()
            for p in st.points:
                p.co = base
        except Exception as e:
            print("[Ruler Mark] collapse:", e)
        context.scene.rm_settings.ruler_index = -1
        _redraw()
        _schedule_refresh()
        self.report({'INFO'}, "Ruler converted to mesh line '{}'".format(obj.name))
        return {'FINISHED'}


class RM_OT_clear(Operator):
    bl_idname = "object.rm_clear_rulers"
    bl_label = "Clear all rulers"
    bl_description = "Remove every ruler from the RulerData3D layer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        gp, layer = _ruler_layer(context)
        if layer is None:
            self.report({'INFO'}, "No rulers to clear")
            return {'CANCELLED'}
        try:
            gp.layers.remove(layer)              # drop the whole RulerData3D layer
        except Exception as e:
            self.report({'WARNING'}, "Couldn't clear: {}".format(e))
            return {'CANCELLED'}
        context.scene.rm_settings.ruler_index = -1
        _redraw()
        _schedule_refresh()
        self.report({'INFO'}, "All rulers cleared")
        return {'FINISHED'}


# ------------------------------------------------------------------ panel
class RM_PT_panel(Panel):
    bl_label = "Ruler Mark"
    bl_idname = "RM_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Ruler"

    def draw(self, context):
        layout = self.layout
        s = context.scene.rm_settings
        strokes = _find_rulers(context)

        if not strokes:
            layout.label(text="Draw a ruler with Measure", icon='INFO')
            layout.label(text="(keep the Measure tool active)")
            return

        total = len(strokes)
        idx = _effective_index(context, strokes)
        following = s.ruler_index < 0

        row = layout.row(align=True)
        row.operator("object.rm_step_ruler", text="", icon='TRIA_LEFT').delta = -1
        row.label(text="Ruler {} / {}{}".format(idx + 1, total, "  (last)" if following else ""))
        row.operator("object.rm_step_ruler", text="", icon='TRIA_RIGHT').delta = 1
        row = layout.row(align=True)
        picking = _PICK["on"]
        row.operator("object.rm_pick_ruler",
                     text="Click a ruler…" if picking else "Pick in viewport",
                     icon='EYEDROPPER', depress=picking)
        row.operator("object.rm_last_ruler", text="Last", icon='TRACKING_BACKWARDS')

        col = layout.column(align=True)
        col.prop(s, "length")
        col.prop(s, "azimuth")
        col.prop(s, "elevation")
        layout.operator("object.rm_refresh_ruler", text="Update viewport", icon='FILE_REFRESH')

        layout.separator()
        layout.label(text="Place mark at selected ruler:")
        layout.prop(s, "mark_at", expand=True)
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("object.rm_vertex_at_ruler", text="Vertex", icon='VERTEXSEL')
        row.operator("object.rm_empty_at_ruler", text="Empty", icon='EMPTY_AXIS')
        layout.prop(s, "empty_size")

        layout.separator()
        layout.operator("object.rm_convert_ruler", text="Ruler → mesh line",
                        icon='OUTLINER_OB_MESH')
        layout.operator("object.rm_clear_rulers", text="Clear all rulers", icon='TRASH')


# ------------------------------------------------------------------ registration
classes = (RM_Settings, RM_OT_vertex, RM_OT_empty, RM_OT_refresh,
           RM_OT_step, RM_OT_last, RM_OT_pick, RM_OT_convert, RM_OT_clear, RM_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.rm_settings = PointerProperty(type=RM_Settings)

    ns = bpy.app.driver_namespace
    old = ns.get(_NS_KEY)
    if old is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(old, 'WINDOW')
        except Exception:
            pass
    ns[_NS_KEY] = bpy.types.SpaceView3D.draw_handler_add(
        _draw_highlight, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    ns = bpy.app.driver_namespace
    h = ns.get(_NS_KEY)
    if h is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        except Exception:
            pass
        ns[_NS_KEY] = None

    if hasattr(bpy.types.Scene, "rm_settings"):
        del bpy.types.Scene.rm_settings
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == "__main__":
    register()
