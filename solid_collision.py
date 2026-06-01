bl_info = {
    "name": "Solid Collision",
    "author": "Vladimir (with Claude)",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Solid",
    "description": "Mark objects as solid; moving one pushes the others away so they can't overlap.",
    "category": "Object",
}

import bpy
import itertools
from mathutils import Vector
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty, PointerProperty
from bpy.types import PropertyGroup, Panel, Operator

# per-object flag marking it as a solid that participates in collision
_SOLID_KEY = "scol_is_solid"

# tiny extra separation so resolved objects rest just clear of each other
# instead of exactly touching, which would jitter-retrigger the overlap test.
_EPS = 1e-4


# ---------------------------------------------------------------- geometry
def _world_aabb(obj, pad=0.0):
    """World axis-aligned bounding box as a mutable [minx,miny,minz,maxx,maxy,maxz].
    Padded outward by `pad` on every side."""
    mw = obj.matrix_world
    cs = [mw @ Vector(c) for c in obj.bound_box]
    xs = [c.x for c in cs]; ys = [c.y for c in cs]; zs = [c.z for c in cs]
    return [min(xs) - pad, min(ys) - pad, min(zs) - pad,
            max(xs) + pad, max(ys) + pad, max(zs) + pad]


def _shift_box(box, v):
    box[0] += v.x; box[1] += v.y; box[2] += v.z
    box[3] += v.x; box[4] += v.y; box[5] += v.z


def _overlap_mtv(a, b):
    """Minimum translation vector to ADD to box `b` to separate it from box `a`.
    None when the two boxes do not overlap.

    Pushes along the axis of least penetration (classic AABB resolution), in the
    direction that moves b's center away from a's center."""
    px = min(a[3], b[3]) - max(a[0], b[0])
    py = min(a[4], b[4]) - max(a[1], b[1])
    pz = min(a[5], b[5]) - max(a[2], b[2])
    if px <= 0.0 or py <= 0.0 or pz <= 0.0:
        return None
    cax = a[0] + a[3]; cbx = b[0] + b[3]
    cay = a[1] + a[4]; cby = b[1] + b[4]
    caz = a[2] + a[5]; cbz = b[2] + b[5]
    if px <= py and px <= pz:
        return Vector(((px + _EPS) * (1.0 if cbx >= cax else -1.0), 0.0, 0.0))
    if py <= pz:
        return Vector((0.0, (py + _EPS) * (1.0 if cby >= cay else -1.0), 0.0))
    return Vector((0.0, 0.0, (pz + _EPS) * (1.0 if cbz >= caz else -1.0)))


# ---------------------------------------------------------------- selection
def _is_solid(obj):
    return obj is not None and obj.type == 'MESH' and obj.get(_SOLID_KEY)


def _solids(context):
    return [o for o in context.scene.objects if _is_solid(o)]


def _split(x, y, mover):
    """Return (fx, fy): the fraction of the separation each of x, y should absorb.
    The moved object stays put; pushes propagate outward to the farther neighbor."""
    if mover is x:
        return 0.0, 1.0                      # x is being dragged -> y moves fully
    if mover is y:
        return 1.0, 0.0
    if mover is not None:                     # neither is the mover: shove the farther one
        cm = mover.matrix_world.translation
        dx = (x.matrix_world.translation - cm).length
        dy = (y.matrix_world.translation - cm).length
        return (0.0, 1.0) if dy >= dx else (1.0, 0.0)
    return 0.5, 0.5                           # no mover (Separate now): split evenly


# ---------------------------------------------------------------- solver
def resolve(context, mover):
    """Push solid objects apart until none overlap (or iteration budget runs out).

    `mover` (if solid) is pinned: it keeps the position the user dragged it to and
    only displaces the objects it runs into. We translate via .location and track
    each box by shifting its cached AABB, so we never read a stale matrix_world
    mid-pass — this assumes the solids are unparented top-level objects, where
    local location equals world translation."""
    s = context.scene.scol_settings
    solids = _solids(context)
    if len(solids) < 2:
        return 0
    half_gap = max(s.gap, 0.0) * 0.5
    boxes = {o: _world_aabb(o, half_gap) for o in solids}

    moves = 0
    for _ in range(max(s.iterations, 1)):
        changed = False
        for x, y in itertools.combinations(solids, 2):
            mtv = _overlap_mtv(boxes[x], boxes[y])
            if mtv is None:
                continue
            fx, fy = _split(x, y, mover)
            if fx:
                vx = -mtv * fx
                x.location += vx
                _shift_box(boxes[x], vx)
            if fy:
                vy = mtv * fy
                y.location += vy
                _shift_box(boxes[y], vy)
            changed = True
            moves += 1
        if not changed:
            break
    return moves


# ---------------------------------------------------------------- live handler
def _live_handler(scene, depsgraph=None):
    # re-entrancy guard: our own .location writes fire depsgraph_update_post again.
    if getattr(_live_handler, "_busy", False):
        return
    try:
        s = scene.scol_settings
    except AttributeError:
        return
    if not s.enabled:
        return
    mover = bpy.context.view_layer.objects.active
    if not _is_solid(mover):                  # only react while a solid is being moved
        return
    _live_handler._busy = True
    try:
        resolve(bpy.context, mover)
    except Exception as e:
        print("[Solid Collision]", e)
    finally:
        _live_handler._busy = False


