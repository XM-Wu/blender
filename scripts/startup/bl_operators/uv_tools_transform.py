# SPDX-License-Identifier: GPL-2.0-or-later
"""UV orientation, edge-chain straightening and regular quad rectangularization."""
import math
import bpy
import bmesh
from collections import defaultdict, deque
from mathutils import Vector
from mathutils.geometry import box_fit_2d
from bpy.props import EnumProperty, PointerProperty
from bpy_extras.bmesh_utils import bmesh_linked_uv_islands
from .uvcalc_transform import is_face_uv_selected_fn_from_context, is_loop_edge_uv_selected_fn_from_context


MODES = (
    ('ORIENT_H', 'Orient Horizontally', 'Rotate island long side to U'),
    ('ORIENT_V', 'Orient Vertically', 'Rotate island long side to V'),
    ('ORIENT_3D', 'Align UV to 3D', 'Orient selected islands using the main 3D axis, falling back when it has no stable tangent direction'),
    ('ROTATE_CW', 'Rotate Clockwise 90°', 'Rotate each selected island clockwise 90 degrees around its bounds center'),
    ('ROTATE_CCW', 'Rotate Counterclockwise 90°', 'Rotate each selected island counterclockwise 90 degrees around its bounds center'),
    ('ROTATE_180', 'Rotate 180°', 'Rotate each selected island 180 degrees around its bounds center'),
    ('STRAIGHT', 'Straighten', 'Straighten selected open edge chains between their endpoints'),
    ('STRAIGHT_H', 'Straighten Horizontally', 'Straighten selected open edge chains along U'),
    ('STRAIGHT_V', 'Straighten Vertically', 'Straighten selected open edge chains along V'),
    ('RECT_H', 'Rectangularize Horizontally', 'Make regular quad islands rectangular, long side along U'),
    ('RECT_V', 'Rectangularize Vertically', 'Make regular quad islands rectangular, long side along V'),
)

AXES = (('X','+X',''),('Y','+Y',''),('Z','+Z',''),
        ('NEG_X','−X',''),('NEG_Y','−Y',''),('NEG_Z','−Z',''))
TARGET_ANGLES = {'POS_U':0.0, 'NEG_U':math.pi, 'POS_V':math.pi/2, 'NEG_V':-math.pi/2}


class UVToolsOrientationSettings(bpy.types.PropertyGroup):
    reference: EnumProperty(name='Reference', default='BOUNDS', items=(
        ('BOUNDS','UV Shape','Orient the UV bounding rectangle'),
        ('EDGES','Selected Edges','Use selected UV edges to orient the whole island without deforming it'),
        ('GEOMETRY','3D Direction','Use model-space directions to orient the whole island')))
    axis: EnumProperty(name='Main Axis', items=AXES, default='Z')
    fallback_axis: EnumProperty(name='Fallback', items=AXES, default='Y')
    space: EnumProperty(name='Space', items=(('LOCAL','Local','Object local axes'),('WORLD','World','World axes including object transforms')), default='LOCAL')
    target: EnumProperty(name='Target', default='POS_V', items=(
        ('POS_U','+U',''),('NEG_U','−U',''),('POS_V','+V',''),('NEG_V','−V','')))


def rotate_points(points, angle):
    center = Vector(((min(p.x for p in points)+max(p.x for p in points))/2,
                     (min(p.y for p in points)+max(p.y for p in points))/2))
    c, s = math.cos(angle), math.sin(angle)
    return [center+Vector((c*(p.x-center.x)-s*(p.y-center.y),
                           s*(p.x-center.x)+c*(p.y-center.y))) for p in points]


def selected_edge_angle(faces, layer, selected, vertical):
    ids, _, _ = nodes_for(faces, layer)
    seen = set()
    cosine = sine = total = 0.0
    for face in faces:
        for loop in face.loops:
            if not selected(loop):
                continue
            other = loop.link_loop_next
            key = tuple(sorted((ids[loop], ids[other])))
            if key in seen:
                continue
            seen.add(key)
            direction = other[layer].uv-loop[layer].uv
            length = direction.length
            if length < 1e-12:
                continue
            angle = math.atan2(direction.y, direction.x)
            cosine += length*math.cos(2*angle)
            sine += length*math.sin(2*angle)
            total += length
    if total <= 1e-12 or math.hypot(cosine,sine) < total*1e-4:
        raise ValueError('Select a clear UV edge direction; perpendicular or collapsed selections are ambiguous')
    angle = (math.pi/2 if vertical else 0.0)-math.atan2(sine,cosine)/2
    # Edges have no intrinsic forward direction: prefer the smallest rigid rotation.
    return (angle+math.pi/2) % math.pi-math.pi/2


