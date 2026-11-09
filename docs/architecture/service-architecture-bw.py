#!/usr/bin/env python3
"""Generate a black-and-white service architecture block diagram (.drawio) + PIL preview."""
from __future__ import annotations

import html
import os

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
DRAWIO_PATH = os.path.join(OUT_DIR, "service-architecture-bw.drawio")
PREVIEW_PATH = os.path.join(OUT_DIR, "service-architecture-bw.preview.png")

W, H = 2600, 1700

TITLE = "Hardware DataBase · 服务架构框图"
SUBTITLE = "当前分支 · 同步服务 / 异步 Worker / 共享平台 / 外部依赖 · 黑白框线图"

TOP_BOXES = {
    "users": (1050, 38, 500, 60, "部门员工 employee（内容） · 系统管理员 system_admin（治理）", "统一入口，角色与 KB 权限由服务端判定"),
    "spa": (1020, 123, 560, 72, "React 18 + TypeScript SPA · Vite 6 · Tailwind 4", "知识库问答 · 文件工作台 · Wiki/资产 · 记忆 · 扩展 · 管理后台"),
    "edge": (1060, 220, 480, 60, "nginx（生产 :8080） · Vite proxy（开发 :5176）", "HTTPS / Bearer · SSE(fetch stream) · multipart 上传"),
}

API_BOX = (760, 310, 1080, 140)
API_TITLE = "FastAPI API 服务 · /api/v1（唯一后端入口）"
API_SUB = "鉴权依赖 · 请求准入 · 统一错误映射 · OTel instrumentation · SSE / multipart"
API_CHIPS = [
    ("认证 / 用户 / 部门", "/login · /whoami · /users · /departments"),
    ("知识库 / 文件 / 解析", "/kbs · /files · /parse-tasks"),
    ("会话 / Turn / SSE", "/conversations · /turns · /events"),
    ("导出 / 附件", "/exports · /artifacts · /attachments"),
    ("Wiki / 资产 / 记忆", "/wiki · /doc-assets · /memories"),
    ("评估 / 质量 / 成本", "/evaluation · /quality · /costs"),
    ("扩展 / 沙箱", "/plugins/mcp · /sandbox"),
    ("治理 / 日志 / 配置", "/governance · /logs · /config"),
]

COL_X = [60, 480, 900, 1320, 1740, 2160]
COL_W = 360
COL_Y = 530
COL_H = 700
HEADER_H = 44
SERVICE_X_INSET = 20
SERVICE_W = 320
SERVICE_Y0 = 590

