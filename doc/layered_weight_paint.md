# xBlender 分层权重绘制 · 调研与首版设计

2026-11-20。专题分支 `feature/layered-weight-paint`，基线 `main@de9283f87f8`。本文是依据官方文档和本地源码提出的实现计划。交互尚未在 Maya 2027 或 xBlender 原型中实测。

## 从肩部修形开始

角色已有整套蒙皮。绑定师在屈臂姿势下新建「肩部修正」，画锁骨和上臂的权重，在腋下刷黑遮罩，再拖动不透明度比较两个方案。关掉这一层，原先的肩部立即可见。回到原生画笔还能独立修改原稿；切回 Skin Tools 可以接着修上一笔。把这个例子完整走通，作为第一版的交付边界。

## 参考工具有哪些能力

ngSkinTools 官网确认 Autodesk 收购了其技术与知识产权。以下以 Maya 2027 手册说明集成版行为，以 ngSkinTools v2 作者文档补充原插件的实现语义；早期手册涉及的透明度规则单独标明。[ngSkinTools 公告](https://www.ngskintools.com/) · [Maya 2027 Skin Tools](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-70ED2FB6-10FB-4481-BDC1-B0C34FAC769F.htm)

1. **初始化和归属。** 从已绑定网格开始，已有蒙皮作为起点。Maya 可以在绑定时初始化，也可以事后初始化；同一网格的多个 skinCluster 可选其一。删除分层数据可保留已经算出的权重，重置到默认权重则有单独的流程。已有层时，经典画笔、原生镜像等直接写权重的命令受到限制。[初始化](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-250D7C1A-8F25-4B2E-9492-9A1F93B72FAA.htm) · [工具兼容性](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-B5E373C5-EDF1-4A03-B9E3-375CB6EFEA3F.htm)
2. **层栈和分组。** 每层可以容纳多根骨骼。活动层接收绘制；从下向上合成，上层优先。支持开关、调序、不透明度、复制、删除、合并与填充透明区。组可以含多个子层，共用一张遮罩，适合把手掌和手指作为整体控制。[图层操作](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-AD7FB9C2-20EB-475E-973A-C97DB81C8358.htm) · [层与组](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-7EFA9498-1E66-416B-8652-9BBC84042220.htm)
3. **覆盖量、遮罩和不透明度。** 原插件早期手册明确区分了三者。一个顶点在层里的骨骼权重和可以小于 1，剩余份额透出下层；Mask 是独立的逐顶点乘数；Opacity 控制整层。刷黑遮罩后还能刷白找回原先分布。Maya 2027 另提供把削减的权重重新分给其他影响骨的选项。设计时要明确采用哪种行为。[原插件透明度规则](https://www.ngskintools.com/documentation/userguide/layers/) · [2027 画笔设置](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-48D3C7E7-A036-45B3-B4D5-7028C1903753.htm)
4. **画笔与命中范围。** Replace 逼近指定数值，Add 增加，Scale 乘以系数，Smooth 从邻域平滑权重，Sharpen 收紧过渡。Surface 适合沿可见表面画，Volume 可以跨分离的壳，Screen 可以穿透前后面。半径、衰减、强度、迭代及压感分别控制影响范围和每次改变量。可临时切平滑、反转画笔、按选区隔离。[画笔行为](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-F3C455D3-7345-45BB-A1F3-1E9B9224B3C5.htm) · [投射与压感](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-48D3C7E7-A036-45B3-B4D5-7028C1903753.htm)
5. **影响骨与反馈。** 骨骼列表支持搜索、仅看本层用到的骨骼、锁定选中或未选中的骨骼、选择受影响顶点，以及权重剪切、复制、替换／加／减粘贴。v2 可从表面取样主导骨骼，也可指向关节选择。可显示单骨灰阶、单骨色带、多骨配色、遮罩或原始网格。不同视图回答的是「我在改什么」和「最终是什么」。[影响骨](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-FD837719-AEE0-4D24-9CB0-4779D978BB17.htm) · [v2 视口工具](https://www.ngskintools.com/v2/features/) · [显示设置](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-48D3C7E7-A036-45B3-B4D5-7028C1903753.htm)
6. **批量赋值和清理。** Flood 将当前操作应用于选区或全网格。Set Weights 可设置迭代、每点影响骨数、权重向新骨骼扩散的范围、体积平滑。工具还包括按最近关节初始化所选骨骼、按阈值剪小权重、Unify、填充透明区、复制组件权重和粘贴平均值。Component Editor 可按层精确编辑和锁定。输出阶段还能单独限制影响数、剔除小权重，让作者保留高精度层数据。[批量操作](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-29BB61CC-8399-4E16-B71C-6E67EA1BEF1D.htm) · [赋值工具](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-2C449A92-0BDA-443B-8EFC-45DC7942B32F.htm) · [组件编辑](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-6E1C025E-E0B0-4362-8FB2-E2D0219D2498.htm) · [输出效果](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-65EC54A8-38C9-46BE-9AA2-44366EB420D8.htm)
7. **镜像有三种用途。** 交互镜像同步写对侧，一次性镜像复制或翻转选层，镜像效果在求值时产生另一侧，可以事后改方向。顶点对应和骨骼配对分别配置。Maya 支持 XYZ、指定方向或按笔画判断、接缝宽度、最近表面或 UV 映射，也可提供专门的对称参考网格，减少眼睑、嘴唇的错配。权重、遮罩和 DQ 双四元数混合权重可分别镜像。[三种镜像](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-9DC7CC54-5506-4358-8143-BDC27840423E.htm) · [映射设置](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-3F111981-48DB-4232-AF65-B0AD8CFB3418.htm)
8. **迁移和管线。** Maya 支持导入、导出和跨目标传层；ngSkinTools2 提供 Python 层 API、读写权重和 Mask/DQ 通道、JSON 导入导出及跨网格转移。跨网格需分别解决顶点对应和骨骼对应。v2 以 skinCluster 输入输出为基准，避免拿下游细分后的顶点编号编辑上游权重。[Maya 菜单](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-C048A881-F289-4335-A846-49E1CF2A1B99.htm) · [v2 图层 API](https://www.ngskintools.com/documentation/v2/api/layers/) · [传递 API](https://www.ngskintools.com/documentation/v2/api/transfer/) · [v2 的拓扑处理](https://www.ngskintools.com/documentation/v2/userguide/version2-whats-new/)

## 第一版的界面和数据约定

- **出厂工作区 Weight Paint。** 按参考图，左约 82% 为贯穿全高的 3D 视图，底部保留视图自带的笔刷资产架；右栏约 18%，上约五分之一为 Outliner，余下为 Weight Layers。比例是初始值，可拖动。新建 General 文件即可看到工作区，已有文件可从添加工作区菜单引入。有可绘制活动网格时进入 Weight Paint，无可用网格时显示选择提示。
- **Weight Layers 是原生区域编辑器。** 在任意区域的 Editor Type 菜单可选，标题栏放到底部，左下角保留编辑器类型选择器。编辑器跟随活动对象，上部为可调序图层和不透明度，下部为可搜索的影响骨列表；Mask 作为单独绘制目标。未初始化显示「从现有权重创建图层」。切回 Native 时仍可看到层栈状态。
- **绘制模式内的切换。** 3D 视图 Weight Paint 标题栏提供 Native / Skin Tools。第一次初始化从当前骨骼变形组复制一份 Base，新增修正层全透明。Native 笔画只编辑原生组，Skin Tools 笔画只编辑当前层／遮罩。两边分别记住笔刷和目标。切换立刻显示该来源的权重和变形。退出绘制模式后，姿态、动画和渲染继续用该对象最后选定的来源。再次打开文件恢复选择。普通文件默认 Native。
- **一个明确的数据交换动作。** “Bake to Vertex Groups”将合成结果写回对应的原生变形组，保留层栈，可撤销。若初始化后又画过 Native，烘焙前显示覆盖提示。布料、选择集和非骨骼用途的其他组保留。首版按单个 Mesh 对象、一个明确的 Armature 目标和可对应的原始拓扑设计；多 Armature 或映射不明时给出具体原因，避免猜选。已锁定的变形组在可能被烘焙修改时要指出冲突。
- **能完成日常修形的绘制工具。** 新建、重命名、复制、删除、上下移动、开关层、Opacity；可绘权重及灰度 Mask。首版实现 Surface 的 Replace、Add、Scale、Smooth，支持调半径／强度、选区限制和 Flood。权重 Smooth 同步处理邻域的本层骨骼分布，遮罩 Smooth 只处理标量。视口可切「本层」「遮罩」「最终」，图层编辑器持续显示本次落笔的目标。

### 一个可验证的合成约定

对每个顶点，从底向上合成。设下层结果为 B，本层各骨骼的原始权重为 L，总和为 s，k 为图层不透明度乘以该顶点的遮罩。

    s = 0: result = B
    s > 0: result = (1 − k) × B + k × (L / s)

例如下层完全属于大腿，上层只给小腿画了 0.6。白色遮罩、100% 不透明度时，该顶点完全跟随小腿。若要膝盖渐变，可绘制半灰遮罩，让上下层各贡献 50%，也可以在同一层给两根骨骼分配权重。**Layer Only** 默认显示本层原始笔画，**Stack Result** 显示用于蒙皮的合成权重。组的遮罩和不透明度在子层合成后生效。

## 工程顺序与验收

1. **先验证数据隔离和变形。** 当前 `editors/sculpt_paint/mesh/paint_weight.cc` 直接请求 `mesh.deform_verts_for_write()`；Armature 经 `modifiers/intern/MOD_armature.cc` 进入 `blenkernel/intern/armature_deform.cc` 读取变形权重。先做两骨网格和两套明显不同的权重，只在选定 Armature 的求值输入上接分层结果。用同一姿势比较实际顶点坐标，验证编辑、对象、渲染求值一致，并核对原始 Mesh 权重。评估网格若发生拓扑变化，必须有可靠的原始索引映射才能按原点绘制。
2. **实现存储与合成。** 以对象的蒙皮目标持有独立层栈，骨骼／顶点组引用须能跟踪重命名和重排，不能长期保存裸组序号。层保存稀疏非零权重和可选 Mask，缓存不写入文件。划定支持的拓扑编辑，失去对应时暂停分层求值并保留数据。实现版本兼容、复制、撤销／重做、局部失效及显式烘焙。共享 Mesh 的对象需分别验证预览隔离；烘焙时明确处理多用户写入。用固定权重向量测空层、黑白灰遮罩、非满覆盖、开关及调序。
3. **打通视口、编辑器与工作区。** 在 Weight Paint 内分派画笔和批量命令，复用现有笔画采样、压力、选区与撤销框架；逐项防止原生命令从其他入口绕过来源选择。`draw/intern/mesh_extractors/extract_mesh_vbo_weights.cc` 是权重着色的接入点。注册新的 SpaceType、DNA、RNA、区域和菜单项，入口参考 `editors/space_api/spacetypes.cc`。在出厂 `release/datafiles/startup.blend` 提供工作区，参考 `blenloader/intern/versioning_defaults.cc` 与 `tests/python/bl_uv_workspace_defaults.py` 核验区域类型、方向和位置。
4. **按绑定师操作验收。** 在弯肘角色上改上臂、画半灰遮罩、关闭再打开层、切两套工具分别落笔。逐次对比两套数据、视口颜色和骨骼变形。覆盖切对象、一步一笔 Undo/Redo、保存重开、再次烘焙、非变形组、骨骼改名、共享网格和拓扑失配。工厂启动检查截图布局，旧文件检查默认来源。用 10 万顶点／100 根骨／8 层同一资产记录局部笔画与整层调透明度的耗时、P95 及附加内存，和同机 Native 对照；这些是拟测工况。

第二阶段优先补层组／组遮罩、向下合并、按最近关节初始化、单次和交互镜像、表面取样骨骼。第三阶段再做镜像效果与映射编辑、体积平滑与锐化、输出影响数与剪枝、权重剪贴板、跨网格传层和交换格式。逐点 DQ 混合需先确认和 Blender Armature 的 Preserve Volume 的语义对应，单列设计。

## 首版实装与实测

本分支已经实现上述分层绘制流程。图层顺序、笔刷、目标和来源由对象持有；权重与遮罩用网格上的点域属性保存，Armature 通过稳定的顶点组标识读取合成结果。层内按用到的骨骼分配通道，每个已分配通道覆盖全部顶点。合成通道也随文件保存。链接复制的对象在修改同一份图层数据时分离各自的存储。原生骨骼组的数值只在显式烘焙时写入。

Windows Release 已验证默认工作区、右下编辑器的底部选择器、默认和 XM 键位下的实际鼠标笔画、撤销／重做、弯肘示例的权重和灰度遮罩。自动用例 `tests/python/bl_skin_layers.py` 检查对象／编辑／渲染求值、图层混合、选区画笔、共享网格、改骨名、拓扑失配、烘焙和文件重开。原有 UV 工作区的出厂布局回归也通过。

同机后台对 10 万顶点、100 根骨骼、8 层测得，笔画数据已准备好时，每次更新 64 个顶点并完成依赖图求值的中位数为 11.07 ms，P95 为 12.76 ms。整层调整不透明度的中位数为 49.95 ms，P95 为 55.31 ms。每项预热两次后采样 12 次，不含鼠标拾取和 GPU 刷新。相对初始化前，进程私有内存增加约 320 MiB，包含已分配的权重通道、合成结果、笔画缓存和依赖图数据。

## V2 实现计划与验收

1. **权重反馈与选骨。** 当前影响骨使用黑到白，其他影响骨按稳定通道标识分配色相，顶点按各骨骼权重加色。本层与最终预览分别读取当前层和合成结果；Mask 保持纯灰阶。骨骼列表以绿色骨形图标表示本层至少有一个顶点权重大于零，过滤可与名称搜索叠加。指标使用作者绘制值，调整不透明度或遮罩不会隐藏已画的骨骼。
2. **绑定姿势检查。** 点列表同步活动姿态骨，网格保持绘制对象。3D 视图的 N 侧栏在 Weight Paint 模式提供 Bones In Front 和 Reset Pose。重置会一次清除骨骼的局部姿态变换，骨架继续使用姿态求值，已有动画和约束仍可驱动骨骼。视图顶栏的 Pose Rig 进入绑定骨架的姿态模式，Back to Weight Paint 回到原网格继续画。
3. **图层树。** 普通层保存权重，组保存子层、遮罩和不透明度。先完成组内合成，再对组的总贡献应用一次遮罩。普通层上的 Add Child 会将原层原位包装成组，再加入空子层。支持嵌套、折叠、同级调序、复制／删除子树和同级向下合并。V1 图层按原顺序留在根级。[Maya 2027 层与组](https://help.autodesk.com/cloudhelp/2027/ENU/Maya-CharacterAnimation/files/GUID-7EFA9498-1E66-416B-8652-9BBC84042220.htm)
4. **补齐第二阶段工具。** 从网格表面取样主导骨骼；按绑定静止关节位置为全网格或点选区赋初值；在模型本地 X 轴提供一次性和随笔镜像。按静止几何建立双向配对，按 Blender 左右命名查找对侧骨，未配对顶点保留。顶栏 X 控制随笔镜像；面板 Mirror 按钮独立于此开关。Current Bone 从活动骨所在侧复制到配对骨，All Bones 按选定方向复制当前层的完整分布。单骨操作仅在目标点需要让出权重时，按比例压低其他通道。组遮罩和层遮罩只镜像灰度值。
5. **验收。** 用混权顶点验证切骨换色、灰度遮罩和原生色带，检查列表从零画出及清零后的绿标。验证父组半透明只应用一次、组遮罩笔画、合并前后最终变形一致，以及 V1 文件重开、编辑撤销和实际工作区显示。

V2 已用 V1 弯肘文件和左右骨测试网格验证。实际窗口覆盖切骨换色、活动骨高亮、前置显示、表面取样、静止姿势和组层；对称鼠标笔画用一次撤销同时恢复左右两侧。自动用例 `tests/python/bl_skin_layers_v2.py` 验证组遮罩、笔画增量合成、绿标清零、移动子层、向下合并、最近关节赋权、一次性镜像和嵌套文件重开。镜像当前支持本地 X 轴与 Blender 左右骨命名，中心点只绘制一次。
