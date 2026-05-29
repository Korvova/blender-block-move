bl_info = {
    "name": "Intersection Highlighter",
    "author": "Vladimir (with Claude)",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Intersect",
    "description": "Highlight mesh objects that intersect each other, live or on demand.",
    "category": "Object",
}

import bpy
import mathutils
import itertools
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, PointerProperty)
from bpy.types import PropertyGroup, Panel, Operator
from mathutils.bvhtree import BVHTree

# per-object custom prop holding the original (r,g,b,a) before we tinted it.
# Stored on the object (not a module global) so it survives module reloads and
# .blend save/reopen — otherwise a highlight color can get stuck with no record
# of what to restore to.
_ORIG_KEY = "_ih_orig_color"

# no-intersection lock: object name -> last non-penetrating location (Vector)
_LOCK_GOOD = {}


# ---------------------------------------------------------------- geometry
def _eval_geom(obj, depsgraph):
    """World-space (triangulated BVH, sample points) for precise classification.

    Sample points are vertices plus triangle centroids. Centroids matter
    because two interpenetrating boxes have every vertex lying on the other's
    boundary — only interior face points land inside to reveal the overlap.
    Triangle centroids (not raw polygon centers) keep every sample on the real
    surface: a concave or holed ngon — e.g. the keyhole top face of a box with
    a recess — has its centroid floating in empty space over the opening, which
    reads as a phantom collision."""
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    me.calc_loop_triangles()
    mw = obj.matrix_world
    verts = [mw @ v.co for v in me.vertices]
    tris = [t.vertices[:] for t in me.loop_triangles]
    bvh = BVHTree.FromPolygons(verts, tris)
    pts = list(verts)
    pts.extend((verts[a] + verts[b] + verts[c]) / 3.0 for a, b, c in tris)
    ev.to_mesh_clear()
    return bvh, pts


def _signed_min(bvh, pts):
    """Smallest signed distance from pts to the surface in bvh.
    Negative means the point sits behind the surface normal (inside)."""
    best = None
    for p in pts:
        loc, nor, idx, dist = bvh.find_nearest(p)
        if loc is None:
            continue
        sd = dist if (p - loc).dot(nor) >= 0.0 else -dist
        if best is None or sd < best:
            best = sd
    return best


def _classify_precise(geom_a, geom_b, thr):
    """0 = apart, 1 = touching (within thr), 2 = interpenetrating (deeper than thr)."""
    bvh_a, pts_a = geom_a
    bvh_b, pts_b = geom_b
    cands = [m for m in (_signed_min(bvh_b, pts_a),
                         _signed_min(bvh_a, pts_b)) if m is not None]
    if not cands:
        return 0
    mm = min(cands)
    if mm < -thr:
        return 2
    if mm <= thr:
        return 1
    return 0


def _point_inside(bvh, p):
    """Even-odd ray test: cast +Z and count surface crossings (robust for concave)."""
    direction = mathutils.Vector((0.0, 0.0, 1.0))
    origin = p.copy()
    crossings = 0
    while True:
        loc, nor, idx, dist = bvh.ray_cast(origin, direction)
        if loc is None:
            break
        crossings += 1
        origin = loc + direction * 1e-5
    return crossings % 2 == 1


def _penetrates(geom_a, geom_b):
    """True if the two solids really interpenetrate.

    Triangle overlap catches any surface crossing; the point-in-mesh fallback
    catches full containment with no crossing. Unlike a nearest-face-normal
    sign test this stays correct for concave shapes (holes, recesses), where
    a point in the cavity finds its nearest face on the rim pointing the wrong
    way and gets misread as 'inside the solid'."""
    bvh_a, pts_a = geom_a
    bvh_b, pts_b = geom_b
    if bvh_a.overlap(bvh_b):
        return True
    if pts_a and _point_inside(bvh_b, pts_a[0]):
        return True
    if pts_b and _point_inside(bvh_a, pts_b[0]):
        return True
    return False


def _world_aabb(obj):
    mw = obj.matrix_world
    cs = [mw @ mathutils.Vector(c) for c in obj.bound_box]
    xs = [c.x for c in cs]; ys = [c.y for c in cs]; zs = [c.z for c in cs]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _aabb_hit(a, b):
    return (a[0] <= b[3] and a[3] >= b[0] and
            a[1] <= b[4] and a[4] >= b[1] and
            a[2] <= b[5] and a[5] >= b[2])