COLUMNS = [
    {
        "title": "查询与 Agent 服务",
        "sub": "同步链路 · SSE 流式",
        "footer": "事件 / turn 账本 → PostgreSQL · Redis",
        "services": [
            ("Conversation & Turn Service", [
                "创建 turn / 启动 / 取消",
                "turn 状态机 · 租约 + fencing",
                "chat_turn_events（seq 单调）",
                "SSE after=seq 断点续传",
            ]),
            ("Query Orchestrator", [
                "意图识别 · route / tool 白名单",
                "source_scope / 附件快照",
                "capability 过滤 · backend authority",
            ]),
            ("Agent Runtime", [
                "deepagents create_deep_agent",
                "ReAct 循环 · recursion_limit",
                "prompt 装配 · fail-open",
                "tool_started / tool_result 事件",
            ]),
            ("Retrieval Tool Adapters", [
                "document_search / kb_grep",
                "evidence_read / circuit_search",
                "spreadsheet_* / conversation_search",
                "wiki_search / memory_search / 附件",
            ]),
            ("Model Gateway", [
                "model_factory（多 profile 路由）",
                "chat_model_runtime",
                "llm_governor 并发闸门 / 队列",
                "cost sink / usage ledger",
            ]),
        ],
        "deps": [
            ("LLM 服务（外部）", ["Ollama / OpenAI-compatible", "profile · timeout · retry"]),
            ("RAGFlow（外部）", ["document_search 检索", "dataset · chunk · retrieval"]),
        ],
    },
    {
        "title": "摄取与索引服务",
        "sub": "上传 · 解析 · 结构化索引",
        "footer": "解析任务账本 → PostgreSQL",
        "services": [
            ("Document Manager", [
                "上传 / 删除 / 列表 / 下载",
                "KB scope + 权限校验",
                "归档 + 内容 SHA-256 去重",
            ]),
            ("Ingestion Orchestrator", [
                "route_file → PipelineSpec",
                "source_group → dataset_kind",
                "parse task 生命周期",
                "handler dispatch",
            ]),
            ("Pipeline Registry", [
                "document_rag（doc/docx/pdf）",
                "spreadsheet（.xlsx）",
                "circuit_design（.edf/.edif）",
                "external_conversation",
            ]),
            ("Pipeline Handlers", [
                "RAGFlowDocumentHandler",
                "SpreadsheetPipelineHandler",
                "CircuitPipelineHandler",
                "ExternalConversationHandler",
            ]),
            ("Index Services", [
                "TableIndexStore（每 KB SQLite）",
                "CircuitIndexService / QueryEngine",
                "ExternalConversationQueryEngine",
                "FTS5 pointer index",
            ]),
        ],
        "deps": [
            ("RAGFlow（外部）", ["文档解析 / 索引", "governance · design"]),
            ("文件存储", ["table_indexes · circuits", "external_conversations · archives"]),
        ],
    },
    {
        "title": "治理与资产服务",
        "sub": "权限 · Wiki · 记忆 · 附件",
        "footer": "审计 / Query Trace → PostgreSQL",
        "services": [
            ("Auth & RBAC Service", [
                "login / token / whoami",
                "users / departments / KB",
                "system_admin / employee",
                "audit_events（fail-soft）",
            ]),
            ("KB Scope & Sharing", [
                "department_id + kb_name",
                "默认私有 · owner 共享",
                "system_admin 内容访问拒绝",
            ]),
            ("Wiki & Document Assets", [
                "wiki pages / revisions / links",
                "doc assets / versions / events",
                "hardware assets / candidates",
            ]),
            ("Memory Service", [
                "memory catalog / consent",
                "revision / projection",
                "LangMem candidate → verified",
                "outbox / retention",
            ]),
            ("Attachment Service", [
                "OCR / PDF reflow",
                "docx / xlsx / pdf revision",
                "visual cache / embeddings",
                "FTS5 / cleanup outbox",
            ]),
        ],
        "deps": [
            ("PostgreSQL（pgvector）", ["auth / control plane", "turns · audit · memory"]),
            ("文件存储", ["wiki · doc_assets", "attachments · projects"]),
        ],
    },
    {
        "title": "交付与质量服务",
        "sub": "导出 · 评估 · 反馈 · 成本",
        "footer": "任务队列 / 租约 → PostgreSQL",
        "services": [
            ("Export Service", [
                "ResultSnapshot（不可变）",
                "result_export_jobs / outbox",
                "MD/XLSX/DOCX/PDF/PPTX",
                "artifact preview / download",
            ]),
            ("Evaluation Service", [
                "RAGAS validate/collect/score/run",
                "阈值门禁 · 历史对比",
                "snapshot.jsonl / summary.json",
                "裁判 LLM / RAGFlow 检索",
            ]),
            ("Quality & Feedback", [
                "反馈工单 / 评分",
                "复核共享 / 审批",
                "consent / audit",
            ]),
            ("Cost Ledger", [
                "llm_usage_events",
                "price versions / budgets",
                "部门成本账本",
            ]),
            ("Notification Service", [
                "notifications 表",
                "30s 轮询 / 可选 webhook",
                "附件审批 / 任务完成",
                "fail-soft",
            ]),
        ],
        "deps": [
            ("PostgreSQL（pgvector）", ["export / cost / quality", "snapshots · ledger"]),
            ("文件存储", ["exports / evaluations", "artifact · report"]),
        ],
    },
    {
        "title": "扩展与沙箱服务",
        "sub": "MCP · Skill · 受控执行",
        "footer": "审批 / 任务账本 → PostgreSQL",
        "services": [
            ("Extension Registry", [
                "MCP server 登记 / 测试",
                "enable / disable",
                "department scope",
                "employee subscription",
            ]),
            ("Capability Resolver", [
                "external_tool.invoke",
                "write_tools 白名单",
                "user consent / 二次校验",
                "audit / quota / governor",
            ]),
            ("Skill Catalog", [
                "text skill / ZIP",
                "draft → reviewed → tested → enabled",
                "fixed bundle binding",
                "untrusted prompt data",
            ]),
            ("Sandbox Client", [
                "task ledger（PG）/ idempotency",
                "lease / fencing / CAS",
                "cancel / timeout / reconcile",
                "artifact broker client",
            ]),
            ("Artifact Broker", [
                "stage → scan → commit",
                "opaque artifact ID",
                "ACL / hash / manifest",
                "retention",
            ]),
        ],
        "deps": [
            ("MCP Gateway", ["transport / session", "tool allowlist · schema"]),
            ("Sandbox Runner + Broker", ["唯一 OCI 控制权", "stage → scan → commit"]),
        ],
    },
    {
        "title": "异步 Worker 服务",
        "sub": "消费 PG 任务账本 + Redis 事件",
        "footer": "外部调用复用 LLM / RAGFlow",
        "worker": True,
        "services": [
            ("worker-chat", ["claim turn · _run_turn", "Agent loop / tool 调用", "SSE 事件写入"]),
            ("worker-parse", ["parse task claim", "表格 / RAGFlow 状态", "失败重试 / 租约"]),
            ("worker-export", ["export job claim", "受控渲染 + artifact", "保留期清理"]),
            ("worker-memory", ["reflection / projection", "consent / outbox", "retention / reconcile"]),
            ("worker-evaluation", ["RAGAS run", "score / report", "阈值门禁"]),
            ("worker-quality", ["反馈工单 workflow", "质量任务", "审计"]),
        ],
        "deps": [
            ("PostgreSQL（pgvector）", ["任务账本 / 租约", "events · receipts · quota"]),
            ("Redis", ["SSE Stream / cancel", "distributed admission"]),
        ],
    },
]

