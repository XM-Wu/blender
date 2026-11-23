# XBone Rigging

Select the **Rigging** workspace tab or open it from the workspace **+ → Rigging** menu in an existing file. It places the 3D Viewport on the left, the Rigging editor in the middle, and the Outliner above Scene Properties on the right. You can also switch any single area to **Animation → Rigging**. The editor follows the active object and bone in the current view layer. In Object Mode, **Create Guide** offers the templates and import tools.

In XBone Edit Mode, **Transform Children** in the viewport header controls whether moving, rotating or scaling a parent also transforms its descendants. Turn it off to place the parent independently.

In Object Mode, open **Create Guide → Place XBiped Landmarks**, then set the character's
forward direction and symmetry. The session starts at the world origin at the bundled XBiped
template size with **Pelvis** selected. Existing guides are hidden while you place markers and
their visibility is restored when you generate the guide or discard the markers. Start in an
empty scene or use a visible character as a reference. The panel names the current marker.
Click **Next Marker** to select the next joint at its template position. In the 3D
Viewport, press **G** to move it. **Previous Marker** selects a placed joint for adjustment.
Revisiting a marker preserves its position. You can save the scene and resume later.
A symmetric setup starts with 12 body markers and reflects the left side across the pelvis.
An asymmetric setup starts with 20. **Hide Other Landmarks** keeps the current marker visible
as you step forward or back. Click it again to review all placed markers.

After placing the body, choose **Match Finger Joints** or **Match Face** for more control.
The finger steps cover three joints and the tip of each digit, reusing the middle fingertip
already placed with the body. The face steps cover eye centers, eye aim targets, the mouth
base, rotation center, jaw and lips. You can complete either group first, then add the other.
Click **Generate Fitted XBiped Guide** and then **Build Rig**. Straight limbs get a small bend
to define the IK plane.

Select an XBiped Guide or its built Rig and choose **Edit XBiped Landmarks** to adjust it again.
Existing XBiped templates can also start from their guide joints. **Update XBiped Guide**
applies the changes to the same guide, including its custom branches. Use **Rig** in the Rigging editor header to return with the edits pending, or **Apply Guide and Rebuild**
to apply them to the existing rig. **Cancel** or **Esc** discards the marker session and returns to the object and mode where it started.
Markers reopen at the current guide pivots. A manually asymmetric guide opens with independent left and right
markers, even if originally created symmetrically. Toggle **Mirror Left to Right** while editing to preview or
disable symmetry. **Update XBiped Guide** applies marker positions to the guide only.

In Object Mode, select the meshes and an XBone, then choose **Mesh Bindings → Add Armature to Selected Meshes**.
The list shows all meshes in the current scene with an Armature modifier targeting that XBone, including hidden meshes.
Click a name to select one mesh or **Select Bound Meshes** to select all selectable matches in the current view layer.
**Remove XBone Modifiers** removes every editable matching modifier in the scene while retaining vertex groups and skin layers.

Use **Left to Right** or **Right to Left** in the Guide panel to mirror guide positions, orientations and limb settings across the armature's local X plane. The operation updates the entire opposite side and creates missing counterparts. Enable **Selected Source Joints Only** in Adjust Last Operation to limit the source. **Shifter Guide Display** switches between guide curves and the ordinary XBone display. Biped and Game Biped use root, locator, blade and pole shapes sampled from Maya 2027.