def _aabb_pad(a, p):
    return (a[0] - p, a[1] - p, a[2] - p, a[3] + p, a[4] + p, a[5] + p)


def _classify_bbox(a, b, thr):
    """Approximate 3-state test: shrink A by thr for overlap, grow it for touch."""
    if _aabb_hit(_aabb_pad(a, -thr), b):
        return 2
    if _aabb_hit(_aabb_pad(a, thr), b):
        return 1
    return 0


# ---------------------------------------------------------------- pairing
def _mesh_objs(seq):
    return [o for o in seq if o and o.type == 'MESH']


def _selected_mesh(context):
    # select_get() reads the persistent flag and is reliable inside handlers,
    # unlike context.selected_objects which can come back empty there.
    vl = context.view_layer
    objs = vl.objects if vl else bpy.data.objects
    return [o for o in objs if o.type == 'MESH' and o.select_get()]


def _pairs(context):
    s = context.scene.ih_settings
    if s.scope == 'ALL':
        objs = _mesh_objs(context.scene.objects)
        return list(itertools.combinations(objs, 2))
    if s.scope == 'ACTIVE_OTHERS':
        vl = context.view_layer
        act = vl.objects.active if vl else None
        if not act or act.type != 'MESH':
            return []
        others = [o for o in _selected_mesh(context) if o != act]
        return [(act, o) for o in others]
    objs = _selected_mesh(context)                       # SELECTED (pairwise)
    return list(itertools.combinations(objs, 2))


# ---------------------------------------------------------------- coloring
def _restore(obj):
    orig = obj.get(_ORIG_KEY)
    if orig is not None:
        orig = tuple(orig)
        if tuple(obj.color) != orig:
            obj.color = orig
        del obj[_ORIG_KEY]


def _paint(obj, col):
    col = tuple(col)
    if _ORIG_KEY not in obj:
        obj[_ORIG_KEY] = tuple(obj.color)
    if tuple(obj.color) != col:
        obj.color = col


def restore_all():
    for o in bpy.data.objects:
        if _ORIG_KEY in o:
            o.color = tuple(o[_ORIG_KEY])
            del o[_ORIG_KEY]


def run_check(context):
    s = context.scene.ih_settings
    dg = context.evaluated_depsgraph_get()
    pairs = _pairs(context)
    thr = s.touch_threshold

    involved = set()
    for a, b in pairs:
        involved.add(a); involved.add(b)

    # per object: 0 apart, 1 touching, 2 intersecting — keep the strongest
    state = {o: 0 for o in involved}
    if pairs:
        if s.method == 'PRECISE':
            cache = {o: _eval_geom(o, dg) for o in involved}
            for a, b in pairs:
                st = _classify_precise(cache[a], cache[b], thr)
                if st:
                    state[a] = max(state[a], st)
                    state[b] = max(state[b], st)
        else:
            cache = {o: _world_aabb(o) for o in involved}
            for a, b in pairs:
                st = _classify_bbox(cache[a], cache[b], thr)
                if st:
                    state[a] = max(state[a], st)
                    state[b] = max(state[b], st)

    # restore anything we colored that is no longer part of the test set
    for o in bpy.data.objects:
        if _ORIG_KEY in o and o not in involved:
            _restore(o)

    hit_col = tuple(s.hit_color)
    touch_col = tuple(s.touch_color)
    n_hit = n_touch = 0
    for o in involved:
        st = state[o]
        if st == 2:
            _paint(o, hit_col); n_hit += 1
        elif st == 1:
            _paint(o, touch_col); n_touch += 1
        else:
            _restore(o)
    return n_hit, n_touch


# ---------------------------------------------------------------- viewport
def set_object_color_shading(context):
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                sp = area.spaces.active
                if sp.shading.type == 'SOLID':
                    sp.shading.color_type = 'OBJECT'


