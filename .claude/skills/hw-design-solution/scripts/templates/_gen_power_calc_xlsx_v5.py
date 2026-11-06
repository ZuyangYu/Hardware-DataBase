# -*- coding: utf-8 -*-
"""第四步 电源计算表 v5.3〔2026-10-20 按用户反馈〕
● 删"设计电流"口径——校核逻辑=电源输出能力 > 负载最大耗流（用户定）；表尾 D1/极值核校按最大电流口径；
● DMD 阵列 1.8V 电流按手册值定案（用户 2026-10-20 已确认），删"max 待向 TI 索取"标注；
● R1 设计 500mA 待确认项随设计口径取消一并删除；
● v5.2 基础：单 sheet 极简公式——直送/LDO 输入=输出、buck=Iout×Vout÷0.9÷6V、
  6V 总线=SUM(折算行)、9V 总线=6V÷(入口×0.90)，每格公式单一逻辑可读。

skill 适配说明：输出目录默认原始路径 D:\\python_project\\20_SOL，
可用环境变量 HW_DESIGN_OUT 覆盖（目录不存在时自动创建），便于跨机直接运行。
"""

import os

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

OUT_DIR = os.environ.get("HW_DESIGN_OUT") or r"D:\python_project\20_SOL"
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, "第四步_电源计算表_v5.xlsx")

# ── 配色 ──
C_HDR = '44546A'
C_RAIL = 'DEEBF7'
C_SUM = 'FCE4D6'
C_KEY = 'E2EFDA'
C_WARN = 'FFF2CC'
C_LINE = 'BFBFBF'
C_NOTE = '808080'

F_HDR = Font(name='微软雅黑', size=10, bold=True, color='FFFFFF')
F_RAIL = Font(name='微软雅黑', size=10, bold=True)
F_BASE = Font(name='微软雅黑', size=10)
F_SMALL = Font(name='微软雅黑', size=9, color=C_NOTE)
F_TITLE = Font(name='微软雅黑', size=13, bold=True)
F_SUB = Font(name='微软雅黑', size=9, color='595959')
F_SUM = Font(name='微软雅黑', size=10, bold=True)

THIN = Side(style='thin', color=C_LINE)
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CTR = Alignment(horizontal='center', vertical='center', wrap_text=True)
RIGHT = Alignment(horizontal='right', vertical='center')
LEFT = Alignment(horizontal='left', vertical='center', wrap_text=True)


def fill(hexcode):
    return PatternFill('solid', fgColor=hexcode)


def cell(ws, r, c, v, font=F_BASE, align=LEFT, fl=None, border=True, numfmt=None):
    x = ws.cell(row=r, column=c, value=v)
    x.font = font
    x.alignment = align
    if fl: x.fill = fill(fl)
    if border: x.border = BORDER
    if numfmt: x.number_format = numfmt
    return x


def _tw(s):
    """估算显示宽度（微软雅黑 10 号：全角字符≈2，ASCII≈1.1）"""
    return sum(2 if ord(ch) > 127 else 1.1 for ch in str(s))


def autofit(ws, row, cols, line_h=15, pad_w=2.5, pad_h=8):
    """按单元格内容（\n 分行取最长行）自适应列宽与该行行高；只增不减"""
    nlines = 1
    for c in cols:
        v = ws.cell(row=row, column=c).value
        if v is None:
            continue
        lines = str(v).split('\n')
        nlines = max(nlines, len(lines))
        need = max(_tw(x) for x in lines) + pad_w
        L = get_column_letter(c)
        if (ws.column_dimensions[L].width or 0) < need:
            ws.column_dimensions[L].width = need
    ws.row_dimensions[row].height = nlines * line_h + pad_h


