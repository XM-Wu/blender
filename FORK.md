# 这个 Fork 改了什么

基于 Blender **5.3.0 Alpha**（`main`，`703834468bf`，2026-10-03）。上游功能都还在，下面只列这个 fork 自己加的行为。

## 自定义枢轴

对齐 Maya 的 Custom Pivot。只作用于 3D 视图里的 **Move / Rotate / Scale / Transform** 工具（`builtin.move`、`builtin.rotate`、`builtin.scale`、`builtin.transform`）。选择工具上的 Context Gizmo 不在范围内。

按住 **D**（按住，不是切换模式）时：

- 同时出现移动和旋转手柄，缩放手柄隐藏。
- 拖移动手柄改枢轴位置，拖旋转手柄（含 trackball 和视图转盘）改枢轴朝向。
- 缩放手柄不参与编辑枢轴。
- 松手后，后续的移动、旋转、缩放都绕这个枢轴进行。朝向被改过之后，gizmo 轴向跟着新的朝向。
- 整组选择共用一个枢轴。Individual Origins 也会被这个中心覆盖。
- 换选择（物体、网格元素、编辑骨骼或姿态骨骼）后枢轴重置，不会钉在原地。

吸附沿用 Blender 自己的吸附：

- 吸附目标、元素来自当前工具设置。
- 和普通变换一样，用 **Ctrl** 开关吸附：吸附关着时按住 Ctrl 打开，开着时按住 Ctrl 关掉。
- 枢轴可以吸到当前选中或活动物体上，和 3D 游标的吸附范围一致。

枢轴位置记在活动物体的局部空间里；姿态模式记在活动姿态骨骼的 `pose_mat` 空间里，所以物体或骨骼变换之后枢轴会跟着走。朝向是世界空间四元数，不会跟着物体一起转。网格编辑模式里，如果选择只是平移（相对包围盒中心的偏移没变），枢轴跟着平移；绕枢轴旋转或缩放时，枢轴留在原地。右键取消一次还没确认的枢轴编辑，会回到编辑前的位置和朝向。

## Scale 工具限制负缩放

3D 视图的 Scale 工具默认把缩放系数钳制到 0，拖过枢轴也不会翻到负数。工具设置（顶栏和侧边栏的 Active Tool）里有 **Clamp Negative** 勾选框，关掉后恢复原来的负缩放。

只在当前工具是 Scale 时生效。Move、Rotate、Transform、Scale Cage，以及快捷键在其它工具下触发的缩放，都不钳制。

## 目标焊接

编辑模式的 3D 视图工具栏里有 **Target Weld**。在顶点或边选择模式下，按住一个顶点或一条边，拖到另一个顶点或另一条边上松开，源焊到目标上，目标留在原地。

- 只处理当前活动物体，不跨物体。
- 顶点和边选择同时打开时，离光标更近的那种元素作为拖动对象。拖动开始后只在同一种元素里找目标。
- 单击和选择工具相同：普通单击替换选择，点空白处取消全部选择；**Shift+单击**切换该顶点或边。只开面选择时单击仍然能选面，但拖动不会焊接。
- 按下后先进入模态。拖过拖动阈值才算焊接；没点中顶点或边时，拖动不会被当成单击。Esc 或右键取消。一次成功的焊接是一步撤销，单击改选择也是一步撤销。
- 拖动开始后才换十字光标并画引导线，单击不会闪一下焊接光标。拖动中源是橙色，目标是绿色。

边的两端这样配对：已经共用的顶点保持不动；否则取两端位移平方和更小的那一种，避免大致平行的边拧过去。然后把每个源顶点焊到配对的目标顶点上，源边塌到目标边上。

键位在 `3D View Tool: Edit Mesh, Target Weld`，使用当前键位预设的工具鼠标。Industry Compatible 会从默认键位带上这项，并额外得到中键。悬停 gizmo 带 `WM_GIZMO_HIDDEN_KEYMAP`，高亮时不吃掉这次点击。

## 键位

工业兼容键位里 **D** 会切到 Annotate，和按住 D 编辑枢轴冲突。这个 fork 加了一套派生键位，没有改原来的 Industry Compatible。

在 **编辑 → 偏好设置 → 键位映射** 里选择：

**XM KeyMapping**

它和 Industry Compatible 相同，只是去掉了各模式里 `builtin.annotate` 的 **D** 快捷键。Annotate 仍可从工具栏选用。Ctrl+D 等带修饰键的绑定保留。