- **Biped Template** and **Game Biped Template** read the bundled mGear Shifter guides. **Import Shifter Template (.sgt)** reads templates made with the supported components. Set the source up axis and scale in the importer. The presets use Y up and scale factors of 0.1 and 0.01, respectively.
- **Edit Guide** lets you place and rename joints before building. **New Custom Guide** starts with a root. In Edit Mode select the intended parent, then use **Add Guide Component** for FK joints, left or right arms, or legs. Each limb supplies three joint pivots and a bend plane.
- For an extra chain on XBiped, edit its guide, select the attachment joint and use **Add Guide Component → FK Joint**. Set the side and length, rename the joint, then add the next joint with the previous one active. Build or rebuild and enable **Show Additional FK Controls** to pose the new chain. The branch attaches to the skin joint of its parent component, preserving its guide offset for animation and FBX export. Generic FK rings face across the chain; a terminal ring follows the incoming segment. The FK joint retains its authored rotation axes.
- To keep an existing skeleton's joint names, hierarchy and rest frames, select the XBone object and choose **Create Guide from Selected XBone**. Select a wrist or ankle in Edit Mode and choose **Toggle Two-Joint IK at Active Joint**. The two ancestors form the limb. Give a straight limb a small bend toward its intended elbow or knee direction.
- **Build Rig** creates an XBone armature and opens Pose Mode. Bind the character mesh to that armature. Use **Guide / Rig** in the Rigging editor header to switch between editing the source guide and posing the existing rig. Edited joints stay on the guide across switches and file saves. Choose **Apply Guide and Rebuild** on the Guide panel to update the same rig object. Its mesh modifiers, Action and NLA tracks keep their targets. Matching control names retain their poses and animation paths. Changing classic Biped division counts distributes the existing division weights between neighboring new joints by position along the component. Renamed or removed controls may require animation-path edits.
- Biped starts with IK legs and FK arms. In Pose Mode move or rotate **IK.leg_L0_ankle** or **IK.leg_R0_ankle** to pose a foot. Move the pole to steer the knee. Use **Show Skeleton** to see the deformation joints behind the controls.
- The bundled Biped uses rest curves sampled from a Maya 2027 Shifter build, including the world octagon, nested ground squares and sided limb outlines. The pole lines follow the knees and elbows as the rig is posed. Supplemental Shifter outlines follow their XBone joints as display geometry. **Show Additional FK Controls** exposes XBone controls without a matching Shifter outline. Create a new Biped guide and build it to get this layout in an existing scene.
- Rotate FK controls to pose their chains. The limb property blends from FK at 0 to IK at 1. **Match IK** and **Match FK** preserve the pose and select the incoming endpoint control. With Auto Key enabled the operator keys the matched channels and the switch at the current frame. The Biped eye targets aim each eye individually, and the shared eye control moves both targets.
- To export a rest skeleton to FBX, select the generated rig, set the armature to **Rest Position**, and enable **Selected Objects** and **Only Deform Bones** in the FBX exporter. Return to **Pose Position** for animation. Controller and solver bones have deformation disabled.

## XBiped elbows and knees

Choose **XBiped Template → Build Rig** for the Biped controls and skin skeleton with sliding knees.
Each leg has a **Knee Glide** slider in the Rigging editor's Pose Controls panel. At zero the two knee pivots coincide.
The tibial pivot moves anteriorly relative to the femoral end in the bend plane.
Travel increases smoothly from 60° to 155° of flexion. Knee Glide defaults to 1
(3.5% of shin length at peak flexion). Values up to 4 amplify travel proportionally
(7% at 2 and 14% at 4); above 1 only the sliding distance is amplified.
**Tibial Rotation (°)** adds inward rotation to the shin skin joints along their longitudinal
axes using the same bend profile (6° at full strength by default). The approximation models bone
movement only. Both default values are rig parameters rather than subject-specific measurements.

FK carries the ankle and foot with the sliding shin. IK solves the two joint centers against the
reverse-foot effector and pole, keeping reachable ankles planted. Use **Match IK** or **Match FK**
to retain the current pose when switching. The existing Biped and Game Biped templates keep their
original solvers and export hierarchies.

XBiped legs default to **Max Stretch = 1**. Raise this setting on a foot IK control for a
stretchable leg. In FK, select the arc outside either knee or its **Knee FK** button in the
Selector. Rotate local X to bend: a 90° input folds the knee by 90° and swings the thigh
forward by 9°, moving the knee pivot and carrying the foot. The original shin FK control
remains available for independent adjustments. Edit the source with **Guide**, then choose **Apply Guide and Rebuild** to add
the handles to an existing XBiped.

Each XBiped arm has a separate **Elbow Glide** (0–4, default 1), **Forearm Rotation (°)**
(default 6°) and **Elbow FK** handle. Forearm travel uses the same bend profile and is
scaled to forearm length. Rotate the outer elbow arc around local X in FK, or select
**Elbow FK** in the Selector. In IK the wrist position, pole and independent wrist rotation
controls remain available. XBiped arms also start with **Max Stretch = 1**.

## Selector

Split an area and choose **Animation → Selector** in its editor type menu. Choose the character in the header. Classic Biped opens the mGear Biped picker. Game Biped and custom rigs open a board of their selectable controls. The header also switches pages and imports or exports polygon pickers in mGear's `.pkr` format. Imported layouts and available background images are stored on the character in the blend file.

Click a shape to select its pose controls, Shift-click to extend, or Ctrl-click to toggle. Drag on the empty board for box selection. The wheel zooms around the cursor, middle-drag pans, and **F** frames the page. Right-click to place a button bound to the current pose selection, move or rebind an existing button, or remove it.

