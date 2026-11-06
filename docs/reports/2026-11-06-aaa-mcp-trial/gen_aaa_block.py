# -*- coding: utf-8 -*-
"""AAA / ADAS TCN2 系统框图生成器（②）
数据源：AAA 知识库的 HSI、ICD、原理图 EDF、需求表；详见同目录数据源 md。
输出：ADAS_TCN2_系统框图.drawio / .svg / .png
"""
import html
import os

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
NAME = "ADAS_TCN2_系统框图"

# ── 样式 ──────────────────────────────────────────────────────────────
S_ZONE_EXT = ("rounded=0;fillColor=#FAFAFA;strokeColor=#999999;verticalAlign=top;align=left;"
              "spacingLeft=12;spacingTop=8;fontSize=16;fontStyle=1;whiteSpace=wrap;")
S_ZONE_MB = ("rounded=0;fillColor=#FFFFFF;strokeColor=#666666;verticalAlign=top;align=left;"
             "spacingLeft=12;spacingTop=8;fontSize=16;fontStyle=1;whiteSpace=wrap;")
S_SIG = "rounded=1;fillColor=#DAE8FC;strokeColor=#6C8EBF;fontSize=12;whiteSpace=wrap;html=1;"
S_MEM = "rounded=1;fillColor=#FFF2CC;strokeColor=#D6B656;fontSize=12;whiteSpace=wrap;html=1;"
S_CONN = "rounded=0;fillColor=#FFE6CC;strokeColor=#D79B00;fontSize=12;whiteSpace=wrap;html=1;"
S_PWR = "rounded=1;fillColor=#D5E8D4;strokeColor=#82B366;fontSize=12;whiteSpace=wrap;html=1;"
S_EXT = "rounded=0;fillColor=#E1D5E7;strokeColor=#9673A6;fontSize=12;whiteSpace=wrap;html=1;"
S_EDGE = ("edgeStyle=orthogonalEdgeStyle;rounded=0;jumpStyle=arc;jumpSize=10;strokeColor=#000000;"
          "endArrow=block;endFill=1;fontSize=12;")
S_EDGE_NOARROW = ("edgeStyle=orthogonalEdgeStyle;rounded=0;jumpStyle=arc;jumpSize=10;strokeColor=#000000;"
                  "endArrow=none;fontSize=12;")
S_PWR = S_PWR  # block style
S_PWR_EDGE = ("edgeStyle=orthogonalEdgeStyle;rounded=0;strokeColor=#CC0000;strokeWidth=2;"
              "endArrow=block;endFill=1;fontSize=12;fontColor=#990000;")
S_PWR_EDGE_NOARROW = ("edgeStyle=orthogonalEdgeStyle;rounded=0;strokeColor=#CC0000;strokeWidth=2;"
                      "endArrow=none;fontSize=12;fontColor=#990000;")

# ── 几何 ──────────────────────────────────────────────────────────────
W, H = 3100, 1450


def B(idx, x, y, w, h, text, style):
    return dict(id=idx, x=x, y=y, w=w, h=h, text=text, style=style)


ZONES = [
    B("Zmb", 330, 40, 2710, 1360, "ADAS 控制器主板（MB）", S_ZONE_MB),
]

