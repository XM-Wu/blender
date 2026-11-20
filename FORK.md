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
- 拖动开始后才换十字光标并画引导线，单击不会闪一下焊接光标。拖动中源是橙色，目标是绿色，引导线是浅蓝色。顶点只画点，不把一个点和它的镜像连成边。边则分别画出源边、目标边，以及各自的镜像边。
- 网格对称打开时，焊一侧会连同镜像顶点、镜像边一起焊。X、Y、Z 可同时开，后开的轴会再镜像前面已经生成的配对，所以斜向那一份也会焊上。拓扑镜像打开时同样生效。落在镜像平面上的点是它自己的镜像，不会被焊两次。把点直接焊到它的镜像点上时只保留这一次，不会再反向焊回来。两侧可以焊到同一个中心点。单击选择不跟着镜像。

边的两端这样配对：已经共用的顶点保持不动；否则取两端位移平方和更小的那一种，避免大致平行的边拧过去。然后把每个源顶点焊到配对的目标顶点上，源边塌到目标边上。

键位在 `3D View Tool: Edit Mesh, Target Weld`，使用当前键位预设的工具鼠标。Industry Compatible 会从默认键位带上这项，并额外得到中键。悬停 gizmo 带 `WM_GIZMO_HIDDEN_KEYMAP`，高亮时不吃掉这次点击。

## Knife 对称

编辑模式的 Knife，以及 Knife Project，在网格对称打开时会把每一段确认的切割映到对称的另一侧。

- 镜像发生在物体局部空间，映的是这一刀的交点，不是把屏幕上的笔画像翻转一次。透视下镜像那一刀的切割面不经过相机。
- X、Y、Z 可同时开，斜向那一份也会切。拓扑镜像打开时同样生效。顶栏显示 Symmetry。
- 拖动预览同时画出镜像那一刀和镜像交点。两端都落在原来这一刀上时不再另画一条线。
- 整刀已经躺在镜像平面上时不再切第二次。平面上新产生的点记成它自己的镜像，后面从这里继续切仍能连上。
- 一刀穿过对称面时，映到本刀已经切过的交点上的点会丢掉，避免另一侧切两次。落在平面上的点保留，从中心切出去仍能连上。镜像交点不足两个时跳过这一份。
- 这是网格编辑对称，不是 Mirror 修改器。带 Mirror 修改器时，编辑的仍是真实的那一半，修改器自己复制结果。

## 键位

工业兼容键位里 **D** 会切到 Annotate，和按住 D 编辑枢轴冲突。这个 fork 加了一套派生键位，没有改原来的 Industry Compatible。

在 **编辑 → 偏好设置 → 键位映射** 里选择：

**XM KeyMapping**

XM 沿用 Industry Compatible 的鼠标操作，并去掉了各模式里 `builtin.annotate` 的 **D** 快捷键。Annotate 仍可从工具栏选用。Ctrl+D 等带修饰键的绑定保留。

这套映射额外有这些网格编辑操作：

- 网格编辑、**Move** 工具激活时，按住 **Ctrl+Shift** 拖移动 gizmo（轴、平面或中心）直接做 Vertex Slide，松开确认。拖的是 gizmo，不是中键。
- **Ctrl+中键**沿法线缩放（Shrink/Fatten），松开确认。不依赖当前是哪个工具。
- **Shift+V** 开始顶点/边滑动。先尝试 Edge Slide，不行再 Vertex Slide。变换模态里原来的 `VERT_EDGE_SLIDE` 快捷键已去掉，只保留这一组 **Shift+V**。