# ══════════════════════ 数据（唯一数据源，单位 mA） ══════════════════════
# 轨定义:（轨号, 轨名, 电压, 电源芯片, 口径一句话）
RAILS = [
    ('R1',  '6V 直送 TPS9900S',      '6V',   'SA24506E',     '手册 §5.12：typ 98/max 119（含高压三轨满载折算）'),
    ('R2',  '3.3V_MCU',             '3.3V', 'SA21346B LDO', 'FC4150 Table 7〔GPIO/上拉免计〕'),
    ('R3',  '5V_CAN',               '5V',   'SA21346B LDO', 'TCAN1043 §6.7 dominant'),
    ('R4',  '3.3V 主轨【合并】',     '3.3V', 'SA25903 buck', '手册负载合计 292 客户分支 404 待打合'),
    ('R5',  '1.8V【合并】',          '1.8V', 'SA25903 buck', '手册 §6.6〔DMD 阵列按手册值，用户已确认〕'),
    ('R6',  'DDR 1.1V',             '1.1V', 'SA25903 buck', 'LPDDR4 手册 G1 1DDR 830'),
    ('R7',  'DVDD_CPU 0.9V【SVB1】', '0.9V', 'SA25903 buck', '欧冶场景 1 实测 233 底线 1200 兜底'),
    ('R8',  'DVDD_NPU 0.9V【SVB2】', '0.9V', 'SA23004 buck', '欧冶场景 1 实测 411 底线 3000 兜底'),
    ('R9',  'DVDD 0.9V【SVB0】',     '0.9V', 'TPS62816 buck', '欧冶场景 1 实测 789 底线 4000 兜底'),
    ('R12', 'DMD 1.1V',             '1.1V', 'SA25903 buck', 'DLPC231 §5.5 max 467'),
    ('R13', '5V_DRV',               '5V',   'SA21346B LDO', '用户确认 20mA'),
]
topo_is_ldo = {r[0]: 'LDO' in r[3] for r in RAILS}
volt_float = {'3.3V': 3.3, '1.8V': 1.8, '1.1V': 1.1, '0.9V': 0.9, '5V': 5.0, '6V': 6.0}