BLOCKS = [
    # 外部对象
    B("CAM", 60, 200, 200, 140, "摄像头模组\nSony IMX728\n3840×2160 / MIPI 4-lane\n供电：VCC3V3_EQ / VCC1V8", S_EXT),
    B("HARNESS", 60, 520, 200, 130, "整车线束\n12V 常电 / KL15\nCAN0~CAN3", S_EXT),
    B("ETHLINK", 60, 780, 200, 100, "1000BASE-T1\n外部链路", S_EXT),
    # 连接器
    B("X1900", 380, 140, 220, 280,
      "X1900 整车连接器 16P\nTE/1318382-8\n\nKL15 / UBD 9~16V\nCAN0~CAN3\n100BASE-T1（预留/兼容）", S_CONN),
    B("X1902", 380, 470, 220, 100, "X1902 以太网连接器\nE6S20A-40MT5-C\n1000BASE-T1", S_CONN),
    B("X1903", 380, 620, 220, 150, "X1903 摄像头 FPC 30P\n101106-203050-HF\nMIPI0 ×4 / I2C / RST", S_CONN),
    # 接口
    B("CAN0", 720, 140, 240, 80, "CAN0 收发器\nTCAN1044A / TCAN1145 / TJA1043\n（选贴）\n供电：VCC50_CAN", S_SIG),
    B("CAN1", 720, 240, 240, 80, "CAN1 收发器\nTCAN1044A / TCAN1145 / TJA1043\n（选贴）\n供电：VCC50_CAN", S_SIG),
    B("CAN2", 720, 340, 240, 80, "CAN2 收发器\nTCAN1044A / TCAN1145 / TJA1043\n（选贴）\n供电：VCC50_CAN", S_SIG),
    B("CAN3", 720, 440, 240, 80, "CAN3 收发器\nTCAN1044A / TCAN1145 / TJA1043\n（选贴）\n供电：VCC50_CAN", S_SIG),
    B("ETH", 720, 580, 240, 130,
      "以太网 PHY\nU1400 DP83TC812R\n100BASE-T1（MII）\n千兆兼容 DP83TG720R\n供电：VCC3V3_ETH / VCC1V0_ETH", S_SIG),
    # 主控/存储
    B("SOC", 1300, 180, 320, 240, "视频 SoC\nEYEQ6L (U400)\nMIPI0 / OSPI / LPDDR4 / SPI\n供电：VCC0V75 / VCC1V1 / VCC1V8\n　　　VCC3V3_EQ / VCC1V8_EQ / 0V75_MIPI_DDR", S_SIG),
    B("MCU", 1300, 780, 320, 220,
      "安全 MCU\nSAK-TC377TP-96F300S AA (U900)\nCAN FD / ETH / SPI / ADC\n供电：VCC3V3_MCU", S_SIG),
    B("DDR", 1850, 180, 280, 120, "LPDDR4\nU800 K4F8E3S4HD-GHCL02V\n1GB / 32-bit\n供电：VCC1V1 / VCC1V8", S_MEM),
    B("NAND", 1850, 360, 280, 120, "NAND FLASH\nU502 W35N02JWTBKF\n2Gb / OSPI 1.8V\n供电：VCC1V8", S_MEM),
    B("LEVEL", 1850, 560, 280, 100, "电平转换\nU503 NXB0104PW-Q100\n3V3 SPI ↔ 1V8 SPI\n用途：MCU ↔ EQ6 FLASH\n供电：VCC3V3_MCU / VCC1V8", S_SIG),
    # 电源列
    B("P1", 2300, 140, 240, 100, "输入保护 / EMI / 唤醒\nF11 + D1600 + L1600 + F25", S_PWR),
    B("P2", 2300, 280, 240, 100, "一级 Buck\nU1600 LN10046FSQ1LQR\nUBD_PR → VCC3V3", S_PWR),
    B("P3", 2300, 420, 240, 120, "PMIC + 看门狗\nU1700 LP87702RRHBRQ1\nVCC1V8 / VCC1V1 / VCC50", S_PWR),
    B("P4", 2300, 580, 240, 90, "二级 Buck\nU1800 TPS62872Y1QWRXSRQ1\nVCC3V3 → VCC0V75", S_PWR),
    B("P5", 2300, 710, 240, 90, "二级 Buck\nU1804 SA23002CDFD\nVCC3V3 → 0V75_MIPI_DDR", S_PWR),
    B("P6", 2300, 840, 240, 85, "负载开关\nU1802 TPS22918\nVCC3V3 → VCC3V3_EQ", S_PWR),
    B("P7", 2300, 965, 240, 85, "以太网电源\nU1500 TPS22918 + U1501 LDO\nVCC3V3 → 3V3_ETH / 1V0_ETH\nU1502 LN20042：UBD_PR→3V3_ETHPSD", S_PWR),
    B("P8", 2300, 1090, 240, 90, "过压保护\nU1701 TLVH431AQDBVRQ1\nVCC3V3 → VCC3V3_MCU", S_PWR),
    B("P9", 2300, 1220, 240, 85, "负载开关\nU1803 TPS22918\nVCC1V8 → VCC1V8_EQ", S_PWR),
]

BY = {b["id"]: b for b in ZONES + BLOCKS}


def P(bid, side, pos):
    """端点：块 + 边 + 沿边绝对坐标"""
    b = BY[bid]
    if side == "L":
        return (b["x"], pos)
    if side == "R":
        return (b["x"] + b["w"], pos)
    if side == "T":
        return (pos, b["y"])
    if side == "B":
        return (pos, b["y"] + b["h"])
    raise ValueError(side)