- 网格编辑模式下，**Delete** 按当前点、边、面选择模式直接溶解所选几何（`mesh.dissolve_mode`），**Backspace** 打开原生删除菜单。这两个键位由 XM 预设提供，无需 Interactive Tools 插件。
- **Y** 切换到 Target Weld，**S** 调出 Loop Cut 工具组。工具组快捷键默认返回上次使用的子工具，可在 XM 预设中关闭 **Remember Last Tool in Group**。
- **A** 在物体模式打开 Apply 菜单，在网格编辑模式应用物体变换；**Alt+F** 显示全部物体。**4** 在物体与编辑模式间切换。
- 在网格或 UV 编辑中，**C** 标记接缝，**Shift+C** 清除接缝。XM 会处理 X-Ray Selection Tools 圆选快捷键对这两个键位的占用。

已经加载过的用户键位是一份拷贝。改完后要在偏好设置里重新选择 XM KeyMapping，新快捷键才会写进去。

默认 Blender 键位没有把 D 做成 Annotate 工具切换，D 仍是 Annotate 笔画的修饰键。3D 视图里额外绑了 `view3d.gizmo_pivot_edit`（D 按下和松开，`any`），只刷新视图并继续传递事件，所以点中 gizmo 时枢轴编辑优先生效，Annotate 笔画仍然可用。

## 穿透选择与镜像边标记

网格编辑模式下，框选、圈选和套索工具的顶栏提供 **Select Through**。开启后，拖选期间临时显示 X-Ray，可以选到被遮挡的几何；确认或取消后恢复之前的视口显示。关闭 **Select Backfacing** 可在穿透选择时排除背向元素；手动隐藏的几何仍跳过。

圈选单独保存 **Select Through**，框选和套索沿用原来的设置；**Select Backfacing** 仍由三个工具共用。打开旧文件时，圈选继承文件里原有的穿透状态，之后可以分别调整并保存。例如，可以用框选选穿模型，再用圈选只选可见表面。

网格编辑对称也适用于标记和清除 **Seam**、**Sharp**。启用多个对称轴时，可见的镜像边会一起更新。

## 临时工作平面

在网格编辑模式下选中活动面，打开 3D 视图侧栏 **View → Temporary World Coordinates → Set Workplane from Face**。工作平面以面中心为原点、面法线为 Z 轴，视口网格、导航和新建物体对齐到这一平面。**View Workplane from Above** 切到工作平面顶视图，**Restore Defaults** 恢复进入前的视图、游标、变换朝向和枢轴设置。

## 默认挤出

网格编辑的挤出工具组第一项是 **Extrude**（`builtin.extrude_outset`），也是默认子工具。它是 OmniOutset（ZXY，GPL-3.0-or-later）的内置移植，逻辑仍在 `mesh.omni_outset`。

`mesh.omni_outset` 按当前选择模式分流：

- 面选择：沿各面法线挤出，侧壁间距均匀（`mesh.omni_outset_face`）。
- 边选择：边等距向外挤出（`mesh.omni_outset_edge`）。
- 顶点模式中，完整选中的面或边也能走对应的挤出；只剩孤立选点时退回 `view3d.edit_mesh_extrude_move_normal`。

工具栏的 **Extrude** 使用原生变换手柄。拖动手柄先创建挤出几何，再按手柄约束平移；启用网格对称时会处理镜像侧。由菜单或快捷键调用的 Smart Face / Edge Extrude 还可调节偏移、分段和锥度。

默认键位的 **E**，以及 Industry Compatible 的 **Ctrl+E**，都指向这个挤出。Alt+E 挤出菜单、边菜单和面菜单的第一项也是它。原来的区域挤出还在工具组里，标签是 **Extrude Region**。

## 物体模式下的 Gizmo 位置

物体模式的视口顶栏里，变换枢轴左边有一个边界框开关（`tool_settings.use_gizmo_object_center`）。

- 关掉时，gizmo 仍画在当前枢轴上。
- 打开时，物体模式的移动、旋转、缩放 gizmo 画在所选物体几何包围盒的中心。没有包围盒的物体用原点参与计算。多个物体取合并后的包围盒中心。用 gizmo 旋转或缩放时绕这个中心。
- **G / R / S** 仍使用旁边的变换枢轴，不跟这个开关走。
- 按住 **D** 确认过的自定义枢轴优先于这个开关。