# 轨→芯片明细（数据源，含手册出处详注；表格中由 MATRIX 呈现）
LOADS = {
    # === R1 ===
    'R1': [('TPS9900S', 'DMD 高压 PMIC', '6V 供电脚整组（含高压三轨满载折算）', 98, 119,
            'TPS9900 §5.12：System on typ 98/max 119—含 DMD 电压调节器满载折算 63mA〔公式 16V=[(16/6)IVBIAS+(8.5/6)IVOFFT+(10/6)IVRESET]/0.9〕+空载 3/56mA Headlight 配置'),
           ('DLP5530S', 'DMD 阵列', '高压三轨负载侧: VOFFSET 6 + VBIAS 1 + VRESET 4.5', 11.5, 11.5,
            '〔已折算入上行走线 6V 整组值—不重复计入轨合计）手册 §6.6 typ〔8.75/16.5/10.5V 测〕')],
    # === R2 ===
    'R2': [('FC4150', 'MCU', 'VDD_HV + VREFH', 37, 98.8,
            'FC4150 Table 7：VDD_HV typ 36.3@25°C/max 98.1@125°C〔手册按 5V 测，3.3V 轨按上界保守〕；VREFH 0.7〔Table 24〕；GPIO/板级上拉免计〔2026-10-20 用户规则〕')],
    # === R3 ===
    'R3': [('TCAN1043', 'CANFD 收发器', 'VCC', 70, 110, '手册 §6.7 dominant：typ 70@60Ω / 110 含总线故障（无 max 列，取手册最大值）'),
           ('SN3257-Q1', '调光路由', 'VDD', 0.04, 0.068, '手册 DC 表 I_DD active 40/68μA'),
           ('SN74LVC1G04-Q1', '反相器', 'VCC', 0.01, 0.01, '手册 §5.5 Icc max 10μA')],
    # === R4 ===
    'R4': [('LQ560', 'SOC', 'DVDD33 整域（含 DVDD3318@3.3V、SENSOR 并入）', 51.5, 51.5, '用户确认典型最大 170mW〔2026-10-20〕'),
           ('DP83TC812', 'ETH PHY', 'VDDA + VDDIO/VDDMAC（RGMII）', 70, 84, '手册 Table 8-4'),
           ('XN411', 'eMMC', 'VCC', 60, 85, '手册 Table 10（HS400）：Read 85/Write 60'),
           ('TCAN1043', 'VIO', 0.45, 0.45, '手册 §6.7 I_IO：dominant 450μA（RXD floating/TXD=0V）；recessive 5μA—原"取 5 保守"系估算，已修正为手册值'),
           ('DMD 3.3V 分支（经 R10 开关）', 'DLPC231S + TPS9900S 数字 + GD25F128F', 50.6, 69.1,
            '手册合计（DLPC231 30.1 + TPS9900 数字 4 + GD25F128F 35）；客户板对板分支 404 Max—差 335 待打合')],
    # === R5 ===
    'R5': [('XN411', 'eMMC', 'VCCQ', 80, 170, '手册 Table 10（HS400）：Read 170/Write 80'),
           ('SN65DSI83', 'DSI>LVDS 桥', 'VCC 整芯', 77, 124, '手册表 6.5：typ 77/max 124（LVDS 83MHz/1280×800/85°C）—原取 130 已修正为手册值'),
           ('LQ560', 'SOC', 'DVDD18 整域（已含 MIPITX）', 28, 28, '用户确认 50mW 为总功耗口径〔2026-10-20〕，已含 MIPITX 轨—不再单列 30mA'),
           ('LPDDR4', '南亚', 'VDD1', 30, 30, '手册 G1 IDD 表单列 max@125°C：IDD5 刷新 30'),
           ('DMD 板→DLPC231S（经 R11 开关）', 'VCC18', 71, 151.6, '手册 §5.5 I(VCC18)：typ 71/max 151.6'),
           ('DMD 板→DLP5530S（DMD 阵列，经 R11 开关）', 'VDD + VDDI', 365, 365,
            '手册 §6.6：VDD 310 + VDDI 55（typ 1.95V 测）—用户 2026-10-20 确认按手册值')],
    # === R6 ===
    'R6': [('LQ560', 'SOC', 'VDDIO_DDR 整域（含经磁珠派生的 VDDIOA_DDR/VDDIO_DDR_CK 引脚）', 200, 200,
            '用户确认典型最大 220mW 整域口径〔2026-10-20〕—磁珠为滤波元件不计耗流，派生引脚耗流计入本域'),
           ('LPDDR4', '南亚', 'VDD2/VDDQ', 400, 830, '手册 G1 IDD4R 487+343（最坏持续读）；typ 为应用估算')],
    # === R7 ===
    'R7': [('LQ560', 'SOC', 'DVDD_CPU（欧冶场景 1）', 233, 233, '场景 1 实测=210mW/0.9V；真最大以欧冶底线 1200 兜底')],
    # === R8 ===
    'R8': [('LQ560', 'SOC', 'DVDD_NPU（欧冶场景 1）', 411, 411, '场景 1 实测=370mW/0.9V；真最大以欧冶底线 3000 兜底')],
    # === R9 ===
    'R9': [('LQ560', 'SOC', 'DVDD 主核（欧冶场景 1）', 789, 789, '场景 1 实测=710mW/0.9V；SVB 1.0V 时 ≈860；真最大以欧冶底线 4000 兜底')],
    # === R12 ===
    'R12': [('DLPC231S', 'CORE 1.1V 整芯', 201, 467.1, '手册 §5.5 I(VCC11) total：typ 201/max 467.1；客户板对板 1.1V 分支 470 ≈ max 467.1 吻合')],
    # === R13 ===
    'R13': [('TPS9900S', 'DRVR_PWR 栅极 5V', 20, 20, '用户确认按 20mA 评估〔2026-10-20〕；Qg 打合后回修')],
}