OBS_BOXES = [
    ("OTel SDK", ["FastAPI / HTTPX 自动埋点", "Agent / LLM / Tool / Worker"]),
    ("OTel Collector", ["OTLP gRPC :4317", "HTTP :4318 · batch"]),
    ("Prometheus · Tempo · Loki", ["metrics · traces · logs", "15s scrape / alert rules"]),
    ("Grafana · Phoenix", ["Overview Dashboard", "AI / RAG trace"]),
]


def esc(v: str) -> str:
    return html.escape(v, quote=True)


cells: list[str] = []


def add_vertex(cid: str, value: str, style: str, x: float, y: float, w: float, h: float) -> None:
    cells.append(
        f'<mxCell id="{cid}" value="{esc(value)}" style="{style}" vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
    )


def add_text(cid: str, value: str, x: float, y: float, w: float, h: float,
             size: float = 12, color: str = "#000000", bold: bool = False,
             align: str = "left", valign: str = "middle") -> None:
    style = (
        f"text;html=1;strokeColor=none;fillColor=none;align={align};verticalAlign={valign};"
        f"whiteSpace=wrap;fontSize={size};fontColor={color};"
    )
    if bold:
        style += "fontStyle=1;"
    add_vertex(cid, value, style, x, y, w, h)


def add_box(cid: str, x: float, y: float, w: float, h: float, stroke: float = 1.3) -> None:
    add_vertex(
        cid, "",
        f"rounded=0;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#000000;strokeWidth={stroke};",
        x, y, w, h,
    )


def add_label_box(cid: str, title: str, lines: list[str], x: float, y: float, w: float, h: float,
                  title_size: float = 12.5, body_size: float = 10.5, pad: float = 8) -> None:
    body = "<br>".join(f"<span style='color:#000000'>•</span> {esc(ln)}" for ln in lines)
    value = (
        "<div style='text-align:left'>"
        f"<div style='font-size:{title_size}px;font-weight:700;color:#000000;line-height:1.2;margin-bottom:4px'>{esc(title)}</div>"
        f"<div style='font-size:{body_size}px;color:#222222;line-height:1.45'>{body}</div>"
        "</div>"
    )
    add_vertex(
        cid, value,
        "rounded=0;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#000000;"
        f"strokeWidth=1.2;align=left;verticalAlign=top;spacing={pad};spacingTop={pad - 1};",
        x, y, w, h,
    )


def add_hline(cid: str, x: float, y: float, w: float, thickness: float = 1.5) -> None:
    add_vertex(cid, "", "rounded=0;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=none;",
               x, y - thickness / 2, w, thickness)


def add_vline(cid: str, x: float, y: float, h: float, thickness: float = 1.5) -> None:
    add_vertex(cid, "", "rounded=0;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=none;",
               x - thickness / 2, y, thickness, h)