开关只在物体模式显示。标志位是 `ToolSettings.transform_flag` 的 `SCE_XFORM_GIZMO_OBJECT_CENTER`，默认关。

## UV 工具面板

Properties 编辑器新增 **UV Tools** 上下文，提供 Seam、Unwrap 和 Texel Packing 模块。操作按钮使用大号方形彩色图标；`UILayout.operator()` 增加独立的 `icon_scale` 参数，默认值为 1，原有按钮大小不变。

- **Mark Seam / Stitch UVs**：标记接缝、按当前顶点或边选择模式缝合；UV 已连续时清除选中接缝，没有可缝合的边界时提示。支持撤销、重做。
- **Unwrap**：调用 Blender 原生展开操作。UV Tools 提供 Texel Packing 和 Pack to Others，原生 Pack Islands 保留在 UV 菜单中。
- **诊断显示**：入口位于 UV 编辑器顶栏，不在 UV Tools 中重复显示。UV 与模型同步着色，底部统计按模型表面积加权。
  - **Distortion（默认）**：`sqrt((面 UV 面积 / 面 3D 面积) / (岛 UV 总面积 / 岛 3D 总面积))`。先汇总原始多边形的三角形面积，按 UV 连通岛归一化。1 为中性，红色相对压缩，蓝色相对膨胀；整体等比缩放不改变结果。该公式与本机 RizomUV 2027 的九组平面实验匹配，但不宣称覆盖其所有退化或非平面处理细节。
  - **Shape Stretch**：雅可比最大/最小主伸缩比，1 为保形，灰→黄→红表示方向性畸变增强；能发现面积不变的非等比拉伸，不检测镜像方向。
  - **Density**：相对参与统计的网格总面积比，反映 UV 面积分配，不是相对于目标 px/m 的绝对密度。
- 热图连续填色后单独绘制细线框。编辑模式的交互变换缓存拓扑、局部更新受影响三角形及 GPU 块；岛归一化因子通过 GPU 纹理更新；UV 连通关系改变时重建岛划分，保留未受影响的绘制块。坐标变化检测仍需扫描，不能保证任意规模模型的固定帧率。
- Sync Selection 使用 UV 编辑器原生入口，不在 UV Tools 内重复放置。
- UV 顶栏和选择菜单提供点、边、面、岛四个互斥模式。默认与工业兼容（含 XM）键位的 1/2/3/4 统一调用 `uv_tools.select_mode`；切到点、边、面时清除岛选择标记，同步模式同时更新网格和 UV 选择类型。启动时迁移旧 UV Editor 的无修饰键模式绑定，避免只改变 3D 模式。
- 打包算法的设计要点、原生实现现状与改进方向见 [doc/uv_pack_design.md](doc/uv_pack_design.md)。
- **Texel Packing**（`uv.texel_pack`）：按所选纹素密度策略安排 UV 岛。
  - 在纹素栅格上打包，间距、边缘留白都按像素计算，支持像素对齐。
  - TD 有三种模式：统一并填满、保持相对比例、指定 px/m；可设 TD 容差和逐岛密度权重。容差大于零时优先安排 Keep Scale 岛，其余岛再按容差缩小；没有额外新增 Mixed Scale 控件。
  - 自动堆叠相同的岛，可把副本偏移若干 tile 用于烘焙。
    - 镜像岛（包括对称网格的左右两半）可选，默认不堆叠。
    - 手动堆叠组总是允许镜像，形状容差至少 5%。
  - 支持分组聚集、每组一个 UDIM、多 UDIM 溢出，以及以锁定岛或未选中岛为障碍的增量打包。
  - 逐岛约束保存为 `uvpack_*` 面属性，UV 编辑器侧栏新增 **Texel Pack** 页：
    - **Island Constraints**：选中岛后直接修改旋转、翻转、保持缩放、密度和锁定，改动立即作用于整个岛，并可撤销。所选岛取值不一致时，值旁显示 ◆。
    - **Groups / Stacks**：分别列出正在使用的 id，显示颜色和面数；每行可选中对应的岛，或把所选岛加入该组。底部可新建 id 或把所选岛移出。
    - 两个面板标题上各有开关：打开后，在 UV 编辑器中用各自颜色的半透明包围框圈出每个分组（`G<id>`）或堆叠组（`S<id>`）并标注 id。
  - 以后台任务运行，有进度条，可按 Esc 取消，完成后整步撤销；最近一次报告显示在面板中。
  - 侧栏和叠加显示通过 `Mesh.texel_pack_info()` 只读取编辑网格，不在绘制时创建 Python `bmesh` 元素。创建这些元素会重新分配网格数据，在拖动 UV 时导致崩溃。
  - 测试命令：`blender --background --factory-startup --python-exit-code 1 --python tests/python/bl_uv_texel_pack.py`