这套映射额外有这些网格编辑操作：

- 网格编辑、**Move** 工具激活时，按住 **Ctrl+Shift** 拖移动 gizmo（轴、平面或中心）直接做 Vertex Slide，松开确认。拖的是 gizmo，不是中键。
- **Ctrl+中键**沿法线缩放（Shrink/Fatten），松开确认。不依赖当前是哪个工具。
- **Shift+V** 开始顶点/边滑动。先尝试 Edge Slide，不行再 Vertex Slide。变换模态里原来的 `VERT_EDGE_SLIDE` 快捷键已去掉，只保留这一组 **Shift+V**。

已经加载过的用户键位是一份拷贝。改完后要在偏好设置里重新选择 XM KeyMapping，新快捷键才会写进去。

默认 Blender 键位没有把 D 做成 Annotate 工具切换，D 仍是 Annotate 笔画的修饰键。3D 视图里额外绑了 `view3d.gizmo_pivot_edit`（D 按下和松开，`any`），只刷新视图并继续传递事件，所以点中 gizmo 时枢轴编辑优先生效，Annotate 笔画仍然可用。

## 默认挤出

网格编辑的挤出工具组第一项是 **Extrude**（`builtin.extrude_outset`），也是默认子工具。它是 OmniOutset（ZXY，GPL-3.0-or-later）的内置移植，逻辑仍在 `mesh.omni_outset`。

`mesh.omni_outset` 按当前选择模式分流：

- 面选择：沿各面法线挤出，侧壁间距均匀（`mesh.omni_outset_face`）。
- 边选择：边等距向外挤出（`mesh.omni_outset_edge`）。
- 只有顶点选择：没有对应的 outset，退回原来的 `view3d.edit_mesh_extrude_move_normal`。

拖动时对角线位移同时改挤出距离。按住 **Ctrl** 改的是向外偏移，按住 **Shift** 降低拖动灵敏度。右键或 Esc 取消并还原网格。工具设置里可以开关按选择尺寸自适应拖动，以及 HUD 和字号。设置记在 `WindowManager.omni_outset` 上，不是插件偏好。

默认键位的 **E**，以及 Industry Compatible 的 **Ctrl+E**，都指向这个挤出。Alt+E 挤出菜单、边菜单和面菜单的第一项也是它。原来的区域挤出还在工具组里，标签是 **Extrude Region**。

## 物体模式下的 Gizmo 位置

物体模式的视口顶栏里，变换枢轴左边有一个边界框开关（`tool_settings.use_gizmo_object_center`）。

- 关掉时，gizmo 仍画在当前枢轴上。
- 打开时，物体模式的移动、旋转、缩放 gizmo 画在所选物体几何包围盒的中心。没有包围盒的物体用原点参与计算。多个物体取合并后的包围盒中心。用 gizmo 旋转或缩放时绕这个中心。
- **G / R / S** 仍使用旁边的变换枢轴，不跟这个开关走。
- 按住 **D** 确认过的自定义枢轴优先于这个开关。

开关只在物体模式显示。标志位是 `ToolSettings.transform_flag` 的 `SCE_XFORM_GIZMO_OBJECT_CENTER`，默认关。

## 实现要点

给以后合并上游时对照。