def add_arrow(cid: str, src: str, dst: str, dashed: bool = False, label: str = "") -> None:
    style = (
        "edgeStyle=none;rounded=0;html=1;exitX=0.5;exitY=1;exitDx=0;exitDy=0;"
        "entryX=0.5;entryY=0;entryDx=0;entryDy=0;endArrow=block;endSize=7;endFill=1;"
        "strokeWidth=1.5;strokeColor=#000000;"
    )
    if dashed:
        style += "dashed=1;dashPattern=4 3;"
    cells.append(
        f'<mxCell id="{cid}" value="{esc(label)}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


def add_point_arrow(cid: str, x: float, y1: float, y2: float, dashed: bool = False) -> None:
    style = (
        "edgeStyle=none;rounded=0;html=1;endArrow=block;endSize=7;endFill=1;"
        "strokeWidth=1.5;strokeColor=#000000;"
    )
    if dashed:
        style += "dashed=1;dashPattern=4 3;"
    cells.append(
        f'<mxCell id="{cid}" style="{style}" edge="1" parent="1">'
        f'<mxGeometry relative="1" as="geometry"><mxPoint x="{x}" y="{y1}" as="sourcePoint"/>'
        f'<mxPoint x="{x}" y="{y2}" as="targetPoint"/></mxGeometry></mxCell>'
    )


# title + legend
add_text("title", TITLE, 40, 26, 1700, 38, size=28, bold=True)
add_text("subtitle", SUBTITLE, 42, 68, 1700, 24, size=12, color="#333333")
add_hline("legend_solid", 1980, 52, 40)
add_text("legend_solid_label", "同步调用", 2030, 40, 200, 24, size=10.5, color="#222222")
for i in range(5):
    add_hline(f"legend_dash_{i}", 1980 + i * 9, 84, 5, thickness=1.5)
add_text("legend_dash_label", "异步任务 / 横切", 2030, 72, 300, 24, size=10.5, color="#222222")

# top boxes
for key, (x, y, w, h, title, sub) in TOP_BOXES.items():
    add_box(f"top_{key}", x, y, w, h, stroke=1.4)
    add_text(f"top_{key}_title", title, x + 12, y + 8, w - 24, 20, size=12.5, bold=True)
    add_text(f"top_{key}_sub", sub, x + 12, y + h - 26, w - 24, 18, size=9.8, color="#333333")

# principles
add_box("principles", 60, 135, 680, 155, stroke=1.2)
add_text("principles_title", "核心原则", 76, 145, 300, 22, size=13, bold=True)
PRINCIPLES = [
    "同步 / 异步分离：HTTP 不等待模型，SSE 订阅结果",
    "RAGFlow 固定文档后端；PG 账本 + Redis 实时面",
    "工具 fail-open；权限 / 作用域 fail-closed",
    "system_admin 只治理，不访问 KB 内容",
    "共享依赖重复标注，生产部署为同一实例",
]
for i, line in enumerate(PRINCIPLES):
    add_text(f"principles_{i}", "• " + line, 78, 174 + i * 20, 640, 18, size=10, color="#222222")

add_arrow("arr_users_spa", "top_users", "top_spa")
add_arrow("arr_spa_edge", "top_spa", "top_edge")
add_arrow("arr_edge_api", "top_edge", "api")

# API box
ax, ay, aw, ah = API_BOX
add_box("api", ax, ay, aw, ah, stroke=2.0)
add_text("api_title", API_TITLE, ax + 18, ay + 8, aw - 36, 22, size=15, bold=True)
add_text("api_sub", API_SUB, ax + 18, ay + 31, aw - 36, 18, size=9.8, color="#333333")
for i, (title, path) in enumerate(API_CHIPS):
    row, col = divmod(i, 4)
    cx = ax + 20 + col * 253
    cy = ay + 54 + row * 42
    add_box(f"api_chip_{i}", cx, cy, 245, 36, stroke=0.9)
    add_text(f"api_chip_{i}_t", title, cx + 4, cy + 3, 237, 15, size=9.4, bold=True, align="center")
    add_text(f"api_chip_{i}_p", path, cx + 4, cy + 19, 237, 14, size=8.6, color="#333333", align="center")

# API -> service bus
BUS_Y = 495
add_vline("api_trunk", 1300, ay + ah, BUS_Y - (ay + ah))
add_hline("api_bus", COL_X[0] + COL_W / 2, BUS_Y, COL_X[-1] + COL_W / 2 - (COL_X[0] + COL_W / 2))

# columns
for ci, col in enumerate(COLUMNS, start=1):
    x = COL_X[ci - 1]
    center = x + COL_W / 2
    add_box(f"col_{ci}", x, COL_Y, COL_W, COL_H, stroke=1.8)
    add_vertex(
        f"col_{ci}_hdr", "",
        "rounded=0;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=#000000;strokeWidth=1.5;",
        x, COL_Y, COL_W, HEADER_H,
    )
    add_text(f"col_{ci}_title", col["title"], x + 12, COL_Y + 5, COL_W - 24, 20,
             size=14, color="#FFFFFF", bold=True)
    add_text(f"col_{ci}_sub", col["sub"], x + 12, COL_Y + 24, COL_W - 24, 16,
             size=9.2, color="#E6E6E6")
    add_point_arrow(f"bus_drop_{ci}", center, BUS_Y, COL_Y, dashed=bool(col.get("worker")))

    services = col["services"]
    if col.get("worker"):
        sh, gap = 92, 10
    else:
        sh, gap = 112, 12
    for si, (title, lines) in enumerate(services):
        sy = SERVICE_Y0 + si * (sh + gap)
        add_label_box(f"svc_{ci}_{si}", title, lines, x + SERVICE_X_INSET, sy, SERVICE_W, sh,
                      title_size=12.5 if not col.get("worker") else 12,
                      body_size=10.4 if not col.get("worker") else 9.8, pad=8)
    add_text(f"col_{ci}_footer", col["footer"], x + 10, 1202 if not col.get("worker") else 1196,
             COL_W - 20, 14, size=8.6, color="#333333", align="center")

    # dependency split
    deps = col["deps"]
    dep_w = (COL_W - 10 * (len(deps) - 1)) / len(deps)
    dep_centers = [x + dep_w / 2 + di * (dep_w + 10) for di in range(len(deps))]
    add_vline(f"dep_trunk_{ci}", center, COL_Y + COL_H, 40)
    if len(deps) > 1:
        add_hline(f"dep_split_{ci}", min(dep_centers), COL_Y + COL_H + 40,
                  max(dep_centers) - min(dep_centers))
    for di, (dtitle, dlines) in enumerate(deps):
        dcx = dep_centers[di]
        add_point_arrow(f"dep_drop_{ci}_{di}", dcx, COL_Y + COL_H + 40, 1300)
        dx = x + di * (dep_w + 10)
        add_label_box(f"dep_{ci}_{di}", dtitle, dlines, dx, 1300, dep_w, 100,
                      title_size=11.2, body_size=9.5, pad=7)

# process boundary between API-hosted services and independent workers
cells.append(
    '<mxCell id="proc_boundary" style="edgeStyle=none;rounded=0;html=1;endArrow=none;'
    'strokeWidth=1.2;strokeColor=#000000;dashed=1;dashPattern=4 3;" edge="1" parent="1">'
    '<mxGeometry relative="1" as="geometry"><mxPoint x="2130" y="530" as="sourcePoint"/>'
    '<mxPoint x="2130" y="1400" as="targetPoint"/></mxGeometry></mxCell>'
)
add_text("proc_boundary_label", "生产进程边界", 2070, 509, 120, 14, size=8.2, align="center")

# notes
add_text("note_shared",
         "注：共享依赖在多个服务栈下重复出现，表示该服务对其有直接依赖；生产部署中 PostgreSQL / Redis / 文件存储为同一实例。",
         60, 1420, 1300, 20, size=9.5, color="#333333")
add_text("note_boundary",
         "边界：HDB API / Worker 不启动第三方进程、不接触 OCI/Docker socket；Sandbox Runner 是唯一运行时控制方。",
         1500, 1420, 1020, 20, size=9.5, color="#333333")

# observability bar
OX, OY, OW, OH = 60, 1470, 2460, 120
add_box("obs", OX, OY, OW, OH, stroke=1.6)
add_text("obs_title", "可观测性（横切所有服务，fail-soft）", OX + 16, OY + 8, 900, 20,
         size=12.5, bold=True)
obs_w = [580, 420, 620, 560]
obs_x = [OX + 20, OX + 640, OX + 1100, OX + 1760]
for i, ((title, lines), bx, bw) in enumerate(zip(OBS_BOXES, obs_x, obs_w)):
    by = OY + 38
    add_box(f"obs_box_{i}", bx, by, bw, 68, stroke=1.1)
    add_text(f"obs_box_{i}_t", title, bx + 10, by + 6, bw - 20, 18, size=10.8, bold=True)
    add_text(f"obs_box_{i}_b", " · ".join(lines), bx + 10, by + 27, bw - 20, 32,
             size=9.6, color="#333333")
    if i < 3:
        add_hline(f"obs_link_{i}", bx + bw + 2, by + 34, obs_x[i + 1] - (bx + bw) - 4)
        add_vertex(
            f"obs_arrow_{i}", "",
            "shape=triangle;direction=east;fillColor=#000000;strokeColor=#000000;",
            obs_x[i + 1] - 8, by + 30, 8, 8,
        )

add_text("footer", "依据：README.md · wiki/02-architecture.md · src/ 当前分支 ｜ 生成：2026-10-30",
         60, 1640, 1400, 18, size=8.8, color="#666666")

# --------------------------------------------------------------------------------
xml = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mxfile host="app.diagrams.net" modified="2026-10-30T12:00:00.000Z" agent="OpenCode" version="24.7.17" type="device">\n'
    '  <diagram id="hdb-service-architecture-bw" name="服务架构框图（黑白）">\n'
    f'    <mxGraphModel dx="0" dy="0" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{W}" pageHeight="{H}" background="#FFFFFF" math="0" shadow="0">\n'
    "      <root>\n"
    '        <mxCell id="0"/>\n'
    '        <mxCell id="1" parent="0"/>\n'
    + "".join(f"        {cell}\n" for cell in cells)
    + "      </root>\n"
    "    </mxGraphModel>\n"
    "  </diagram>\n"
    "</mxfile>\n"
)
with open(DRAWIO_PATH, "w", encoding="utf-8") as f:
    f.write(xml)