### UV 编辑与打包的后续改动

- **Checker Preview** 在所选网格和 UV 画布上显示棋盘格。切换时保留原图、材质和视图取景；预览与诊断热图绘制在原生选择边线下方。
- UV 边、面模式下，追加环选保留已有选择，同步选择开关两种状态都支持。朝向工具可参照 UV 包围框、所选边或模型的 3D 方向。
- UV 快捷工具栏集中了接缝、缝合、展开和打包。变换工具支持横向或纵向定向、拉直、旋转、沿 U/V 翻转、矩形化（Rect），以及适配和对齐。Rect 可处理不规则岛、指定的四角和局部选择的点链、边链或边界环。
- **Heuristic Search** 先生成基准排布，再搜索满足相同约束的候选，视口实时显示当前最好结果。可设置总搜索时间和无改善时限；**Enter** 接受最好结果，**Esc** 恢复启动前的 UV。
- **Pack to Others** 以未选中或锁定的岛作为纹素密度参照和固定障碍，将选中岛排入剩余空间。已有布局可以作为新增岛的密度基准。
- 分组可命名、选择、隐藏或显示成员，并可按组分配同名材质。堆叠检测可在尚未分配的选中岛中找出多组匹配形状，为每组分别建立堆叠。

出厂的 **UV Editing** 工作区将 3D 视图放在左侧、UV 编辑器放在中间，右侧保留大纲视图，并在下方显示 UV Tools。该布局由启动默认值代码生成，普通 Layout 工作区保留完整的 Properties 标签。

## 快速晶格与 Apply All

3D 视图的物体右键菜单、网格编辑右键菜单新增 **Quick Lattice**（`object.quick_lattice`）：

- 物体模式支持多个选中 Mesh，共用一个晶格；网格编辑模式支持多物体的选中顶点、边、面。
- 按目标的共同世界坐标包围盒拟合。物体模式考虑求值后的几何；编辑模式按原始选中顶点计算，并将晶格修改器放到栈首。平面、线和单点选择会补上最小厚度，避免零尺寸。
- 复用原生 `object.lattice_add_to_selected` 创建晶格、初始化分辨率和添加修改器，不生成 Empty、Hook 或额外控制器。创建后直接进入原生晶格编辑模式并选中控制点；默认分辨率为 2×2×2。
- 编辑模式自动创建 `Quick Lattice` 顶点组，权重为 1，只影响选中部分。共享网格数据在需要写入权重时复制，避免影响未选中的关联实例。物体模式不创建限制组。

活动对象为晶格时，物体右键菜单和晶格编辑右键菜单提供 **Apply All**（`object.lattice_apply_all`）：