def anchor(bid, side, pos):
    b = BY[bid]
    if side == "L":
        return 0.0, (pos - b["y"]) / b["h"]
    if side == "R":
        return 1.0, (pos - b["y"]) / b["h"]
    if side == "T":
        return (pos - b["x"]) / b["w"], 0.0
    if side == "B":
        return (pos - b["x"]) / b["w"], 1.0
    raise ValueError(side)


# 边：src/dst 可以是 ("block", side, pos) 或 ("pt", x, y)
# kind: sig / pwr / pwr_noarrow / sig_noarrow
EDGES = []


def E(idx, src, dst, pts, label="", kind="sig", arrow=True, ldy=0):
    EDGES.append(dict(id=idx, src=src, dst=dst, pts=pts, label=label, kind=kind, arrow=arrow, ldy=ldy))


# 外部接口
E("e_cam", ("CAM", "R", 270), ("X1903", "L", 695), [(310, 270), (310, 695)],
  "MIPI0 ×4 / I2C / PWR")
E("e_harness", ("HARNESS", "R", 585), ("X1900", "L", 280), [(300, 585), (300, 280)],
  "12V / KL15 / CAN")
E("e_ethlink", ("ETHLINK", "R", 830), ("X1902", "L", 520), [(320, 830), (320, 520)],
  "1000BASE-T1")

# CAN 四路
E("e_can0", ("X1900", "R", 200), ("CAN0", "L", 180), [(640, 200), (640, 180)], "CAN0")
E("e_can1", ("X1900", "R", 260), ("CAN1", "L", 280), [(655, 260), (655, 280)], "CAN1")
E("e_can2", ("X1900", "R", 320), ("CAN2", "L", 380), [(670, 320), (670, 380)], "CAN2")
E("e_can3", ("X1900", "R", 380), ("CAN3", "L", 480), [(685, 380), (685, 480)], "CAN3")

# CAN → MCU（四条并行，避免重叠）
E("e_can0_mcu", ("CAN0", "R", 180), ("MCU", "L", 800),
  [(1020, 180), (1020, 800)], "CAN0 TX/RX")
E("e_can1_mcu", ("CAN1", "R", 280), ("MCU", "L", 820),
  [(1040, 280), (1040, 820)], "CAN1 TX/RX")
E("e_can2_mcu", ("CAN2", "R", 380), ("MCU", "L", 840),
  [(1060, 380), (1060, 840)], "CAN2 TX/RX")
E("e_can3_mcu", ("CAN3", "R", 480), ("MCU", "L", 860),
  [(1080, 480), (1080, 860)], "CAN3 TX/RX")

# ETH
E("e_x1902_eth", ("X1902", "R", 520), ("ETH", "L", 620), [(700, 520), (700, 620)],
  "1000BASE-T1")
E("e_eth_mcu", ("ETH", "R", 650), ("MCU", "L", 980), [(1000, 650), (1000, 980)],
  "MII/RGMII / MDIO / RST")

# 摄像头
E("e_cam_soc", ("X1903", "B", 490), ("SOC", "L", 300),
  [(490, 1080), (1240, 1080), (1240, 300)], "MIPI0 ×4 / I2C / RST")

# 存储与控制
E("e_ddr", ("SOC", "R", 230), ("DDR", "L", 230), [], "LPDDR4")
E("e_nand", ("SOC", "R", 380), ("NAND", "L", 420), [(1680, 380), (1680, 420)], "OSPI")
E("e_spi_mcu", ("SOC", "B", 1400), ("MCU", "T", 1400), [], "SPI0（经调试口）/ RST / EN")
E("e_level_mcu", ("MCU", "R", 820), ("LEVEL", "L", 660), [(1680, 820), (1680, 660)], "SPI 3V3")
E("e_level_nand", ("LEVEL", "T", 1880), ("NAND", "B", 1880), [], "SPI 1V8")

# 电源输入
E("e_ubd", ("X1900", "R", 160), ("P1", "L", 190),
  [(610, 160), (610, 95), (2280, 95), (2280, 190)], "UBD 9~16V", kind="pwr")
E("e_kl15", ("X1900", "R", 180), ("P1", "L", 210),
  [(625, 180), (625, 110), (2260, 110), (2260, 210)], "KL15", kind="sig", ldy=14)