# 芯片x轨矩阵行: (板, 芯片, {轨号: (typ, max, 格内备注)}, 芯片备注)
MATRIX = [
    ('SOC 板', 'LQ560', {'R4': (51.5, 51.5, 'DVDD33'), 'R5': (28, 28, 'DVDD18 含 MIPITX'),
                          'R6': (200, 200, 'VDDIO_DDR'), 'R7': (233, 233, 'CPU'), 'R8': (411, 411, 'NPU'),
                          'R9': (789, 789, 'DVDD 主核')}, '域典型最大功耗 170/50/220mW 用户确认〔总功耗口径，含派生域〕；core 三轨=场景 1'),
    ('SOC 板', 'LPDDR4（南亚）', {'R5': (30, 30, 'VDD1'), 'R6': (400, 830, 'VDD2/VDDQ')}, 'max=G1 IDD 表单列@125°C'),
    ('SOC 板', 'XN411（eMMC）', {'R4': (60, 85, 'VCC'), 'R5': (80, 170, 'VCCQ')}, '手册 Table 10（HS400）'),
    ('SOC 板', 'SN65DSI83', {'R5': (77, 124, 'VCC 整芯')}, '表 6.5：LVDS 83MHz/1280×800/85°C 口径'),
    ('SOC 板', 'DP83TC812', {'R4': (70, 84, 'RGMII')}, 'Table 8-4'),
    ('SOC 板', 'TCAN1043', {'R3': (70, 110, 'VCC=5V'), 'R4': (0.45, 0.45, 'VIO=3.3V')}, 'VCC 与 VIO 分属两轨（电压域规则）；VIO=I_IO dominant 450μA'),
    ('SOC 板', 'FC4150', {'R2': (37, 98.8, 'VDD_HV 等')}, 'Table 7 拼接口径；GPIO/上拉免计'),
    ('SOC 板', 'SN3257-Q1', {'R3': (0.04, 0.068, 'VDD')}, '调光路由'),
    ('SOC 板', 'SN74LVC1G04-Q1', {'R3': (0.01, 0.01, 'VCC')}, '调光反相'),
    ('DMD 板', 'TPS9900S', {'R1': (98, 119, '6V 整组'), 'R4': (3.5, 4, '数字 3.3V'), 'R13': (20, 20, 'DRVR_PWR')},
     '§5.12：高压三轨满载已折算入 6V 整组'),
    ('DMD 板', 'DLPC231S', {'R4': (28.1, 30.1, 'VCC33'), 'R5': (71, 151.6, 'VCC18'),
                             'R12': (201, 467.1, 'VCC11')}, '手册 §5.5 实测：TYP 25°C 典型图/MAX 105°C 伪随机噪声'),
    ('DMD 板', 'DMD 阵列（DLP5530S）', {'R5': (365, 365, 'VDD 310+VDDI 55')},
     '手册 §6.6 仅 typ（1.95V 测）；另挂 R1 高压三轨 VOFFSET 6+VBIAS 1+VRESET 4.5=11.5（已折算入 TPS9900S §5.12 整组，不重复计）；P_TOTAL 828mW'),
    ('DMD 板', 'GD25F128F（Flash）', {'R4': (19, 35, 'VCC')}, '直流特性：读 166MHz 19/35、写 12/25 取 max 35'),
]

# 来源列（只写出处，一句话）
SRC = {
    'LQ560': '欧冶用户确认（域功耗）+ 欧冶场景 1 实测（core 三轨）',
    'LPDDR4（南亚）': '手册 G1 IDD 表',
    'XN411（eMMC）': '手册 Table 10',
    'SN65DSI83': '手册表 6.5',
    'DP83TC812': '手册 Table 8-4',
    'TCAN1043': '手册 §6.7',
    'FC4150': '手册 Table 7 / Table 24',
    'SN3257-Q1': '手册 DC 特性表',
    'SN74LVC1G04-Q1': '手册 §5.5',
    'TPS9900S': '手册 §5.12',
    'DLPC231S': '手册 §5.5',
    'DMD 阵列（DLP5530S）': '手册 §6.6（用户已确认）',
    'GD25F128F（Flash）': '手册直流特性表',
}