- 扫描整个文件，包括未选中、隐藏和其他场景的对象，应用所有引用该晶格的 Lattice 修改器，再删除该晶格；其它修改器保留。
- 先在对象与数据副本上尝试应用，全部成功后才写回。非可编辑对象、带 Shape Keys 等原生不能应用的情况会报错并保留原状态，不留下部分应用结果。
- 成功后清理本次修改器使用的 `Quick Lattice` / `Quick Lattice.数字` 顶点组。仍被其它修改器、约束、粒子系统等引用的组保留；存在 Geometry Nodes 时保守保留。用户改名的组不按自动组清理。
- 晶格若有子物体，删除前解除父子关系并保持其世界变换。创建与 Apply All 都支持整步撤销、重做；撤销 Apply All 会恢复晶格、修改器和已清理的顶点组。

不依赖 Interactive Tools 插件。本机对该插件的禁用与移出加载目录属于用户配置，不会由本次 Git 提交自动修改其他机器的插件。

自动化测试位于 `tests/python/bl_quick_lattice.py`，注册为 `script_quick_lattice`。已验证包围盒、不同分辨率、多物体顶点/边/面选择隔离、共享网格保护、隐藏/跨场景目标、多个修改器引用、失败保护和顶点组清理；真实窗口另行验证了撤销与重做。

```sh
blender --background --factory-startup --python-exit-code 1 --python tests/python/bl_quick_lattice.py
```

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
- 镜像在拖动开始时按每个打开的轴调用 `EDBM_verts_mirror_cache_begin`（含自身、不限选择、尊重隐藏，拓扑镜像看 `ME_EDIT_MIRROR_TOPO`），把结果存成顶点下标，鼠标移动时不再重建 KD 树。松开时先展开配对再退出模态，因为退出会释放操作符数据。`weld_verts` 的 map 不能让一个顶点既是源又是目标；多个源可以共用一个目标。预览的边线只画真实的边，不能把点列表的前两个点连起来，否则会把顶点画成它和镜像之间的一条边。
- Knife 对称在工具开始时用同样的镜像缓存，按轴建好后立刻 `cache_end`，一次只能留一层。原刀写完交点上的 KnifeVert 之后，对 mask `1 .. (1 << 轴数) - 1` 依次翻转局部坐标再切。解析顺序是已有 BMVert、已有 BMEdge、面、本刀新顶点、还没有 BMEdge 的 KnifeEdge。镜像交点先按 `linehit_compare` 排好再交给 `knife_apply_linehits`，后者不再排序，否则和源顶点的平行下标会对错。整刀都在平面上时跳过切割，但把源 KnifeVert 映射到自己。预览的 cage 是世界空间，先变到物体局部再翻轴。镜像切割和原刀记在同一次撤销里。
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
- `source/blender/editors/mesh/editmesh_knife.cc`
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

快速晶格相关文件：

- `scripts/startup/bl_operators/object_quick_lattice.py`
- `scripts/startup/bl_ui/space_view3d.py`
- `tests/python/bl_quick_lattice.py`
- `tests/python/CMakeLists.txt`

## 回归测试与历史索引

- 穿透选择的 GPU 回归见 `tests/python/view3d_select_through.py`。它在独立 Blender 窗口中将圈选和框选设为相反状态，用前后重叠的面检查透视和正交视图中的点、边、面选择。
- UV 诊断、预览、变换、打包的回归分别见 `tests/python/bl_uv_tools.py`、`bl_uv_preview_render.py`、`bl_uv_transform.py` 和 `bl_uv_texel_pack.py`。参考密度打包和堆叠检测分别见同目录下的 `bl_uv_pack_to_others.py` 和 `bl_uv_stack_detect.py`。

已有提交可按以下顺序追溯：