print(f"wrote {DRAWIO_PATH} ({len(xml):,} bytes, {len(cells)} cells)")

# --------------------------------------------------------------------------------
# PIL preview
CJK_PATH = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
LATIN_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def is_cjk(ch: str) -> bool:
    return ord(ch) >= 0x2E80 or ch in "（）：；，。！？、《》【】“”‘’…—"


def split_runs(text: str):
    runs, cur, cur_cjk = [], "", None
    for ch in text:
        c = is_cjk(ch)
        if cur_cjk is None or c == cur_cjk:
            cur += ch
        else:
            runs.append((cur, cur_cjk))
            cur = ch
        cur_cjk = c
    if cur:
        runs.append((cur, cur_cjk))
    return runs


def font_for(ch: str, size: int, bold: bool = False):
    if is_cjk(ch):
        return ImageFont.truetype(CJK_PATH, size=size)
    return ImageFont.truetype(BOLD_PATH if bold else LATIN_PATH, size=size)


def mlen(draw, text: str, size: int) -> float:
    return sum(draw.textlength(run, font=font_for(run[0], size)) for run, _ in split_runs(text))


def draw_mixed(draw, xy, text: str, size: int, fill: str, bold: bool = False) -> None:
    x, y = xy
    for run, _ in split_runs(text):
        f = font_for(run[0], size, bold)
        draw.text((x, y), run, font=f, fill=fill)
        x += draw.textlength(run, font=f)


