# Soft-body examples

Open a top-level scene in xBlender and press Space to play its baked timeline.

- **Slime-Corner-Lift.blend** lifts an off-center patch, sways it, then picks it up again to press it onto a 45-degree ramp (frames 210–337). Animate the grip handle, then use Bake Gel to update the result.
- **Slime-Scene-Collision.blend** drops the gel onto a finite platform below world zero, then raises the platform and presses the gel with an animated paddle. Both meshes use Blender’s Collision physics. The embedded 100-frame bake is ready to play.
- **Slime-Live-Grab.blend** opens with a free soft slime selected. Start the live simulation in Physics Properties and select the gel and left-drag its surface while the timeline is paused. Select the amber capsule and move its gizmo to push the gel. Very fast live strokes can briefly lead the simulated contact surface; it catches up when you hold the rod steady. Reselect the gel to grab its surface again. Adjust Grab Radius to change the size of the pulled patch. The xBlender build installs the native live-preview kernels alongside its Python modules. Script-only installations can use the NumPy fallback.
- **Slime-Volumetric-Gel.blend** compares four tetrahedral FEM materials in the same side strike and overhead press. Open a material scene to adjust shear and bulk modulus in Physics Properties → Volumetric Gel. Choose a slime material preset and click **Start Live Simulation** to experiment with the timeline paused. With **Mouse Grab** enabled, left-drag the surface in the viewport and release to let it rebound. **Stop Gel Preview** restores baked playback.
- **Slime-Water-Balloon.blend** and **Slime-Full-Water-Balloon.blend** demonstrate Cloth pressure and gravity on a closed membrane.
- **Slime-Retention-Quartet.blend** calibrates four settings against the full water balloon at frame 114.
- **Slime-80-Fold-Control.blend** and **Slime-Fold-Control-All.blend** compare the original, smooth and guided folds under the same rod.
- **Slime-Gravity-Softness.blend** compares four membranes settling under gravity, followed by two strikes.
- **Slime-Impact.blend** and **Slime-Double-Impact.blend** are the early Blender Soft Body versions.

Keep the `libraries` directory beside the comparison files when moving them. Its individual scenes include the editable source mesh, striker animation and baked Cloth cache. Open a library scene directly to edit or rebake a grade.

## Scene collisions and playback

Select a mesh such as a platform, an obstacle or a paddle and enable **Physics Properties → Collision**. Volumetric Gel follows its evaluated shape and transform, including keyframed motion. Set the collider’s outer thickness to adjust the contact gap. With **Ground Plane → Automatic**, the first enabled mesh collider replaces the fallback ground. Choose **Off** for free fall even in an empty scene, or **Always** to keep an additional horizontal floor at Ground Height.

Select the gel and choose a **Simulation Mode** in Physics Properties.

- **Real Time** runs independently while the timeline is paused. Start Live Simulation to grab the gel or move a collider by hand.
- **Timeline** previews the selected gel at the scene frame rate. Use Space to play or scrub to a frame. Seeking backward recomputes from the simulation start, including Settle Frames.
- **Bake Gel** samples the scene frame range, animated collision meshes and pins, and embeds the result in the blend file for scrubbing, rendering and reopening. For a reproducible push, keyframe the collider. Mouse Grab is an interactive preview tool. Use **Clear Bake** to return to an editable timeline preview, or bake again after changing the setup.
