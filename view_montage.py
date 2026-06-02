"""Quick multi-view montage for fast modeling iteration.

Renders 6 orthographic views (OpenGL/viewport = fast) of a target object,
auto-frames each, and tiles them into ONE PNG.

Layout (3 cols x 2 rows):
    row 1:  FRONT | BACK  | TOP
    row 2:  LEFT  | RIGHT | BOTTOM

Run from Blender's Text editor (Run Script), or:
    exec(open(r"<path>\\view_montage.py").read()); render_views()
"""
import bpy, numpy as np
from mathutils import Vector

PROJECT = r"C:\Users\Владимир\Documents\App\mcp-blender-cloude"
OUT      = PROJECT + r"\montage_6views.png"
TARGET   = None                # None = frame all visible meshes; or "Name", or ["A","B"]
CELL     = 640                 # px per view
MARGIN   = 1.12                # framing padding


def _resolve(target):
    if target is None:
        return [o for o in bpy.context.scene.objects
                if o.type == 'MESH' and o.visible_get()]
    if isinstance(target, str):
        target = [target]
    return [bpy.data.objects[n] for n in target]


def _bbox(objs):
    cs = []
    for ob in objs:
        mw = ob.matrix_world
        cs += [mw @ Vector(c) for c in ob.bound_box]
    mn = Vector((min(c.x for c in cs), min(c.y for c in cs), min(c.z for c in cs)))
    mx = Vector((max(c.x for c in cs), max(c.y for c in cs), max(c.z for c in cs)))
    return (mn + mx) * 0.5, (mx - mn)


def render_views(target=TARGET, out=OUT, cell=CELL):
    scene = bpy.context.scene
    objs = _resolve(target)
    if not objs:
        raise RuntimeError("No objects to frame")
    center, ext = _bbox(objs)
    em = {'x': ext.x, 'y': ext.y, 'z': ext.z}

    cam = bpy.data.objects.get("_ViewCam")
    if cam is None:
        cam = bpy.data.objects.new("_ViewCam", bpy.data.cameras.new("_ViewCam"))
        scene.collection.objects.link(cam)
    cam.data.type = 'ORTHO'

    views = [
        ("front",  Vector(( 0, 1, 0)), 'Z', 'x', 'z'),
        ("back",   Vector(( 0,-1, 0)), 'Z', 'x', 'z'),
        ("left",   Vector((-1, 0, 0)), 'Z', 'y', 'z'),
        ("right",  Vector(( 1, 0, 0)), 'Z', 'y', 'z'),
        ("top",    Vector(( 0, 0, 1)), 'Y', 'x', 'y'),
        ("bottom", Vector(( 0, 0,-1)), 'Y', 'x', 'y'),
    ]

    scene.render.resolution_x = cell
    scene.render.resolution_y = cell
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    prev_cam = scene.camera
    scene.camera = cam

    imgs = {}
    for name, axis, up, raxis, uaxis in views:
        depth = abs(axis.x)*ext.x + abs(axis.y)*ext.y + abs(axis.z)*ext.z
        dist = depth * 0.5 + max(em.values()) + 0.3
        cam.location = center + axis * dist
        cam.rotation_euler = (center - cam.location).to_track_quat('-Z', up).to_euler()
        cam.data.ortho_scale = max(em[raxis], em[uaxis]) * MARGIN
        cam.data.clip_start = 0.001
        cam.data.clip_end = dist + max(em.values()) + 1.0
        fp = PROJECT + ("\\_v_%s.png" % name)
        scene.render.filepath = fp
        try:
            bpy.ops.render.opengl(write_still=True, view_context=False)
        except Exception:
            bpy.ops.render.render(write_still=True)
        im = bpy.data.images.load(fp, check_existing=False)
        w, h = im.size
        a = np.empty(w * h * 4, dtype=np.float32)
        im.pixels.foreach_get(a)
        imgs[name] = a.reshape(h, w, 4)
        bpy.data.images.remove(im)

    scene.camera = prev_cam

    top = np.concatenate([imgs['front'], imgs['back'], imgs['top']], axis=1)
    bot = np.concatenate([imgs['left'], imgs['right'], imgs['bottom']], axis=1)
    grid = np.concatenate([bot, top], axis=0)  # array row 0 = bottom
    H, W, _ = grid.shape
    mont = bpy.data.images.get("Montage")
    if mont:
        bpy.data.images.remove(mont)
    mont = bpy.data.images.new("Montage", W, H, alpha=True)
    mont.pixels.foreach_set(grid.ravel())
    mont.filepath_raw = out
    mont.file_format = 'PNG'
    mont.save()
    return out


if True:  # run on exec / Run Script
    print("Montage saved:", render_views())