def draw_center(draw, cx, y, text, size, fill, bold=False):
    draw_mixed(draw, (cx - mlen(draw, text, size) / 2, y), text, size, fill, bold)


def wrap_mixed(draw, text: str, size: int, max_w: float) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        cand = cur + ch
        if mlen(draw, cand, size) <= max_w or not cur:
            cur = cand
        else:
            idx = cur.rfind(" ")
            if idx > 0:
                lines.append(cur[:idx])
                cur = cur[idx + 1:] + ch
            else:
                lines.append(cur)
                cur = ch
    if cur:
        lines.append(cur)
    return lines


def dashed(draw, x1, y1, x2, y2, fill="#000000", width=2, dash=6, gap=4):
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length == 0:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    pos = 0
    while pos < length:
        end = min(pos + dash, length)
        draw.line([x1 + ux * pos, y1 + uy * pos, x1 + ux * end, y1 + uy * end], fill=fill, width=width)
        pos += dash + gap


img = Image.new("RGB", (W, H), "#FFFFFF")
draw = ImageDraw.Draw(img)

# title + legend
draw_mixed(draw, (40, 26), TITLE, 27, "#000000", bold=True)
draw_mixed(draw, (42, 68), SUBTITLE, 11, "#333333")
draw.line([1980, 52, 2020, 52], fill="#000000", width=2)
draw_mixed(draw, (2030, 40), "同步调用", 10, "#222222")
dashed(draw, 1980, 84, 2024, 84, width=2)
draw_mixed(draw, (2030, 72), "异步任务 / 横切", 10, "#222222")

