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

## 键位

工业兼容键位里 **D** 会切到 Annotate，和按住 D 编辑枢轴冲突。这个 fork 加了一套派生键位，没有改原来的 Industry Compatible。

在 **编辑 → 偏好设置 → 键位映射** 里选择：

**Industry Compatible Custom Pivot**

它和 Industry Compatible 相同，只是去掉了各模式里 `builtin.annotate` 的 **D** 快捷键。Annotate 仍可从工具栏选用。Ctrl+D 等带修饰键的绑定保留。

默认 Blender 键位没有把 D 做成 Annotate 工具切换，D 仍是 Annotate 笔画的修饰键。3D 视图里额外绑了 `view3d.gizmo_pivot_edit`（D 按下和松开，`any`），只刷新视图并继续传递事件，所以点中 gizmo 时枢轴编辑优先生效，Annotate 笔画仍然可用。

## 实现要点

给以后合并上游时对照。

- 新变换上下文 `CTX_GIZMO_PIVOT`。它和 `CTX_CURSOR` 一样不走编辑网格的普通变换路径，吸附可以打到选中物体。
- 新操作符 `TRANSFORM_OT_gizmo_pivot`。只有 `OPTYPE_BLOCKING`，不进撤销栈。复用现有的 transform invoke / modal / cancel / exec，以及变换的 modal 键位。
- 枢轴状态按 `View3D` 存在 `transform_gizmo_3d.cc` 的静态表里，视图释放时清掉。四视图共用一个 `View3D`。
- 位置存在参考物体（或姿态骨骼）的局部空间，确认后才写回；取消靠 transform 自己恢复。编辑网格的跟随用参考顶点相对包围盒中心的偏移来区分平移和旋转/缩放。
- 自定义朝向通过把 `orient_matrix` 和 `orient_matrix_type` 设成同一个方向槽，让变换走 `V3D_ORIENT_CUSTOM_MATRIX`。视图对齐的旋转环（`ROT_C`、`ROT_T`）不用这个矩阵。
- `translate` 补上了 `center_override`，移动也可以使用这个自定义中心。
- Scale 钳制是 gizmo 组属性 `use_clamp_negative`（默认开），画在 `builtin.scale` 的工具设置里。`initResize` 发现当前工具是 3D 视图的 Scale 且勾选打开时，置 `T_CLAMP_SCALE_NONNEGATIVE`。`applyResize` 在生成缩放矩阵前把三个系数钳到不小于 0。

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
- `scripts/presets/keyconfig/Industry_Compatible_Custom_Pivot.py`