- 新变换上下文 `CTX_GIZMO_PIVOT`。它和 `CTX_CURSOR` 一样不走编辑网格的普通变换路径，吸附可以打到选中物体。
- 新操作符 `TRANSFORM_OT_gizmo_pivot`。只有 `OPTYPE_BLOCKING`，不进撤销栈。复用现有的 transform invoke / modal / cancel / exec，以及变换的 modal 键位。
- 枢轴状态按 `View3D` 存在 `transform_gizmo_3d.cc` 的静态表里，视图释放时清掉。四视图共用一个 `View3D`。
- 位置存在参考物体（或姿态骨骼）的局部空间，确认后才写回；取消靠 transform 自己恢复。编辑网格的跟随用参考顶点相对包围盒中心的偏移来区分平移和旋转/缩放。
- 自定义朝向通过把 `orient_matrix` 和 `orient_matrix_type` 设成同一个方向槽，让变换走 `V3D_ORIENT_CUSTOM_MATRIX`。视图对齐的旋转环（`ROT_C`、`ROT_T`）不用这个矩阵。
- `translate` 补上了 `center_override`，移动也可以使用这个自定义中心。
- Scale 钳制是 gizmo 组属性 `use_clamp_negative`（默认开），画在 `builtin.scale` 的工具设置里。`initResize` 发现当前工具是 3D 视图的 Scale 且勾选打开时，置 `T_CLAMP_SCALE_NONNEGATIVE`。`applyResize` 在生成缩放矩阵前把三个系数钳到不小于 0。
- XM KeyMapping 给 Generic Gizmo 的拖拽补了 Ctrl+Shift+左键，否则 gizmo 键位不认修饰键，拖不到手柄上。`gizmo_move_vert_slide` 只在这套键位、`builtin.move`、网格编辑、拖的是移动手柄时，把操作符换成 `TRANSFORM_OT_vert_slide`。按住 D 编辑枢轴优先。
- **Shift+V** 走 `mesh.vert_edge_slide`。包装操作符的 `bl_options` 为空，撤销记在它调用的 slide 上。它先 `transform.edge_slide`，失败再 `transform.vert_slide`。变换模态映射里的 `VERT_EDGE_SLIDE` 也改成 Shift+V，并删掉原来的那条。
- 目标焊接的拾取走 `em_setup_viewcontext`。`ED_view3d_viewcontext_init` 不填 `em`，最近元素查询会直接空返回。操作符事件也没有 GL 上下文，查询前要调用 `view3d_operator_needs_gpu`，否则选中缓冲读不到。边焊接用 bmesh `weld_verts` 的 `targetmap`，目标顶点留在原地；`pointmerge` 会把参与的顶点收到同一个点，不能用来焊边。单击选择用 `EDBM_select_pick`：Shift 是 `SEL_OP_XOR`，否则 `SEL_OP_SET` 并 `deselect_all`。用 `WM_event_drag_test` 区分单击和拖动，光标和绘制回调只在拖动开始后装上。
- 默认挤出是工具组元组的第一项 `builtin.extrude_outset`。`mesh.omni_outset` 只做分流，`bl_options` 为空，子操作符还在模态时父级返回 `FINISHED`，避免挤出进 Adjust Last Operation。源码按 GPL-3.0-or-later 放在 `bl_operators/mesh_omni_outset.py`，HUD 和翻译分文件。
- 物体中心 gizmo 在 `gizmo_3d_calc_pos` 之后、自定义枢轴之前写入 `twmat[3]`。拖动 gizmo 时给 `center_override` 写同一个包围盒中心，自定义枢轴有效时不覆盖。

主要改动文件：

- `source/blender/editors/transform/transform_gizmo_3d.cc`
- `source/blender/editors/transform/transform_mode_resize.cc`
- `scripts/startup/bl_ui/space_toolsystem_toolbar.py`
- `source/blender/editors/transform/transform_ops.cc`
- `source/blender/editors/transform/transform.cc`
- `source/blender/editors/transform/transform.hh`
- `source/blender/editors/transform/transform_convert.cc`
- `source/blender/editors/transform/transform_convert.hh`
- `source/blender/editors/transform/transform_generics.cc`
- `source/blender/editors/transform/transform_snap.cc`
- `source/blender/editors/include/ED_transform.hh`
- `source/blender/editors/space_view3d/space_view3d.cc`
- `source/blender/editors/space_view3d/view3d_ops.cc`
- `scripts/presets/keyconfig/keymap_data/blender_default.py`
- `scripts/presets/keyconfig/keymap_data/industry_compatible_data.py`
- `scripts/presets/keyconfig/XM_KeyMapping.py`
- `source/blender/editors/mesh/editmesh_target_weld.cc`
- `source/blender/editors/mesh/mesh_intern.hh`
- `source/blender/editors/mesh/mesh_ops.cc`
- `source/blender/editors/mesh/CMakeLists.txt`
- `scripts/startup/bl_operators/mesh_omni_outset.py`
- `scripts/startup/bl_operators/omni_outset_hud.py`
- `scripts/startup/bl_operators/omni_outset_translation.py`
- `scripts/startup/bl_operators/__init__.py`
- `scripts/startup/bl_operators/mesh.py`
- `scripts/startup/bl_ui/space_view3d.py`
- `source/blender/makesdna/DNA_scene_types.h`
- `source/blender/makesrna/intern/rna_scene.cc`