# ---------------------------------------------------------------- block constraint
def enforce_block(context):
    """Stop the active object from penetrating its locked partner.

    Runs from the depsgraph handler, i.e. just after Blender applied a move,
    so the best we can do is revert the moving object to the last position
    where the pair was not interpenetrating — a snap-back 'wall'."""
    s = context.scene.ih_settings
    a, b = s.lock_a, s.lock_b
    if not a or not b or a is b or a.type != 'MESH' or b.type != 'MESH':
        return
    dg = context.evaluated_depsgraph_get()
    ga = _eval_geom(a, dg)
    gb = _eval_geom(b, dg)
    # touching is allowed; only real solid interpenetration is a violation
    if _penetrates(ga, gb):
        mover = context.view_layer.objects.active
        if mover not in (a, b):
            da = (a.location - _LOCK_GOOD.get(a.name, a.location)).length
            db = (b.location - _LOCK_GOOD.get(b.name, b.location)).length
            mover = a if da >= db else b
        good = _LOCK_GOOD.get(mover.name)
        if good is not None:
            mover.location = good
    else:
        _LOCK_GOOD[a.name] = a.location.copy()
        _LOCK_GOOD[b.name] = b.location.copy()


# ---------------------------------------------------------------- live handler
def _live_handler(scene, depsgraph=None):
    if getattr(_live_handler, "_busy", False):
        return
    try:
        s = scene.ih_settings
    except AttributeError:
        return
    if not (s.live or s.block_intersect):
        return
    _live_handler._busy = True
    try:
        if s.block_intersect:
            enforce_block(bpy.context)
        if s.live:
            run_check(bpy.context)
    except Exception as e:
        print("[Intersection Highlighter]", e)
    finally:
        _live_handler._busy = False


def _on_live_toggle(self, context):
    if self.live:
        if self.auto_object_color:
            set_object_color_shading(context)
        run_check(context)
    else:
        restore_all()


def _on_setting_update(self, context):
    # re-check while live so the slider/scope/method respond without a move
    if self.live:
        run_check(context)


def _on_block_toggle(self, context):
    # snapshot a clean baseline to snap back to while the lock is active
    _LOCK_GOOD.clear()
    if self.block_intersect:
        for o in (self.lock_a, self.lock_b):
            if o is not None:
                _LOCK_GOOD[o.name] = o.location.copy()


# ---------------------------------------------------------------- data
class IH_Settings(PropertyGroup):
    live: BoolProperty(
        name="Live highlight",
        description="Continuously re-check while you move objects",
        default=False,
        update=_on_live_toggle,
    )
    scope: EnumProperty(
        name="Check",
        items=[
            ('SELECTED', "Selected (pairwise)", "Check all selected objects against each other"),
            ('ACTIVE_OTHERS', "Active vs others", "Check the active object against the other selected ones"),
            ('ALL', "All in scene", "Check every mesh object against every other (heavier)"),
        ],
        default='SELECTED',
        update=_on_setting_update,
    )
    method: EnumProperty(
        name="Method",
        items=[
            ('PRECISE', "Precise (mesh)", "Exact triangle-level intersection via BVH"),
            ('BBOX', "Fast (bounds)", "Axis-aligned bounding-box overlap; fast but approximate"),
        ],
        default='PRECISE',
        update=_on_setting_update,
    )
    hit_color: FloatVectorProperty(
        name="Hit color", subtype='COLOR', size=4,
        min=0.0, max=1.0, default=(1.0, 0.05, 0.05, 1.0),
        update=_on_setting_update,
    )
    touch_color: FloatVectorProperty(
        name="Touch color", subtype='COLOR', size=4,
        min=0.0, max=1.0, default=(0.10, 0.30, 0.90, 1.0),
        update=_on_setting_update,
    )
    touch_threshold: FloatProperty(
        name="Touch gap", subtype='DISTANCE', min=0.0,
        default=0.05, soft_max=1.0,
        description="Max surface gap (or shallow overlap) that counts as touching",
        update=_on_setting_update,
    )
    auto_object_color: BoolProperty(
        name="Auto viewport color",
        description="Switch Solid-mode viewports to Object color so the highlight is visible",
        default=True,
    )
    lock_a: PointerProperty(
        type=bpy.types.Object, name="Object 1",
        poll=lambda self, o: o.type == 'MESH',
    )
    lock_b: PointerProperty(
        type=bpy.types.Object, name="Object 2",
        poll=lambda self, o: o.type == 'MESH',
    )
    block_intersect: BoolProperty(
        name="Block intersection",
        description="Stop the active object from penetrating its locked partner (snap-back)",
        default=False,
        update=_on_block_toggle,
    )