The stock board includes native limb IK/FK matching, reference-space selection, Select All and transform keying. A dimmed button indicates a missing control or unsupported action. Maya scripts, arbitrary context-menu commands, animated widgets and the spine IK/FK transfer script require separate Blender implementations.

## Classic Biped build

The stock template creates 106 skin joints in the Skeleton collection. Their names, parents and rest frames come from a Maya 2027 Shifter build using mGear revision `a7dd129`. Joint X is the longitudinal axis. Controls, intermediate guide transforms and solver bones have deformation disabled. Skin a mesh using the `*_jnt` names. A mesh bound to an older Biped with guide-based vertex group names needs its weights mapped to this hierarchy.

In **Edit Guide**, select the root guide of an arm or leg to set **Upper Divisions**, **Lower Divisions** and **Support Joints**. Both counts default to 2. Each limb then has nine division joints plus an end joint. Shifter's support samples lie at 49%, 50% and 51% of the two-segment parameter interval. Select the spine or neck component root for its **Divisions** setting (5 and 3 respectively). Rebuild after changing the guide.

The Classic Biped build uses a native two-bone triangle for each arm and leg. Independent FK and IK chains are blended in parent space. The wrist position target (for example **IK.arm_L0_wrist**), its selectable **Display.arm_L0_ikcns_ctl** parent, and the **Display.arm_L0_ikRot_ctl** orientation control have separate animation channels. The wrist rotation space defaults to the forearm. **Match IK** also matches the orientation control.

The reverse-foot pivots follow the IK goal. The forward toe chain starts at the solved ankle and inherits the previous FK toe. Move either foot beyond its reach to see the goal and deformation chain separate. Pose-mode attributes include roll, IK scale, maximum stretch, slide, softness, reverse, volume, roundness, reference spaces and upper-arm roll. Select the limb UI host for its settings, **Display.spineUI_C0_ctl** for spine and neck settings, or **Display.faceUI_C0_ctl** for jaw and lip follow.

The roll solver uses a sampled Bézier span and interpolates the Maya tangent and up frames. Its helpers, including the Geometry Nodes meshes, are hidden and have deformation disabled. The spine and neck use master and slave curves, compensated FK frames and tangent controls. Digit controls propagate translation and rotation through the chain, with segment-specific scale. All of these run in Blender after saving, including when Python auto-execution is disabled.

The regression suite checks the bind hierarchy, Maya single-channel and compound reference poses, division count changes, in-place rebuilds and deform-only FBX round trips. The separate full audit compares all 125 stock Maya controls. Rebuild from the source guide to update an older rig. Control names used for existing animation are retained, but authored rest axes and the wrist rotation channel have changed; check existing animation after rebuilding. Game Biped and custom guides keep the standard XBone IK build. mGear templates and their MIT license are bundled with the startup module.

## Guide and rig lifecycle in mGear Shifter

In the mGear checkout at `a7dd1292`, Guide Explorer's **Build** (Ctrl+B in the tree) selects the guide or component roots and calls `Rig.buildFromSelection()`. Shifter validates these inputs and generates a separate rig hierarchy. In Final mode it hides the original guide when `guide_vis` is off; that attribute defaults to true. **H** toggles guide visibility. Rig controls and joints have their own visibility attributes, `ctl_vis` and `jnt_vis`. [Build and visibility commands](https://github.com/mgear-dev/mgear/blob/a7dd1292/release/scripts/mgear/shifter/guide_explorer/guide_tree_widget.py#L224) · [Build finalization](https://github.com/mgear-dev/mgear/blob/a7dd1292/release/scripts/mgear/shifter/__init__.py#L1229)

**Unbuild** (Ctrl+U in the tree) looks up rig roots via `*.is_rig` and deletes the first result's rig transform. To recreate a guide from the build snapshot, **Extract Guide From Rig** reads the rig root's `guide_data` JSON and imports a guide template. **Extract and Match Guide From Rig** additionally uses supported EPIC joint-to-guide mappings. [Unbuild](https://github.com/mgear-dev/mgear/blob/a7dd1292/release/scripts/mgear/shifter/guide_explorer/guide_tree_widget.py#L241) · [Guide extraction](https://github.com/mgear-dev/mgear/blob/a7dd1292/release/scripts/mgear/shifter/guide_manager.py#L250)

In xBlender, **Guide** follows the selected rig's `rigging_guide` pointer, hides that rig and opens the original guide in Edit Mode. **Rig** returns to that rig in Pose Mode. Only **Apply Guide and Rebuild** applies the guide edits to it. [Edit Source Guide](../scripts/startup/xbone_rigging/__init__.py) · [Rebuild](../scripts/startup/xbone_rigging/rebuild.py)