- `69550036a16` 智能挤出、对称和原生手柄，XM 记住工具组选择；`f2c838f2366` 穿透选择、镜像边标记和 XM 编辑快捷键；`de6c2c4bbf4` 临时工作平面。
- `7da2c6887b3` UV Tools 和增量畸变显示；`9df416d9a36` 快速晶格；`60cf6df1283` Texel Packing 和逐岛约束；`fd80b9b7156` 互斥 UV 选择模式、诊断与固定缩放岛优先打包。
- `a4740c9527a` 棋盘格预览与原生叠加层；`8c3ed847eae` 启发式排布搜索、UV 快捷工具栏、变换与分组管理；`a9052e72015` 参考密度打包和多组堆叠检测；`85df77dfe0b` U/V 翻转和 Rect 局部选择。

## 骨骼批量命名

Edit Mode 的 **Armature → Names → Batch Rename Bones** 和 Pose Mode 的 **Pose → Names** 可为选中骨骼按深度优先顺序编号。XBone 的编辑菜单与右键菜单直接提供该命令。输入基础名 `Arm`，可生成 `Arm_001`、`Arm_002`，或选用侧别得到 `Arm_001_L`、`Arm_002_L`；`_L` 与 `_R` 可用于骨架对称化和 Skin Tools 权重镜像。父骨先于子骨，同级依骨架已有顺序；遍历经过未选骨骼时只给选中项编号。多骨架选择从活动骨架开始连续编号。目标名被未选骨骼或其他变形组占用时可换一个基础名重试。

## 分层权重绘制

**任务进度（2026-11-20）**　V1、V2 和姿势往返改动已随 `b3edc8c48923` 合入 `main`。后续开发从主干继续。需求来源、合成公式、分期计划及实测工况见[专题设计与验收记录](doc/layered_weight_paint.md)。

### 已交付

在 **Weight Paint** 工作区选中带一个 Armature 修改器的网格，右下角的 **Weight Layers** 编辑器提供 **Create Layers from Weights**。它把当前变形组复制到 Base。其他区域也可以从编辑器类型菜单切换成 Weight Layers。

- **工作区入口**。普通启动和新建 General 时，个人启动布局缺少 Weight Paint 会自动补上，保留原来的活动工作区。旧项目使用文件里保存的标签，可从顶部 **+ → General → Weight Paint** 添加。
- 在 3D 视图标题栏用 **Native / Skin Tools** 切换权重来源。进入姿态模式、播放或渲染时，Armature 继续使用对象最后选择的来源。
- 点 **+** 建修正层，选择影响骨后绘制。**Mask** 可用同一套画笔绘制逐顶点遮罩，黑色露出下层，白色保留本层。各层可调序、复制、开关和调整不透明度。
- Skin Tools 笔刷强度默认 **1.0**。**Replace / Add / Scale / Smooth** 支持表面笔画、顶点或面选区与 Flood。Shift 落笔临时平滑。**Display** 可选择本层、灰度遮罩或最终权重。
- **Bake to Vertex Groups** 把合成结果写回变形组，保留层栈并支持撤销。共享网格需要先转为单用户才能烘焙。原始顶点对应关系失效时，恢复拓扑后可继续绘制。
- **图层树**。支持嵌套组、子层、移动子树和同级向下合并。普通层点 **Add Child** 会原位包进新组。子层先合成，组遮罩与不透明度只对合成结果应用一次。
- **选骨与显示**。当前影响骨以黑白显示权重，其他骨骼按稳定通道标识着色。点列表会高亮视口骨骼。绿色骨形图标表示层内至少一个顶点在该骨上有正权重。**Influenced Only** 可叠加名称搜索，刷黑遮罩仍保留已画骨骼的绿标。
- **赋权与镜像**。支持从表面取样主导骨骼、按最近静止关节赋权，以及本地 X 轴的一次性镜像和随笔镜像。顶点按静止几何双向配对，影响骨沿用 Blender 左右命名。随笔镜像直接使用 3D 视图工具标题栏的 X 开关。Weight Layers 的 **Mirror** 可独立执行：**Current Bone** 根据绑定姿势将当前骨骼的权重复制到对侧骨骼；**All Bones** 可选择左到右（+X 到 −X）或右到左。遮罩及没有明确左右位置的骨骼可手选方向。
- **摆姿势后接着画**。视图顶栏的 **Pose Rig** 进入绑定骨架的 Pose Mode，**Back to Weight Paint** 带着新姿势返回发起切换的网格、图层和影响骨。返回目标按 View Layer 记录，同一骨架绑定多个网格时也能找回原对象。**Bones In Front** 和 **Reset Pose** 位于 3D 视图的 **N → Weight Paint → Rig**，仅在 Weight Paint 模式显示。重置一次性清除局部姿态变换，随后可立即摆姿势，动画和约束继续求值。