# ---------------------------------------------------------------- operators
class IH_OT_check_now(Operator):
    bl_idname = "object.ih_check_now"
    bl_label = "Check now"
    bl_description = "Check intersections once and highlight"

    def execute(self, context):
        if context.scene.ih_settings.auto_object_color:
            set_object_color_shading(context)
        n_hit, n_touch = run_check(context)
        self.report({'INFO'}, "{} intersecting, {} touching".format(n_hit, n_touch))
        return {'FINISHED'}


class IH_OT_clear(Operator):
    bl_idname = "object.ih_clear"
    bl_label = "Clear"
    bl_description = "Turn off live mode and the lock, and restore original colors"

    def execute(self, context):
        s = context.scene.ih_settings
        s.live = False
        s.block_intersect = False
        restore_all()
        return {'FINISHED'}


class IH_OT_set_pair(Operator):
    bl_idname = "object.ih_set_pair"
    bl_label = "Set pair"
    bl_description = "Remember the two selected objects as the no-intersection pair"

    def execute(self, context):
        sel = _selected_mesh(context)
        act = context.view_layer.objects.active
        if act and act.type == 'MESH' and act in sel and len(sel) >= 2:
            a = act
            b = next(o for o in sel if o != act)
        elif len(sel) >= 2:
            a, b = sel[0], sel[1]
        else:
            self.report({'WARNING'}, "Select two mesh objects first")
            return {'CANCELLED'}
        s = context.scene.ih_settings
        s.lock_a, s.lock_b = a, b
        if s.block_intersect:
            _LOCK_GOOD.clear()
            _LOCK_GOOD[a.name] = a.location.copy()
            _LOCK_GOOD[b.name] = b.location.copy()
        self.report({'INFO'}, "Blocked pair: {} + {}".format(a.name, b.name))
        return {'FINISHED'}


# ---------------------------------------------------------------- panel
class IH_PT_panel(Panel):
    bl_label = "Intersection Highlighter"
    bl_idname = "IH_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Intersect"

    def draw(self, context):
        layout = self.layout
        s = context.scene.ih_settings

        layout.prop(s, "live", toggle=True,
                    icon='HIDE_OFF' if s.live else 'HIDE_ON')
        col = layout.column(align=True)
        col.prop(s, "scope")
        col.prop(s, "method")

        row = layout.row(align=True)
        row.operator("object.ih_check_now", icon='VIEWZOOM')
        row.operator("object.ih_clear", icon='X')

        box = layout.box()
        box.prop(s, "hit_color")
        box.prop(s, "touch_color")
        box.prop(s, "touch_threshold")
        box.prop(s, "auto_object_color")

        box = layout.box()
        box.label(text="No-intersection lock", icon='LOCKED')
        box.operator("object.ih_set_pair", icon='EYEDROPPER')
        row = box.row(align=True)
        row.prop(s, "lock_a", text="")
        row.prop(s, "lock_b", text="")
        box.prop(s, "block_intersect", toggle=True,
                 icon='LOCKED' if s.block_intersect else 'UNLOCKED')


# ---------------------------------------------------------------- registration
classes = (IH_Settings, IH_OT_check_now, IH_OT_clear, IH_OT_set_pair, IH_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.ih_settings = PointerProperty(type=IH_Settings)
    for h in list(bpy.app.handlers.depsgraph_update_post):
        if getattr(h, "__name__", "") == "_live_handler":
            bpy.app.handlers.depsgraph_update_post.remove(h)
    bpy.app.handlers.depsgraph_update_post.append(_live_handler)


def unregister():
    restore_all()
    for h in list(bpy.app.handlers.depsgraph_update_post):
        if getattr(h, "__name__", "") == "_live_handler":
            bpy.app.handlers.depsgraph_update_post.remove(h)
    if hasattr(bpy.types.Scene, "ih_settings"):
        del bpy.types.Scene.ih_settings
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == "__main__":
    register()