# ══════════════════════ 耗流计算表（唯一 sheet） ══════════════════════
wb = Workbook()
ws2 = wb.active
ws2.title = '耗流计算表'
ws2.sheet_view.showGridLines = False
n_rails = len(RAILS)
COL_BUS9 = 2                    # B: 9V 总线（一级输入）
COL_BUS6 = 3                    # C: 6V 总线（一级输出 = R1 直送 + Σ二级电源输入电流）
COL_RAIL0 = 4                   # D: R1 起
COL_TOT = COL_RAIL0 + n_rails   # 芯片合计
COL_SRC = COL_TOT + 1           # 来源
n_col = COL_SRC

cell(ws2, 2, 1, '耗流计算表—每个芯片用哪路电、用多少', F_TITLE, CTR, border=False)
ws2.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n_col)
cell(ws2, 3, 1, '单位 mA ｜｜ 一二级电源链：每轨折算 6V 输入电流（直送/LDO 输入=输出；buck=Iout×Vout÷0.9÷6V）→ Σ=6V 总线（一级输出）'
                '→ ÷(入口×0.90)=9V 总线（一级输入）max 按入口最小 8.97V；全部公式，手改芯片格自动更新', F_SUB, CTR, border=False)
ws2.merge_cells(start_row=3, start_column=1, end_row=3, end_column=n_col)
ws2.row_dimensions[3].height = 30

HR = 5
cell(ws2, HR, 1, '芯片 ｜ 电源轨', F_HDR, CTR, C_HDR)
cell(ws2, HR, COL_BUS9, '9V 总线\nKL30 入口', F_HDR, CTR, C_HDR)
cell(ws2, HR, COL_BUS6, '6V 总线\n一级输出\nR1+Σ二级输入', F_HDR, CTR, C_HDR)
RAIL_HEAD = {
    'R1':  'R1\n6V 直送\nSA24506E',
    'R2':  'R2\n3.3V_MCU\nSA21346B LDO',
    'R3':  'R3\n5V_CAN\nSA21346B LDO',
    'R4':  'R4\n3.3V 主轨\nSA25903',
    'R5':  'R5\n1.8V\nSA25903',
    'R6':  'R6\nDDR 1.1V\nSA25903',
    'R7':  'R7\nDVDD_CPU 0.9V\nSA25903',
    'R8':  'R8\nDVDD_NPU 0.9V\nSA23004',
    'R9':  'R9\nDVDD 0.9V\nTPS62816',
    'R12': 'R12\nDMD 1.1V\nSA25903',
    'R13': 'R13\n5V_DRV\nSA21346B LDO',
}
for i, (rid, *rest) in enumerate(RAILS):
    cell(ws2, HR, COL_RAIL0 + i, RAIL_HEAD[rid], F_HDR, CTR, C_HDR)
cell(ws2, HR, COL_TOT, '芯片合计\nΣ(SUM)', F_HDR, CTR, C_HDR)
cell(ws2, HR, COL_SRC, '耗流来源', F_HDR, CTR, C_HDR)