E("e_ubd_pr", ("P1", "B", 2420), ("P2", "T", 2420), [], "UBD_PR", kind="pwr")

# 控制总线（MCU → 电源列）
E("e_ctrl", ("MCU", "R", 900), ("pt", 2230, 900), [], "EN / I2C / WDI / RST", kind="sig")
E("e_ctrl_trunk", ("pt", 2230, 190), ("pt", 2230, 1260), [], "电源使能", kind="sig_noarrow", arrow=False)
# 控制支线
for i, bid in enumerate(["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]):
    b = BY[bid]
    y = b["y"] + b["h"] // 2
    E(f"e_ctrl_{bid}", ("pt", 2230, y), (bid, "L", y), [], "EN", kind="sig_noarrow", arrow=False)

# 电源输出断线（源侧）

# 负载侧供电以块内“供电”行表达；完整逐轨映射见数据源 md / ③+ 电源树。


# ── XML / SVG ────────────────────────────────────────────────────────
def esc(s):
    return html.escape(s, quote=True)


def drawio_value(s):
    return esc(s).replace("\n", "&lt;br&gt;")


def edge_points(e):
    src = e["src"]
    dst = e["dst"]
    p0 = P(*src) if src[0] != "pt" else (src[1], src[2])
    p1 = P(*dst) if dst[0] != "pt" else (dst[1], dst[2])
    pts = [p0] + [tuple(p) for p in e["pts"]] + [p1]
    return pts


def midpoint_label(pts, dangle=None, ldy=0):
    if dangle is not None:
        return dangle[0] + 8, dangle[1], "start"
    best = None
    for a, b in zip(pts, pts[1:]):
        d = abs(a[0] - b[0]) + abs(a[1] - b[1])
        if best is None or d > best[0]:
            best = (d, a, b)
    _, a, b = best
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    if a[1] == b[1]:
        return mx, my - 10 + ldy, "middle"
    return mx + 10, my + ldy, "start"


def drawio_edge(e):
    pts = edge_points(e)
    src, dst = e["src"], e["dst"]
    dangle = ((src[1], src[2]) if src[0] == "pt" else ((dst[1], dst[2]) if dst[0] == "pt" else None)) if e["kind"].startswith("pwr") else None
    style = S_EDGE
    if e["kind"].startswith("pwr"):
        style = S_PWR_EDGE if e["arrow"] else S_PWR_EDGE_NOARROW
    elif not e["arrow"]:
        style = S_EDGE_NOARROW
    attrs = [f'id="{e["id"]}"']
    if e["label"]:
        attrs.append(f'value="{drawio_value(e["label"])}"')
    attrs.append(f'style="{style}"')
    attrs.append('edge="1" parent="1"')
    out = [f'<mxCell {" ".join(attrs)}>', '<mxGeometry relative="1" as="geometry">']
    out.append(f'<mxPoint x="{pts[0][0]}" y="{pts[0][1]}" as="sourcePoint"/>')
    out.append(f'<mxPoint x="{pts[-1][0]}" y="{pts[-1][1]}" as="targetPoint"/>')
    wps = [tuple(p) for p in e["pts"]]
    if wps:
        out.append('<Array as="points">')
        for x, y in wps:
            out.append(f'<mxPoint x="{x}" y="{y}"/>')
        out.append('</Array>')
    if e["label"]:
        lx, ly, _ = midpoint_label(pts, dangle, e.get("ldy",0))
        mx, my = (pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2
        offx, offy = lx - mx, ly - my
        out.append(f'<mxPoint x="{offx:.1f}" y="{offy:.1f}" as="offset"/>')
    out.append('</mxGeometry></mxCell>')
    return "\n".join(out)


def drawio_block(b):
    return (f'<mxCell id="{b["id"]}" value="{drawio_value(b["text"])}" style="{b["style"]}" '
            f'vertex="1" parent="1"><mxGeometry x="{b["x"]}" y="{b["y"]}" width="{b["w"]}" '
            f'height="{b["h"]}" as="geometry"/></mxCell>')


def build_drawio():
    parts = [f'<mxfile host="hdb-hw-design"><diagram id="aaa-tcn2" name="ADAS TCN2 系统框图">'
             f'<mxGraphModel dx="0" dy="0" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" '
             f'arrows="1" fold="1" page="1" pageScale="1" pageWidth="{W}" pageHeight="{H}" math="0" shadow="0">'
             f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>']
    # z-order: zones -> edges -> blocks
    for z in ZONES:
        parts.append(drawio_block(z))
    for e in EDGES:
        parts.append(drawio_edge(e))
    for b in BLOCKS:
        parts.append(drawio_block(b))
    parts.append('</root></mxGraphModel></diagram></mxfile>')
    return "\n".join(parts)


def svg_text(x, y, s, size=13, anchor="middle", weight="normal", color="#000000"):
    lines = s.split("\n")
    out = []
    lh = size + 4
    total = lh * len(lines)
    start = y - total / 2 + lh / 2
    for i, line in enumerate(lines):
        out.append(f'<text x="{x}" y="{start + i*lh:.1f}" font-family="Arial, Noto Sans CJK SC, sans-serif" '
                   f'font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}" '
                   f'dominant-baseline="middle" paint-order="stroke" stroke="#ffffff" stroke-width="2">{esc(line)}</text>')
    return "\n".join(out)


def build_svg():
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>']
    # zones
    for z in ZONES:
        fill = "#FAFAFA" if z["id"] == "Zext" else "#FFFFFF"
        out.append(f'<rect x="{z["x"]}" y="{z["y"]}" width="{z["w"]}" height="{z["h"]}" '
                   f'fill="{fill}" stroke="#999999" stroke-width="1"/>')
        out.append(svg_text(z["x"] + 12, z["y"] + 20, z["text"], 16, "start", "bold", "#333333"))
    # edges
    for e in EDGES:
        pts = edge_points(e)
        src, dst = e["src"], e["dst"]
        dangle = ((src[1], src[2]) if src[0] == "pt" else ((dst[1], dst[2]) if dst[0] == "pt" else None)) if e["kind"].startswith("pwr") else None
        color = "#CC0000" if e["kind"].startswith("pwr") else "#000000"
        width = 2 if e["kind"].startswith("pwr") else 1.2
        dash = "" if e["kind"] != "reserved" else ' stroke-dasharray="6,4"'
        path = " ".join(f"{x},{y}" for x, y in pts)
        out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="{width}"{dash}/>')
        if e["arrow"]:
            # arrowhead at last segment
            a, b = pts[-2], pts[-1]
            import math
            ang = math.atan2(b[1] - a[1], b[0] - a[0])
            L = 8
            x1 = b[0] - L * math.cos(ang - 0.5)
            y1 = b[1] - L * math.sin(ang - 0.5)
            x2 = b[0] - L * math.cos(ang + 0.5)
            y2 = b[1] - L * math.sin(ang + 0.5)
            out.append(f'<polygon points="{b[0]},{b[1]} {x1:.1f},{y1:.1f} {x2:.1f},{y2:.1f}" fill="{color}"/>')
        if e["label"]:
            lx, ly, anch = midpoint_label(pts, dangle, e.get("ldy",0))
            out.append(svg_text(lx, ly, e["label"], 12, anch, "normal", "#990000" if e["kind"].startswith("pwr") else "#111111"))
    # blocks
    for b in BLOCKS:
        if "FFE6CC" in b["style"]:
            fill, stroke = "#FFE6CC", "#D79B00"
        elif "D5E8D4" in b["style"]:
            fill, stroke = "#D5E8D4", "#82B366"
        elif "FFF2CC" in b["style"]:
            fill, stroke = "#FFF2CC", "#D6B656"
        elif "E1D5E7" in b["style"]:
            fill, stroke = "#E1D5E7", "#9673A6"
        else:
            fill, stroke = "#DAE8FC", "#6C8EBF"
        out.append(f'<rect x="{b["x"]}" y="{b["y"]}" width="{b["w"]}" height="{b["h"]}" rx="6" '
                   f'fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>')
        out.append(svg_text(b["x"] + b["w"] / 2, b["y"] + b["h"] / 2, b["text"], 13, "middle"))
    out.append('</svg>')
    return "\n".join(out)


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    drawio_path = os.path.join(OUT_DIR, NAME + ".drawio")
    svg_path = os.path.join(OUT_DIR, NAME + ".svg")
    with open(drawio_path, "w", encoding="utf-8") as f:
        f.write(build_drawio())
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(build_svg())
    print("saved", drawio_path)
    print("saved", svg_path)