# top boxes
for key, (x, y, w, h, title, sub) in TOP_BOXES.items():
    draw.rectangle([x, y, x + w, y + h], fill="#FFFFFF", outline="#000000", width=2)
    draw_mixed(draw, (x + 12, y + 7), title, 12, "#000000", bold=True)
    draw_mixed(draw, (x + 12, y + h - 25), sub, 9, "#333333")

# principles (preview)
draw.rectangle([60, 135, 740, 290], fill="#FFFFFF", outline="#000000", width=1)
draw_mixed(draw, (76, 145), "核心原则", 12, "#000000", bold=True)
for i, line in enumerate(PRINCIPLES):
    draw_mixed(draw, (78, 174 + i * 20), "• " + line, 9.8, "#222222")

# top arrows
for x, y1, y2 in [(1300, 98, 123), (1300, 195, 220), (1300, 280, 310)]:
    draw.line([x, y1, x, y2 - 7], fill="#000000", width=2)
    draw.polygon([(x - 4, y2 - 8), (x, y2), (x + 4, y2 - 8)], fill="#000000")

# API
ax, ay, aw, ah = API_BOX
draw.rectangle([ax, ay, ax + aw, ay + ah], fill="#FFFFFF", outline="#000000", width=2)
draw_mixed(draw, (ax + 18, ay + 8), API_TITLE, 14, "#000000", bold=True)
draw_mixed(draw, (ax + 18, ay + 31), API_SUB, 9, "#333333")
for i, (title, path) in enumerate(API_CHIPS):
    row, col = divmod(i, 4)
    cx = ax + 20 + col * 253
    cy = ay + 54 + row * 42
    draw.rectangle([cx, cy, cx + 245, cy + 36], fill="#FFFFFF", outline="#000000", width=1)
    draw_center(draw, cx + 122, cy + 3, title, 9, "#000000", bold=True)
    draw_center(draw, cx + 122, cy + 19, path, 7.5, "#333333")

# API bus
BUS_Y = 495
draw.line([1300, ay + ah, 1300, BUS_Y], fill="#000000", width=2)
draw.line([COL_X[0] + COL_W / 2, BUS_Y, COL_X[-1] + COL_W / 2, BUS_Y], fill="#000000", width=2)

