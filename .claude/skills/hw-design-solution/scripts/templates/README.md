> scripts/templates：各文档生成脚本的架构参考模板——为新项目写生成脚本时按此架构组织。

# scripts/templates · 生成脚本架构参考

SKILL.md 第六节定位：都是数据驱动架构（块注册表/行注册表是唯一数据源，改数据不重写逻辑）。为新项目写生成脚本时按此架构组织，把其中的项目数据换成新项目的。

## 一、脚本清单

| 脚本 | 一句话 | 输出物 | 对应产出物 |
|---|---|---|---|
| `_gen_key_device_list.py` | ①关键器件清单生成器：ITEMS 注册表驱动，5 列定稿版式（深蓝表头/斑马纹/冻结表头/关网格线） | 关键器件清单 xlsx | ① |
| `_gen_power_calc_xlsx_v5.py` | ③电源计算表生成器：RAILS（轨定义）/ LOADS（轨→芯片明细+手册出处）/ MATRIX（呈现矩阵）注册表驱动，折算公式单格单逻辑、typ/max 双块全 SUM 公式链 | 电源计算表 xlsx | ③ |
| `gen_block_diagram.py` | ②整体硬件方案框图生成器：块注册表 B() 唯一坐标来源 + 端点引用解析 P() + 三层输出顺序（zone→edge→节点/文字）+ fit_lines 自适应 | 系统框图 drawio + PNG | ② |
| `_gen_power_tree_drawio.py` | ③+电源树生成器：轨拓扑注册表驱动，全垂直布局分级出图（L1 总览 / L2、L3 板级） | 电源树 drawio + PNG | ③+ |
| `gen_interface_xlsx.py` | ④接口定义表生成器：pin 重排公式 + 接插件数据结构，每接插件一个 sheet | 接口定义表 xlsx | ④ |

## 二、运行方式

```powershell
python <脚本>.py
```

- 输出目录默认 `D:\python_project\20_SOL`；可用环境变量 **`HW_DESIGN_OUT`** 覆盖，目录不存在时自动创建，便于跨机直接运行：

```powershell
$env:HW_DESIGN_OUT = "D:\your\out"
python _gen_key_device_list.py
```

- 脚本内实现口径（现存模板同款）：`OUT_DIR = os.environ.get("HW_DESIGN_OUT") or r"D:\python_project\20_SOL"` → `os.makedirs(OUT_DIR, exist_ok=True)`；新脚本沿用此两行；
- drawio 脚本产出 `.drawio` 后，PNG 导出走 drawio CLI——四个坑（管道挂死 / 大图视口截断 / 导出竞态 / 相对路径静默失败）见 02-block-diagram.md 第 6 节。

## 三、数据驱动架构

**注册表是唯一数据源，改数据不重写逻辑。** 各脚本的注册表：

| 注册表 | 所在脚本 | 装什么 |
|---|---|---|
| `ITEMS` | _gen_key_device_list.py | 清单行：器件/型号/厂家/单机用量 |
| `RAILS` | _gen_power_calc_xlsx_v5.py | 轨定义：轨号/轨名/电压/电源芯片/口径一句话 |
| `LOADS` | _gen_power_calc_xlsx_v5.py | 轨→芯片明细：每条负载的 typ/max 与手册出处详注 |
| `MATRIX` | _gen_power_calc_xlsx_v5.py | 表格呈现矩阵：行怎么排、格怎么填 |
| `B()` / `P()` | gen_block_diagram.py | 块注册表（name, x, y, w, h——唯一坐标来源）/ 端点引用解析（side+pos），挪块联动机制 |
| 轨拓扑 | _gen_power_tree_drawio.py | 轨的级联关系与负载归属，驱动全垂直布局与共干分叉 |

要点：

- 坐标、端点、标签全部从注册表推导——**严禁手写绝对坐标**（02 第 2 节架构铁律）；
- 数据与版式分离：换项目只换数据块，版式与公式逻辑不动。

## 四、新项目适配四步

1. **换数据块**：把注册表里的项目数据换成新项目的（清单行 / 轨定义 / 块布局 / 接插件 pin 表）；
2. **跑脚本**：`python <脚本>.py` 生成产出物；
3. **自检 drawio**：用 `scripts/checks/` 工具链对生成的 drawio 跑几何检测，用法与豁免规则见 02-block-diagram.md 第 7 节；
4. **人工核对数值三源**：逐数值核对来源——芯片手册（记章节表号）/ 设计指南/参考设计文档 / 用户确认记录，三者都不占 → 标"待确认/待打合"列入确认清单（SKILL.md 第四节纪律 1）；再过交付前三道闸收工。

## 五、依赖

| 依赖 | 适用 | 说明 |
|---|---|---|
| Python 3 | 全部脚本 | — |
| openpyxl | 表格类脚本（①③④） | xlsx 生成 |
| 仅标准库 | drawio 生成脚本（②③+） | 拼装 mxGraph XML，不需要第三方库 |
| drawio CLI | PNG 导出 | 桌面版 draw.io 自带，导出纪律见 02 第 6 节 |