def chip_block(r0, label, vi, block_color, sum_label):
    """vi: 0=typ 1=max（芯片行 + 轨合计 SUM 行 + 折算 6V 输入电流行（承载 6V/9V 总线结果））；返回折算行号"""
    cell(ws2, r0, 1, label, F_SUM, LEFT, block_color)
    for cc in range(2, n_col + 1):
        cell(ws2, r0, cc, '', F_BASE, LEFT, block_color)
    ws2.merge_cells(start_row=r0, start_column=1, end_row=r0, end_column=n_col)  # 块头合并全表宽，靠左
    r = r0 + 1
    first = r
    for board, chip, cells, note in MATRIX:
        cell(ws2, r, 1, f'{board}｜{chip}', F_BASE, LEFT)
        cell(ws2, r, COL_BUS9, '—', F_SMALL, CTR)
        cell(ws2, r, COL_BUS6, '—', F_SMALL, CTR)
        for i, (rid, *rest) in enumerate(RAILS):
            c = COL_RAIL0 + i
            if rid in cells:
                cell(ws2, r, c, cells[rid][vi], F_BASE, CTR, numfmt='0.00')
            else:
                cell(ws2, r, c, '—', F_SMALL, CTR)
        cL, cR = get_column_letter(COL_RAIL0), get_column_letter(COL_TOT - 1)
        cell(ws2, r, COL_TOT, f'=SUM({cL}{r}:{cR}{r})', F_BASE, CTR, numfmt='0.00')
        cell(ws2, r, COL_SRC, SRC[chip], F_SMALL, LEFT)
        ws2.row_dimensions[r].height = 30
        r += 1
    last = r - 1
    # 轨合计行（负载侧）
    cell(ws2, r, 1, sum_label, F_SUM, LEFT, C_SUM)
    cell(ws2, r, COL_BUS9, '—', F_SMALL, CTR, C_SUM)
    cell(ws2, r, COL_BUS6, '—', F_SMALL, CTR, C_SUM)
    for i in range(n_rails):
        col = get_column_letter(COL_RAIL0 + i)
        cell(ws2, r, COL_RAIL0 + i, f'=SUM({col}{first}:{col}{last})', F_SUM, CTR, C_SUM, numfmt='0.00')
    cT = get_column_letter(COL_TOT)
    cell(ws2, r, COL_TOT, f'=SUM({cT}{first}:{cT}{last})', F_SUM, CTR, C_SUM, numfmt='0.00')
    cell(ws2, r, COL_SRC, '各轨负载侧合计=SUM', F_SMALL, LEFT, C_SUM)
    ws2.row_dimensions[r].height = 30
    r += 1
    # 折算 6V 输入电流行：负载电流 → 电源芯片输入电流（对 6V 总线的贡献）
    cell(ws2, r, 1, '折算 6V 输入电流 → 总线', F_SUM, LEFT, C_KEY)
    for i, (rid, rname, volt, topo, note) in enumerate(RAILS):
        col = get_column_letter(COL_RAIL0 + i)
        if topo_is_ldo[rid] or rid == 'R1':
            f_ = f'={col}{r - 1}'               # 直送/LDO：输入电流=输出电流
        else:
            f_ = f'=ROUND({col}{r - 1}*{volt_float[volt]}/0.9/6,2)'   # buck: Iout×Vout÷η÷6V
        cell(ws2, r, COL_RAIL0 + i, f_, F_SUM, CTR, C_KEY, numfmt='0.00')
    cL, cR = get_column_letter(COL_RAIL0), get_column_letter(COL_TOT - 1)
    # 6V 总线（一级输出）= Σ各轨输入电流（含 R1 直送）
    cell(ws2, r, COL_BUS6, f'=ROUND(SUM({cL}{r}:{cR}{r}),2)', F_SUM, CTR, C_KEY, numfmt='0.00')
    # 9V 总线（一级输入）= 6V 总线功率/(入口电压×η)；typ 用入口 9V，max 用入口最小 8.97V
    c6 = get_column_letter(COL_BUS6)
    vin = '9' if vi == 0 else '8.97'
    cell(ws2, r, COL_BUS9, f'=ROUND({c6}{r}*6/({vin}*0.9),2)', F_SUM, CTR, C_KEY, numfmt='0.00')
    cell(ws2, r, COL_TOT, '—', F_BASE, CTR, C_KEY)
    cell(ws2, r, COL_SRC, '直送/LDO：输入=输出；buck：Iout×Vout÷0.9÷6V  ｜ C 列=Σ6V 总线（一级输出）；B 列=9V 总线（一级输入）', F_SMALL, LEFT, C_KEY)
    ws2.row_dimensions[r].height = 30
    return r


