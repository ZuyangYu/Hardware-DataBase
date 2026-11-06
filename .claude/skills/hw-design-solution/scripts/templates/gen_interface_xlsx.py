# -*- coding: utf-8 -*-
"""④ 接口定义表生成模板 v1 —— 一个接插件一个 sheet（规则见 references/04-interface.md）
数据驱动：CONNECTOR + 分组管脚定义=唯一数据源，改数据不重写逻辑。
- pin 序支持两种模式（必须与用户确认后选型）：sequential 上排 1~N → 下排 N+1~2N；interleave 奇偶交叉
- 差分对同排相邻、对间 GND、高速与低速之间 GND 隔离带；电源 pin 写过流数值，信号填"—"
- 版式引用 references/table-style.md（深蓝表头/斑马纹/D0D0D0 边框/freeze/隐藏网格线）
输出：接口定义表.xlsx（HW_DESIGN_OUT 可覆盖目录）
"""
import os

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT_DIR = os.environ.get("HW_DESIGN_OUT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, "接口定义表.xlsx")

HEADERS = ["接插件编号", "接插件名称", "Pin编号", "Pin定义（网络名）", "Pin作用", "过流要求"]

# ── pin 序排布 ──
def pin_map(count, mode="sequential"):
    """物理相邻序 index → (pin_no, 行号, 排位)。行号 0 起，同一行上下两 pin 正对。
    sequential：上排 1~N → 下排 N+1~2N（板对板常见，demo 默认）
    interleave：奇偶交叉，上排 1,3,5… 下排 2,4,6…（选用前必须与实物/规格书核对）
    """
    n = count // 2
    out = []
    if mode == "sequential":
        for i in range(n):
            out.append((i + 1, i, "上排"))
        for i in range(n):
            out.append((n + i + 1, i, "下排"))
    elif mode == "interleave":
        for i in range(n):
            out.append((2 * i + 1, i, "上排"))
            out.append((2 * i + 2, i, "下排"))
    else:
        raise ValueError(f"未知 pin 序模式：{mode}")
    return out


# ── demo 数据：J1 板对板 68pin ──
def _pins():
    """68pin 管脚序（物理相邻序=差分同排相邻+对间 GND；高速/低速之间 GND 隔离带）"""
    p = []

    def add(net, func, ocp="—"):
        p.append((net, func, ocp))

    def gnd(n=1, note="地"):
        for _ in range(n):
            add("GND", note)

    def diff(prefix, n):
        for i in range(1, n + 1):
            add(f"{prefix}{i}_P", f"{prefix} 差分+")
            add(f"{prefix}{i}_N", f"{prefix} 差分-")
            gnd(1, "差分对回流地")

    # 高速段
    diff("LVDS_TX", 4)                                   # 12
    gnd(2, "高速段收尾隔离")                              # 14
    for nm in ("RGMII_TXD0", "RGMII_TXD1", "RGMII_TXD2", "RGMII_TXD3"):
        add(nm, "RGMII 发送")
    add("RGMII_TX_CTL", "RGMII 发送使能")
    gnd(1)
    for nm in ("RGMII_RXD0", "RGMII_RXD1", "RGMII_RXD2", "RGMII_RXD3"):
        add(nm, "RGMII 接收")
    add("RGMII_RX_CTL", "RGMII 接收使能")
    add("RGMII_CLK", "RGMII 时钟")
    gnd(2, "高速段收尾隔离")                              # 28
    # 电源段
    for _ in range(4):
        add("VCC_3V3", "3.3V 电源", "≥2A/pin")
    gnd(4, "电源回流")
    add("VCC_1V8", "1.8V 电源", "≥1A/pin")
    add("VDD_CORE", "0.9V 核电", "≥3A/pin")               # 38
    # 低速段
    add("I2C_SCL", "调试 I2C 时钟")
    add("I2C_SDA", "调试 I2C 数据")
    gnd(1)
    add("UART_TX", "调试串口发送")
    add("UART_RX", "调试串口接收")
    gnd(1)
    add("SYS_RESET_N", "系统复位")
    add("PHY_INT_N", "中断")
    add("WAKE", "唤醒")
    gnd(1, "高低速隔离带")                                 # 48
    while len(p) < 64:
        add("GPIO", "预留 GPIO")
    gnd(4, "板对板回流地")                                 # 68
    return p


CONNECTOR = {"id": "J1", "name": "板对板连接器（公）68pin", "mode": "sequential", "pins": _pins()}

NOTES = [
    "备注：",
    "① pin 序排位：sequential（上排 1~34 → 下排 35~68，同列正对）；若连接器为奇偶交叉型，须先确认再改 arrange_pins(mode=\"interleave\")〔04 §一确认项〕",
    "② 差分对同排相邻、对间 GND；高速（LVDS/RGMII）与低速（I2C/UART/GPIO/控制）之间以 GND 隔离带分隔",
    "③ 过流要求三源：SRS 限流 / 接插件规格书 / 芯片手册（记章节表号）；电源 pin ≥2A/pin（9V 3A÷2pin 换算），信号填\"—\"",
]

# ── 版式常量（table-style.md 基准） ──
C_HDR = "1F4E79"
C_ZEBRA = "F5F8FC"
C_LINE = "D0D0D0"
thin = Side(style="thin", color=C_LINE)
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
HDR_FILL = PatternFill("solid", fgColor=C_HDR)
ZEBRA_FILL = PatternFill("solid", fgColor=C_ZEBRA)


def render_sheet(ws, conn):
    pins = conn["pins"]
    pmap = pin_map(len(pins), conn["mode"])
    ncol = len(HEADERS)

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    c = ws.cell(row=1, column=1, value=f"{conn['id']} {conn['name']} — 接口定义表")
    c.font = Font(size=14, bold=True, color=C_HDR)
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 26

    for j, h in enumerate(HEADERS, 1):
        c = ws.cell(row=3, column=j, value=h)
        c.font = Font(bold=True, color="FFFFFF", size=10.5)
        c.fill = HDR_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = BORDER
    ws.row_dimensions[3].height = 22

    # 物理相邻序 pins[i] ↔ pin_map[i]：sheet 行=3+pin_no（pin 号升序即上下排交替落位）
    for i, (pin_no, _row_idx, side) in enumerate(pmap):
        net, func, ocp = pins[i]
        vals = [conn["id"], conn["name"], f"{pin_no}（{side}）", net, func, ocp]
        row = 3 + pin_no
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=j, value=v)
            c.border = BORDER
            c.font = Font(size=10)
            c.alignment = (Alignment(horizontal="center", vertical="center") if j in (1, 3)
                           else Alignment(horizontal="left", vertical="center", indent=1))
            if pin_no % 2 == 0:
                c.fill = ZEBRA_FILL
        ws.row_dimensions[row].height = 20

    # 表尾备注区（table-style §八）
    row += 1
    for t in NOTES:
        x = ws.cell(row=row, column=1, value=t)
        x.font = Font(size=9, bold=(t == "备注："), color="595959")
        x.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncol)
        ws.row_dimensions[row].height = 18
        row += 1

    for j, w in enumerate([10, 24, 12, 22, 18, 12], 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = "A4"
    ws.sheet_view.showGridLines = False


wb = Workbook()
ws = wb.active
ws.title = CONNECTOR["id"]
render_sheet(ws, CONNECTOR)
wb.save(OUT)
print("saved:", OUT, "| connector:", CONNECTOR["id"], "| pins:", len(CONNECTOR["pins"]), "| mode:", CONNECTOR["mode"])