# columns
for ci, col in enumerate(COLUMNS, start=1):
    x = COL_X[ci - 1]
    center = x + COL_W / 2
    draw.rectangle([x, COL_Y, x + COL_W, COL_Y + COL_H], fill="#FFFFFF", outline="#000000", width=2)
    draw.rectangle([x, COL_Y, x + COL_W, COL_Y + HEADER_H], fill="#000000", outline="#000000")
    draw_mixed(draw, (x + 12, COL_Y + 5), col["title"], 13, "#FFFFFF", bold=True)
    draw_mixed(draw, (x + 12, COL_Y + 24), col["sub"], 8.5, "#E6E6E6")
    # bus drop
    if col.get("worker"):
        dashed(draw, center, BUS_Y, center, COL_Y, width=2)
    else:
        draw.line([center, BUS_Y, center, COL_Y - 7], fill="#000000", width=2)
    draw.polygon([(center - 4, COL_Y - 8), (center, COL_Y), (center + 4, COL_Y - 8)], fill="#000000")

    services = col["services"]
    sh, gap = (92, 10) if col.get("worker") else (112, 12)
    for si, (title, lines) in enumerate(services):
        sy = SERVICE_Y0 + si * (sh + gap)
        sx = x + SERVICE_X_INSET
        draw.rectangle([sx, sy, sx + SERVICE_W, sy + sh], fill="#FFFFFF", outline="#000000", width=1)
        draw_mixed(draw, (sx + 8, sy + 7), title, 12 if not col.get("worker") else 11.5, "#000000", bold=True)
        by = sy + 27
        for ln in lines:
            draw.ellipse([sx + 9, by + 4, sx + 12, by + 7], fill="#000000")
            for j, sub in enumerate(wrap_mixed(draw, ln, 9.5, SERVICE_W - 26)):
                draw_mixed(draw, (sx + 17, by), sub, 9.5, "#222222")
                by += 13
            by += 1
    draw_center(draw, center, 1202 if not col.get("worker") else 1196, col["footer"], 8, "#333333")

    deps = col["deps"]
    dep_w = (COL_W - 10 * (len(deps) - 1)) / len(deps)
    dep_centers = [x + dep_w / 2 + di * (dep_w + 10) for di in range(len(deps))]
    draw.line([center, COL_Y + COL_H, center, COL_Y + COL_H + 40], fill="#000000", width=2)
    if len(deps) > 1:
        draw.line([min(dep_centers), COL_Y + COL_H + 40, max(dep_centers), COL_Y + COL_H + 40], fill="#000000", width=2)
    for di, (dtitle, dlines) in enumerate(deps):
        dcx = dep_centers[di]
        draw.line([dcx, COL_Y + COL_H + 40, dcx, 1292], fill="#000000", width=2)
        draw.polygon([(dcx - 4, 1292), (dcx, 1300), (dcx + 4, 1292)], fill="#000000")
        dx = x + di * (dep_w + 10)
        draw.rectangle([dx, 1300, dx + dep_w, 1400], fill="#FFFFFF", outline="#000000", width=1)
        draw_mixed(draw, (dx + 8, 1307), dtitle, 10.5, "#000000", bold=True)
        by = 1327
        for ln in dlines:
            for j, sub in enumerate(wrap_mixed(draw, ln, 8.5, dep_w - 16)):
                draw_mixed(draw, (dx + 8, by), sub, 8.5, "#222222")
                by += 12
            by += 2

# process boundary
dashed(draw, 2130, 530, 2130, 1400, width=1, dash=5, gap=4)
draw_center(draw, 2130, 509, "生产进程边界", 8, "#000000")

# notes
draw_mixed(draw, (60, 1420), "注：共享依赖在多个服务栈下重复出现，表示该服务对其有直接依赖；生产部署中 PostgreSQL / Redis / 文件存储为同一实例。", 9, "#333333")
draw_mixed(draw, (1500, 1420), "边界：HDB API / Worker 不启动第三方进程、不接触 OCI/Docker socket；Sandbox Runner 是唯一运行时控制方。", 9, "#333333")

# observability
OX, OY, OW, OH = 60, 1470, 2460, 120
draw.rectangle([OX, OY, OX + OW, OY + OH], fill="#FFFFFF", outline="#000000", width=2)
draw_mixed(draw, (OX + 16, OY + 8), "可观测性（横切所有服务，fail-soft）", 12, "#000000", bold=True)
obs_w = [580, 420, 620, 560]
obs_x = [OX + 20, OX + 640, OX + 1100, OX + 1760]
for i, ((title, lines), bx, bw) in enumerate(zip(OBS_BOXES, obs_x, obs_w)):
    by = OY + 38
    draw.rectangle([bx, by, bx + bw, by + 68], fill="#FFFFFF", outline="#000000", width=1)
    draw_mixed(draw, (bx + 10, by + 6), title, 10.5, "#000000", bold=True)
    draw_mixed(draw, (bx + 10, by + 27), " · ".join(lines), 9.0, "#333333")
    if i < 3:
        draw.line([bx + bw + 2, by + 34, obs_x[i + 1] - 10, by + 34], fill="#000000", width=2)
        draw.polygon([(obs_x[i + 1] - 10, by + 30), (obs_x[i + 1] - 2, by + 34), (obs_x[i + 1] - 10, by + 38)], fill="#000000")

draw_mixed(draw, (60, 1640), "依据：README.md · wiki/02-architecture.md · src/ 当前分支 ｜ 生成：2026-10-30", 8.5, "#666666")

img.save(PREVIEW_PATH)
print(f"wrote {PREVIEW_PATH} ({W}x{H})")
