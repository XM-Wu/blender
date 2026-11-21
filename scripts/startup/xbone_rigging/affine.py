# SPDX-License-Identifier: GPL-2.0-or-later
"""Extract Maya-style rotation from an affine transform before reapplying parent scale."""
from mathutils import Matrix, Vector


class LocalFrames:
    def __init__(self, arm):
        self.edit = arm.edit_bones
        self.collection = arm.collections["Mechanism"]
        self.frames = []

    def bone(self, name, frame, parent=None):
        b = self.edit.new(name)
        b.head = frame.translation
        b.tail = b.head + Vector((0, .01, 0))
        b.matrix = frame
        b.parent = parent
        b.use_deform = False
        for collection in list(b.collections):
            collection.unassign(b)
        self.collection.assign(b)
        return b

    def rotation(self, name, source, parent, *, scale=False):
        """Return an output whose local rotation uses orthogonalized X, then Y."""
        identity = Matrix.Identity(4)
        raw = self.bone("MCH.Local." + name, identity)
        markers = []
        for i in range(3 if scale else 2):
            frame = identity.copy()
            frame[i][3] = 1.0
            markers.append(self.bone("MCH.Axis" + str(i) + "." + name, frame, raw).name)
        qr = self.bone("MCH.QR." + name, identity)
        dst = self.bone("MCH.Relative." + name, parent.matrix.copy(), parent)
        readers = []
        if scale:
            output = self.bone("MCH.Scale." + name, parent.matrix.copy(), dst)
            for i in range(3):
                readers.append(self.bone("MCH.ScaleRead" + str(i) + "." + name, identity).name)
        self.frames.append((source.name, parent.name, raw.name, markers, qr.name, dst.name, readers,
                            output.name if scale else None))
        if scale:
            return output
        return dst

    def configure(self, rig):
        def con(name, kind, target):
            c = rig.pose.bones[name].constraints.new(kind)
            c.target, c.subtarget = rig, target
            c.owner_space = c.target_space = 'POSE'
            return c
        for source, parent, raw, axes, qr, dst, readers, output in self.frames:
            c = con(raw, 'COPY_TRANSFORMS', source)
            c.target_space = 'CUSTOM'
            c.space_object, c.space_subtarget = rig, parent
            con(qr, 'COPY_LOCATION', raw)
            c = con(qr, 'DAMPED_TRACK', axes[0])
            c.track_axis = 'TRACK_X'
            c = con(qr, 'LOCKED_TRACK', axes[1])
            c.lock_axis, c.track_axis = 'LOCK_X', 'TRACK_Y'
            c = con(dst, 'COPY_TRANSFORMS', qr)
            c.owner_space = 'LOCAL'
            for i, reader in enumerate(readers):
                c = con(reader, 'COPY_LOCATION', axes[i])
                c.target_space = 'CUSTOM'
                c.space_object, c.space_subtarget = rig, qr
                dr = rig.pose.bones[output].driver_add("scale", i).driver
                dr.type = 'AVERAGE'
                v = dr.variables.new()
                v.type = 'TRANSFORMS'
                t = v.targets[0]
                t.id = rig
                t.bone_target = reader
                t.transform_space = 'LOCAL_SPACE'
                t.transform_type = 'LOC_' + "XYZ"[i]