rt = chip_block(HR + 1, '【典型值 typ】', 0, C_RAIL, '轨合计（典型）=SUM')
rm = chip_block(rt + 1, '【最大值 max】', 1, C_SUM, '轨合计（最大）=SUM')

# 当前电源选型输出能力（9V 为灯驱输入非电源输出，填"—"；6V=SA24506E 6A）
CAP = {'R1': '—', 'R2': 300, 'R3': 300, 'R4': 3000, 'R5': 3000, 'R6': 3000,
       'R7': 3000, 'R8': 4000, 'R9': 6000, 'R12': 3000, 'R13': 300}
r = rm + 1
cell(ws2, r, 1, '当前电源选型输出能力', F_SUM, LEFT, C_KEY)
cell(ws2, r, COL_BUS9, '—', F_SUM, CTR, C_KEY)
cell(ws2, r, COL_BUS6, 6000, F_SUM, CTR, C_KEY, numfmt='0.00')
for i, (rid, *rest) in enumerate(RAILS):
    v = CAP[rid]
    cell(ws2, r, COL_RAIL0 + i, v, F_SUM, CTR, C_KEY, numfmt='0.00' if isinstance(v, int) else None)
cell(ws2, r, COL_TOT, '—', F_SUM, CTR, C_KEY)
cell(ws2, r, COL_SRC, '选型规则：输出能力 ≥ 负载最大耗流；0.9V 三轨遵循欧冶底线（CPU 1.2/NPU 3/DVDD 4A）；6V 列=SA24506E 6A；9V 为灯驱输入（SRS 限流 3A）非电源输出', F_SMALL, LEFT, C_KEY)
ws2.row_dimensions[r].height = 30

# 表尾备注：关键核校与待打合项（口径=电源输出能力 > 负载最大耗流；数值与表内折算行一致）
notes = [
    ('备注：', None),
    ('① D1 防反二极管：最大输入 1.01A 时损耗 0.51W，Tj=98℃〔RθJA≤25℃/W〕✓', C_KEY),
    ('② 极端核校：core 三轨取欧冶底线〔1.2/3/4A〕并发、其余轨按 max；9V 输入 ≈1.85A ≤ 3A ✓', C_KEY),
    ('③ 待打合：DMD 3.3V 分支客户口径 404 vs 手册合计 69、差 335 请客户确认构成', C_WARN),
    ('④ 休眠：断电式 <1μA / 软关机 ≈41μA ≤ 100μA ✓', C_KEY),
]
r += 1
for t, c in notes:
    x = ws2.cell(row=r, column=1, value=t)
    x.font = Font(name='微软雅黑', size=9, bold=(t == '备注：'), color='595959')
    x.alignment = Alignment(wrap_text=True, vertical='top')
    ws2.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n_col)
    if c:
        for cc in range(1, n_col + 1):
            ws2.cell(row=r, column=cc).fill = fill(c)
    ws2.row_dimensions[r].height = 18
    r += 1

# 列宽
ws2.column_dimensions['A'].width = 24
ws2.column_dimensions['B'].width = 12
ws2.column_dimensions['C'].width = 12
for i in range(n_rails):
    ws2.column_dimensions[get_column_letter(COL_RAIL0 + i)].width = 9.5
ws2.column_dimensions[get_column_letter(COL_TOT)].width = 11
ws2.column_dimensions[get_column_letter(COL_SRC)].width = 42
# 表头行（含轨头电源芯片三行文本）与 A 列按内容自适应，防显示不全
autofit(ws2, HR, range(1, n_col + 1))
need_a = max(_tw(f'{b}｜{c}') for b, c, *_ in MATRIX) + 2.5
if ws2.column_dimensions['A'].width < need_a:
    ws2.column_dimensions['A'].width = need_a
ws2.freeze_panes = 'D6'

wb.save(OUT)
print('saved v5.3 xlsx（仅耗流计算表）:', OUT)
