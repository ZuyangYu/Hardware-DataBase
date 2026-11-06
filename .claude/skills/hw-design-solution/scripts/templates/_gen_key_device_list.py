# -*- coding: utf-8 -*-
"""生成《关键器件清单》Excel v1.2 —— 整体硬件方案框图第一步产出物
v1.2（2026-10-22 用户修订）：
- 删说明行；器件列/型号列去掉括号说明；不区分 SOC/DMD 板（无分组）
- 删除连接器/插座/灯板（清单=芯片，连接器只在框图体现）
- 删除复位 IC〔2026-10-20 用户已确认方案无单独复位 IC，文档滞后〕
- 新增"厂家"列
数据来源：第五步_硬件方案汇总.md v1.1 + 第四步_电源架构.md v4 + 用户口头定案修正

skill 适配说明：输出目录默认原始路径 D:\\python_project\\20_SOL，
可用环境变量 HW_DESIGN_OUT 覆盖（目录不存在时自动创建），便于跨机直接运行。
"""
import os

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

OUT_DIR = os.environ.get("HW_DESIGN_OUT") or r"D:\python_project\20_SOL"
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, "关键器件清单_v1.xlsx")

# （器件, 型号, 厂家, 单机用量）
ITEMS = [
    ("DC-DC buck",            "SA24506E",           "矽力杰",    1),
    ("DC-DC buck",            "SA25903",            "矽力杰",    5),
    ("DC-DC buck",            "SA23004",            "矽力杰",    1),
    ("DC-DC buck",            "TPS62816",           "TI",        1),
    ("LDO",                   "SA21346B",           "矽力杰",    1),
    ("SOC 主处理器",          "LQ560",              "欧冶",      1),
    ("MCU",                   "FC4150F1MBS1P64T1B", "旗芯微",    1),
    ("LPDDR4",                "NT6AN128T32AC-J1H",  "南亚",      1),
    ("eMMC",                  "XN4118250",          "芯天下",    1),
    ("DSI 转 LVDS 桥接",      "SN65DSI83-Q1",       "TI",        1),
    ("CANFD 收发器",          "TCAN1043GDMTRQ1",    "TI",        1),
    ("以太网 PHY",            "DP83TC812SRHARQ1",   "TI",        1),
    ("调光路由模拟开关",      "SN3257-Q1",          "TI",        1),
    ("逻辑反相器",            "SN74LVC1G04-Q1",     "TI",        1),
    ("DMD 数字微镜器件",      "DLP5530S-Q1",        "TI",        1),
    ("DLP 显示控制器",        "DLPC231S-Q1",        "TI",        1),
    ("DLP PMIC",              "TPS99000S-Q1",       "TI",        1),
    ("SPI NOR Flash",         "GD25F128F",          "兆易创新",  1),
    ("温度传感器",            "TMP411",             "TI",        1),
]

HEADERS = ["序号", "器件", "型号", "厂家", "单机用量"]
NCOL = len(HEADERS)

wb = Workbook()
ws = wb.active
ws.title = "关键器件清单"

thin = Side(style="thin", color="D0D0D0")
border = Border(left=thin, right=thin, top=thin, bottom=thin)
hdr_fill = PatternFill("solid", fgColor="1F4E79")
zebra_fill = PatternFill("solid", fgColor="F5F8FC")

# 标题
ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=NCOL)
c = ws.cell(row=1, column=1, value="一汽大众 CSP 彩色 DLP 像素大灯模组 — 硬件方案关键器件清单")
c.font = Font(size=14, bold=True, color="1F4E79")
c.alignment = Alignment(horizontal="left", vertical="center")
ws.row_dimensions[1].height = 26

# 表头（第 3 行）
HDR_ROW = 3
for j, h in enumerate(HEADERS, 1):
    c = ws.cell(row=HDR_ROW, column=j, value=h)
    c.font = Font(bold=True, color="FFFFFF", size=10.5)
    c.fill = hdr_fill
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.border = border
ws.row_dimensions[HDR_ROW].height = 22

# 数据行（斑马纹）
row = HDR_ROW + 1
for i, (dev, spec, vendor, qty) in enumerate(ITEMS):
    vals = [i + 1, dev, spec, vendor, f"×{qty}"]
    for j, v in enumerate(vals, 1):
        c = ws.cell(row=row, column=j, value=v)
        c.border = border
        c.font = Font(size=10)
        if j in (1, 4, 5):
            c.alignment = Alignment(horizontal="center", vertical="center")
        else:
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        if i % 2 == 1:
            c.fill = zebra_fill
    ws.row_dimensions[row].height = 20
    row += 1

# 列宽
for j, w in enumerate([7, 22, 26, 12, 11], 1):
    ws.column_dimensions[get_column_letter(j)].width = w

ws.freeze_panes = "A4"
ws.sheet_view.showGridLines = False

wb.save(OUT)
print("saved:", OUT, "| devices:", len(ITEMS))