def geometry_axis_direction(triangles, layer, axis, matrix=None):
    """Area-weighted tangent gradients, normalized to avoid UV density dominating direction.

    Return None for a normal-to-surface axis or incoherent folded geometry. This
    is our implementation, not a reproduction of RizomUV's proprietary solver.
    """
    direction = Vector((0,0))
    total_area = weight_sum = 0.0
    index = {'X':0,'Y':1,'Z':2}[axis[-1]]
    sign = -1 if axis.startswith('NEG_') else 1
    axis_vector = Vector(tuple(sign if i == index else 0 for i in range(3)))
    for tri in triangles:
        p = [matrix @ loop.vert.co if matrix is not None else loop.vert.co for loop in tri]
        a, b = p[1]-p[0], p[2]-p[0]
        normal = a.cross(b)
        area = normal.length*.5
        total_area += area
        if area < 1e-12 or normal.length < (a.length_squared+b.length_squared)*1e-8:
            continue
        normal.normalize()
        relevance = (axis_vector-normal*axis_vector.dot(normal)).length
        if relevance < 1e-6:
            continue
        u, v = tri[1][layer].uv-tri[0][layer].uv, tri[2][layer].uv-tri[0][layer].uv
        determinant = u.x*v.y-u.y*v.x
        if abs(determinant) <= max(u.length_squared+v.length_squared,1e-20)*1e-8:
            continue
        h1,h2 = a.dot(axis_vector),b.dot(axis_vector)
        gradient = Vector(((h1*v.y-h2*u.y)/determinant,(u.x*h2-v.x*h1)/determinant))
        if gradient.length < 1e-12:
            continue
        weight = area*relevance
        direction += gradient.normalized()*weight
        weight_sum += weight
    if weight_sum < max(total_area*1e-6,1e-12) or direction.length < weight_sum*.1:
        return None
    return direction.normalized()


def orient(points, vertical):
    center = (Vector((min(p.x for p in points), min(p.y for p in points))) +
              Vector((max(p.x for p in points), max(p.y for p in points)))) * 0.5
    def rotate(angle):
        c, s = math.cos(angle), math.sin(angle)
        return [Vector((c*(p.x-center.x)-s*(p.y-center.y),
                        s*(p.x-center.x)+c*(p.y-center.y))) + center for p in points]
    angle = box_fit_2d(points)
    # box_fit_2d returns the angle applied to the input coordinates.
    result = rotate(angle)
    width = max(p.x for p in result)-min(p.x for p in result)
    height = max(p.y for p in result)-min(p.y for p in result)
    if (vertical and width > height) or (not vertical and height > width):
        result = rotate(angle + math.pi/2)
    return result


def nodes_for(faces, layer):
    nodes, loops, coordinates = {}, defaultdict(list), {}
    for face in faces:
        for loop in face.loops:
            key = (loop.vert.index, tuple(loop[layer].uv))
            node = nodes.setdefault(key, len(nodes))
            loops[node].append(loop)
            coordinates[node] = loop[layer].uv.copy()
    return {loop: node for node, items in loops.items() for loop in items}, loops, coordinates