### 从哪里接手

- 改图层混合或笔画时，同时核对 `compose_stack` 的整栈求值与 `SkinBrush` 的局部更新。对象持有层树，网格点属性保存各层权重、遮罩和合成通道。两条路径在父组半透明、遮罩和层调序后应给出相同结果。[图层与画笔](scripts/startup/bl_operators/skin_layers.py)
- 改选骨、镜像或模式切换时，从工具操作符入手。编辑器列表和 N 侧栏在同一个 UI 模块，模式往返按钮在 3D 视图标题栏。[工具操作符](scripts/startup/bl_operators/skin_layers_tools.py) · [Weight Layers 与侧栏](scripts/startup/bl_ui/space_weight_layers.py) · [视图标题栏](scripts/startup/bl_ui/space_view3d.py)
- 排查“颜色正确但变形不对”时，检查 Armature 修改器选择的权重来源、稳定顶点组标识及原始顶点对应。视口颜色有独立的读取路径。[修改器入口](source/blender/modifiers/intern/MOD_armature.cc) · [骨骼变形求值](source/blender/blenkernel/intern/armature_deform.cc) · [视口权重与颜色](source/blender/draw/intern/mesh_extractors/extract_mesh_vbo_weights.cc)
- 排查新建文件缺标签时，分别测出厂设置、已有个人启动文件和新建 General。布局在 `release/datafiles/startup.blend`，个人布局补齐逻辑在 `wm_files.cc`。公开 fork 限制上传新 LFS 对象，因此这个定制启动文件通过 `.gitattributes` 的路径例外直接存入 Git。[启动加载逻辑](source/blender/windowmanager/intern/wm_files.cc) · [跟踪规则](.gitattributes)

### 如何验证

从仓库根目录执行，命令中的 `blender` 指本 fork 构建的可执行文件。

```sh
blender --background --factory-startup --python-exit-code 1 --python tests/python/bl_skin_layers.py
blender --background --factory-startup --python-exit-code 1 --python tests/python/bl_skin_layers_v2.py
```

- 第一组覆盖工作区、个人启动布局、原生与分层权重隔离、编辑与渲染求值、烘焙和文件重开。第二组覆盖组遮罩与增量合成、影响骨、镜像、姿态重置，以及两个网格共用骨架时改名、保存重开并返回原网格。两组均已通过。[V1 回归](tests/python/bl_skin_layers.py) · [V2 回归](tests/python/bl_skin_layers_v2.py)
- 窗口验证已覆盖实际笔画和镜像笔画的撤销／重做，以及顶栏两个按钮的往返。复测时，选一个修正层画一笔，点 **Pose Rig** 转动前臂，再点 **Back to Weight Paint**。检查新姿势仍在、原层和画笔仍可用，以及 **Rig** 侧栏只在绘制模式出现。

### 后续任务

当前实现针对单个 Armature 修改器；多骨架绑定、跨拓扑重映射需要单独设计。原计划的第三阶段候选包括可开关的镜像效果与映射编辑、体积平滑／锐化、输出影响骨数限制与小权重剪枝、权重剪贴板、跨网格传层及交换格式。逐顶点 DQ 混合仍需先确定与 Blender **Preserve Volume** 的对应语义。以上均待排期。