# ---------------------------------------------------------------- data
class SCOL_Settings(PropertyGroup):
    enabled: BoolProperty(
        name="Live collision",
        description="While on, moving a solid object pushes the others out of its way",
        default=False,
    )
    gap: FloatProperty(
        name="Clearance", subtype='DISTANCE', min=0.0,
        default=0.0, soft_max=1.0,
        description="Empty space to keep between solids after they are pushed apart",
    )
    iterations: IntProperty(
        name="Iterations", min=1, max=50, default=8,
        description="How many resolve passes per move (higher untangles deeper pile-ups)",
    )


# ---------------------------------------------------------------- operators
class SCOL_OT_mark(Operator):
    bl_idname = "object.scol_mark"
    bl_label = "Mark selected solid"
    bl_description = "Add the selected mesh objects to the solid set"

    def execute(self, context):
        n = 0
        for o in context.selected_objects:
            if o.type == 'MESH':
                o[_SOLID_KEY] = True
                n += 1
        if not n:
            self.report({'WARNING'}, "Select mesh objects first")
            return {'CANCELLED'}
        self.report({'INFO'}, "Marked {} solid".format(n))
        return {'FINISHED'}


class SCOL_OT_unmark(Operator):
    bl_idname = "object.scol_unmark"
    bl_label = "Unmark selected"
    bl_description = "Remove the selected mesh objects from the solid set"

    def execute(self, context):
        n = 0
        for o in context.selected_objects:
            if o.get(_SOLID_KEY):
                del o[_SOLID_KEY]
                n += 1
        self.report({'INFO'}, "Unmarked {}".format(n))
        return {'FINISHED'}


class SCOL_OT_unmark_one(Operator):
    bl_idname = "object.scol_unmark_one"
    bl_label = "Remove from solids"
    bl_description = "Remove this object from the solid set"
    name: StringProperty()

    def execute(self, context):
        o = bpy.data.objects.get(self.name)
        if o is not None and o.get(_SOLID_KEY):
            del o[_SOLID_KEY]
        return {'FINISHED'}


class SCOL_OT_clear(Operator):
    bl_idname = "object.scol_clear"
    bl_label = "Clear all"
    bl_description = "Empty the solid set and turn live collision off"

    def execute(self, context):
        for o in _solids(context):
            del o[_SOLID_KEY]
        context.scene.scol_settings.enabled = False
        return {'FINISHED'}


class SCOL_OT_separate(Operator):
    bl_idname = "object.scol_separate"
    bl_label = "Separate now"
    bl_description = "Push all currently overlapping solids apart once"

    def execute(self, context):
        moves = resolve(context, None)
        self.report({'INFO'}, "Resolved {} overlap(s)".format(moves))
        return {'FINISHED'}


# ---------------------------------------------------------------- panel
class SCOL_PT_panel(Panel):
    bl_label = "Solid Collision"
    bl_idname = "SCOL_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Solid"

    def draw(self, context):
        layout = self.layout
        s = context.scene.scol_settings

        layout.prop(s, "enabled", toggle=True,
                    icon='PHYSICS' if s.enabled else 'MESH_CUBE')

        row = layout.row(align=True)
        row.operator("object.scol_mark", icon='ADD')
        row.operator("object.scol_unmark", icon='REMOVE')

        col = layout.column(align=True)
        col.prop(s, "gap")
        col.prop(s, "iterations")

        row = layout.row(align=True)
        row.operator("object.scol_separate", icon='FULLSCREEN_EXIT')
        row.operator("object.scol_clear", icon='X')

        solids = _solids(context)
        box = layout.box()
        box.label(text="Solid objects: {}".format(len(solids)), icon='SNAP_VOLUME')
        col = box.column(align=True)
        for o in solids:
            r = col.row(align=True)
            r.label(text=o.name, icon='MESH_CUBE')
            op = r.operator("object.scol_unmark_one", text="", icon='X')
            op.name = o.name


# ---------------------------------------------------------------- registration
classes = (SCOL_Settings, SCOL_OT_mark, SCOL_OT_unmark, SCOL_OT_unmark_one,
           SCOL_OT_clear, SCOL_OT_separate, SCOL_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.scol_settings = PointerProperty(type=SCOL_Settings)
    for h in list(bpy.app.handlers.depsgraph_update_post):
        if getattr(h, "__name__", "") == "_live_handler":
            bpy.app.handlers.depsgraph_update_post.remove(h)
    bpy.app.handlers.depsgraph_update_post.append(_live_handler)


def unregister():
    for h in list(bpy.app.handlers.depsgraph_update_post):
        if getattr(h, "__name__", "") == "_live_handler":
            bpy.app.handlers.depsgraph_update_post.remove(h)
    if hasattr(bpy.types.Scene, "scol_settings"):
        del bpy.types.Scene.scol_settings
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == "__main__":
    register()