def rectangle(faces, layer):
    if any(len(f.verts) != 4 for f in faces):
        raise ValueError('Rectangularize requires a regular quad island')
    ids, loops, old = nodes_for(faces, layer)
    quads = [[ids[l] for l in face.loops] for face in faces]
    neighbors = defaultdict(list)
    for i, quad in enumerate(quads):
        for j in range(4):
            neighbors[tuple(sorted((quad[j], quad[(j+1)%4])))].append(i)
    if any(len(items) > 2 for items in neighbors.values()):
        raise ValueError('Non-manifold UV island')
    grid = dict(zip(quads[0], ((0, 0), (1, 0), (1, 1), (0, 1))))
    queue, done = deque([0]), {0}
    while queue:
        fi = queue.popleft()
        q = quads[fi]
        for j in range(4):
            a, b, inside = q[j], q[(j+1)%4], q[(j+3)%4]
            for ni in neighbors[tuple(sorted((a, b)))]:
                if ni in done:
                    continue
                other = quads[ni]
                k = other.index(b)
                if other[(k+1)%4] != a:
                    raise ValueError('Inconsistent quad winding')
                offset = (grid[a][0]-grid[inside][0], grid[a][1]-grid[inside][1])
                for node, base in ((other[(k+2)%4], a), (other[(k+3)%4], b)):
                    value = (grid[base][0]+offset[0], grid[base][1]+offset[1])
                    if node in grid and grid[node] != value:
                        raise ValueError('Island has an incompatible loop or pole; add a seam')
                    grid[node] = value
                done.add(ni)
                queue.append(ni)
    if len(done) != len(quads) or len(set(grid.values())) != len(grid):
        raise ValueError('Island is not a rectangular quad grid')
    xs, ys = [v[0] for v in grid.values()], [v[1] for v in grid.values()]
    nx, ny = max(xs)-min(xs), max(ys)-min(ys)
    if nx*ny != len(quads):
        raise ValueError('Island has holes or a non-rectangular boundary')
    lengths = (defaultdict(list), defaultdict(list))
    for a, b in neighbors:
        x, y = grid[a], grid[b]
        axis = 0 if x[0] != y[0] else 1
        distance = (loops[a][0].vert.co-loops[b][0].vert.co).length
        lengths[axis][min(x[axis], y[axis])].append(distance)
    axes = []
    for axis, values in enumerate((xs, ys)):
        positions = {min(values): 0.0}
        for i in range(min(values), max(values)):
            samples = lengths[axis][i]
            positions[i+1] = positions[i] + sum(samples)/len(samples)
        axes.append(positions)
    result = {n: Vector((axes[0][xy[0]], axes[1][xy[1]])) for n, xy in grid.items()}
    def area(coords):
        return sum(abs(sum(coords[q[i]].cross(coords[q[(i+1)%4]]) for i in range(4)))*0.5 for q in quads)
    old_area, new_area = area(old), area(result)
    if min(old_area, new_area) < 1e-16:
        raise ValueError('Degenerate island')
    # Keep UV area, center and winding; only the grid shape changes.
    sign = sum(old[quads[0][i]].cross(old[quads[0][(i+1)%4]]) for i in range(4))
    old_center = sum(old.values(), Vector((0, 0)))/len(old)
    center = sum(result.values(), Vector((0, 0)))/len(result)
    scale = math.sqrt(old_area/new_area)
    result = {n: Vector(((p.x-center.x)*scale, (p.y-center.y)*scale*(1 if sign > 0 else -1))) + old_center
              for n, p in result.items()}
    return loops, result


class UVTOOLS_OT_transform(bpy.types.Operator):
    bl_idname = 'uv_tools.transform'
    bl_label = 'UV Transform'
    bl_options = {'REGISTER', 'UNDO'}
    mode: EnumProperty(items=MODES)

    @classmethod
    def description(cls, context, properties):
        return next(item[2] for item in MODES if item[0] == properties.mode)

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.area and context.area.type == 'IMAGE_EDITOR'

    def execute(self, context):
        plans, meshes = [], []
        settings = context.scene.uv_tools_orientation
        fallback_count = 0
        # BMLoop wrappers do not keep their parent BMesh wrapper alive.
        # Retain every owner, including earlier meshes in multi-object edit mode.
        bmeshes = []
        try:
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(obj.data)
                bmeshes.append(bm)
                bm.verts.index_update()
                layer = bm.loops.layers.uv.active
                if layer is None:
                    continue
                meshes.append(obj.data)
                face_selected = is_face_uv_selected_fn_from_context(context.scene, bm)
                edge_selected = is_loop_edge_uv_selected_fn_from_context(context.scene, bm)
                triangles_by_face = defaultdict(list)
                if self.mode == 'ORIENT_3D':
                    for tri in bm.calc_loop_triangles():
                        triangles_by_face[tri[0].face].append(tri)
                for faces in bmesh_linked_uv_islands(bm, layer):
                    if any(f.hide for f in faces):
                        continue
                    if self.mode.startswith('STRAIGHT'):
                        ids, nodes, points = nodes_for(faces, layer)
                        graph = defaultdict(set)
                        for face in faces:
                            for loop in face.loops:
                                if edge_selected(loop):
                                    a, b = ids[loop], ids[loop.link_loop_next]
                                    graph[a].add(b); graph[b].add(a)
                        remaining = set(graph)
                        while remaining:
                            component, todo = set(), [next(iter(remaining))]
                            while todo:
                                node = todo.pop()
                                if node not in component:
                                    component.add(node); todo.extend(graph[node]-component)
                            remaining -= component
                            ends = [n for n in component if len(graph[n]) == 1]
                            if len(ends) != 2 or any(len(graph[n]) > 2 for n in component):
                                raise ValueError('Select open edge chains without branches or closed loops')
                            chain = [min(ends)]
                            while len(chain) < len(component):
                                chain.append(next(n for n in graph[chain[-1]] if len(chain) == 1 or n != chain[-2]))
                            a, b = points[chain[0]], points[chain[-1]]
                            distance = (b-a).length
                            if distance < 1e-12:
                                raise ValueError('Chain endpoints overlap')
                            direction = (b-a).normalized()
                            if self.mode == 'STRAIGHT_H':
                                direction = Vector((1 if b.x >= a.x else -1, 0))
                            elif self.mode == 'STRAIGHT_V':
                                direction = Vector((0, 1 if b.y >= a.y else -1))
                            lengths = [(points[y]-points[x]).length for x, y in zip(chain, chain[1:])]
                            total, offset = sum(lengths), 0.0
                            start = (a+b)*0.5-direction*distance*0.5
                            for i, node in enumerate(chain):
                                value = start + direction*(distance*offset/total)
                                plans.extend((loop, layer, value) for loop in nodes[node])
                                if i < len(lengths): offset += lengths[i]
                    elif any(face_selected(f, True) for f in faces):
                        if self.mode.startswith('RECT'):
                            nodes, points = rectangle(faces, layer)
                        else:
                            _, nodes, points = nodes_for(faces, layer)
                        ordered = list(points)
                        values = [points[n] for n in ordered]
                        if self.mode == 'ORIENT_3D':
                            triangles = [tri for face in faces for tri in triangles_by_face[face]]
                            matrix = obj.matrix_world if settings.space == 'WORLD' else None
                            direction = geometry_axis_direction(triangles,layer,settings.axis,matrix)
                            if direction is None:
                                direction = geometry_axis_direction(triangles,layer,settings.fallback_axis,matrix)
                                fallback_count += 1
                            if direction is None:
                                raise ValueError('Neither 3D axis gives a stable UV direction; choose another axis or select reference edges')
                            result = rotate_points(values,TARGET_ANGLES[settings.target]-math.atan2(direction.y,direction.x))
                        elif self.mode in {'ORIENT_H','ORIENT_V'} and settings.reference == 'EDGES':
                            result = rotate_points(values,selected_edge_angle(faces,layer,edge_selected,self.mode=='ORIENT_V'))
                        elif self.mode.startswith('ROTATE'):
                            angle = {'ROTATE_CW': -math.pi/2, 'ROTATE_CCW': math.pi/2, 'ROTATE_180': math.pi}[self.mode]
                            center = Vector(((min(p.x for p in values)+max(p.x for p in values))/2,
                                             (min(p.y for p in values)+max(p.y for p in values))/2))
                            c, s = math.cos(angle), math.sin(angle)
                            result = [center + Vector((c*(p.x-center.x)-s*(p.y-center.y),
                                                       s*(p.x-center.x)+c*(p.y-center.y))) for p in values]
                        else:
                            result = orient(values, self.mode.endswith('_V'))
                        plans.extend((loop, layer, value) for n, value in zip(ordered, result) for loop in nodes[n])
            if not plans:
                raise ValueError('Select UV islands or open UV edge chains')
            for loop, layer, value in plans:
                if loop[layer].pin_uv and (loop[layer].uv-value).length > 1e-6:
                    raise ValueError('Unpin affected UVs before transforming')
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        for loop, layer, value in plans:
            loop[layer].uv = value
        for mesh in meshes:
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        if fallback_count:
            self.report({'INFO'}, 'Used fallback axis for {} island(s)'.format(fallback_count))
        return {'FINISHED'}


def register():
    bpy.types.Scene.uv_tools_orientation = PointerProperty(type=UVToolsOrientationSettings)


def unregister():
    del bpy.types.Scene.uv_tools_orientation


classes = (UVToolsOrientationSettings, UVTOOLS_OT_transform)
