#!/usr/bin/env python3
"""Generate the Hardware DataBase layered architecture .drawio + a PIL preview."""
from __future__ import annotations

import html
import os

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
DRAWIO_PATH = os.path.join(OUT_DIR, "current-system-architecture.drawio")
PREVIEW_PATH = os.path.join(OUT_DIR, "preview.png")

W, H = 2600, 2240
LANES = [
    ("l1", 270, 380, "L1 · 交互入口", "React 18 · TypeScript · Vite · Tailwind 4", "#2563EB", "#F5F9FF", "#DBEAFE"),
    ("l2", 680, 380, "L2 · API 层", "FastAPI · /api/v1 · 唯一后端入口", "#7C3AED", "#F8F6FF", "#E9D5FF"),
    ("l3", 1090, 410, "L3 · 编排 · 领域服务", "AppPipeline 单例 + 路由构造域服务", "#059669", "#F4FDF7", "#D1FAE5"),
    ("l4", 1530, 440, "L4 · 运行时 · 引擎", "deepagents · 摄取 · Worker · 模型/资源 · 导出", "#EA580C", "#FFF9F2", "#FED7AA"),
    ("l5", 2000, 520, "L5 · 数据 · 外部依赖", "PostgreSQL · Redis · storage · RAGFlow · LLM", "#334155", "#F8FAFC", "#CBD5E1"),
]

ROWS = [
    {
        "num": "01", "name": "知识问答", "tag": "核心链路",
        "accent": "#2563EB", "fill": "#EFF6FF", "stroke": "#BFDBFE", "text": "#1D4ED8",
        "cells": [
            ("ChatPage · useKbChat", [
                "SSE 实时轨迹 / 引用 / 脚注",
                "刷新、切页不丢 turn",
                "回答内「导出结果」菜单",
                "附件预检（不可信上下文）",
            ]),
            ("Turn 执行模型", [
                "POST /conversations/{id}/turns",
                "POST /turns/{id}/start",
                "GET /turns/{id}/events（SSE）",
                "after=seq 断点续传 · cancel",
            ]),
            ("AppPipeline.query", [
                "ConversationOrchestrator.plan",
                "worker-chat 执行 _run_turn",
                "turn 租约 + fencing_token",
                "→ MultiSourceAgentRunner",
                "权限 / 工具白名单由后端定",
            ]),
            ("deepagents ReAct 循环", [
                "create_deep_agent（LangGraph）",
                "ToolRuntime：作用域/证据/预算",
                "工具：文档/表格/电路/对话/Wiki",
                "记忆/附件 + evidence_read 证据提升",
                "agent_events 契约 · fail-open",
                "model_factory → LLM governor",
            ]),
            ("问答链路依赖", [
                "RAGFlow：document_search",
                "派生索引：表格 / 电路 / 外部对话 / FTS",
                "PG：turns / events / checkpoints",
                "Redis：跨进程 SSE / 取消快进",
                "LLM：Ollama / OpenAI 兼容",
            ]),
        ],
    },
    {
        "num": "02", "name": "摄取与索引", "tag": "多源 Pipeline",
        "accent": "#059669", "fill": "#ECFDF5", "stroke": "#A7F3D0", "text": "#047857",
        "cells": [
            ("KbFilesPage · 文件工作台", [
                "multipart 上传（≤512 MiB）",
                "解析进度 / 失败重试",
                "文件、归档、解析任务面板",
                "附件与 Revision 入口",
            ]),
            ("知识库与文件路由", [
                "POST /kbs/{kb}/files",
                "GET /kbs/{kb}/parse-tasks",
                "GET /kbs/{kb}/files",
                "DELETE /files · 权限校验",
            ]),
            ("DocumentManager → RAGFlowBackend", [
                "IngestionOrchestrator",
                "SHA-256 去重 + 归档",
                "source_group → dataset_kind",
                "source_group_router 权重过滤",
            ]),
            ("PipelineRegistry · 4 Handler", [
                "RAGFlow / Spreadsheet",
                "Circuit / ExternalConversation",
                "10 类 source_group 分类",
                "parse task 生命周期",
            ]),
            ("数据集与派生索引", [
                "RAGFlow governance / design",
                "table_indexes.db（每 KB）",
                "circuits/{kb}/{design_id}/",
                "external_conversations/ · archives/",
            ]),
        ],
    },
    {
        "num": "03", "name": "权限与治理", "tag": "内容隔离",
        "accent": "#D97706", "fill": "#FFFBEB", "stroke": "#FDE68A", "text": "#B45309",
        "cells": [
            ("LoginPage · 管理后台", [
                "用户 / 部门 / KB 挂载",
                "日志中心 · 治理面板",
                "系统配置（热重载）",
                "RAGAS 评估（仅管理员）",
            ]),
            ("认证与治理路由", [
                "/login · /whoami · /users",
                "/departments · /kbs/{name}/assign",
                "/logs/* · /governance/* · /config",
                "/system/status · /health/dependencies",
            ]),
            ("AuthService · KbScope", [
                "Bearer → AuthUser → RequestContext",
                "has_kb_permission（KB + 部门）",
                "AppLogService：审计链 / Trace",
                "路由直接构造域服务",
            ]),
            ("两级角色 + KB 共享", [
                "system_admin：只碰元数据",
                "employee：部门内容角色",
                "KB 默认私有 + owner 共享",
                "sysadmin 内容访问恒拒绝",
            ]),
            ("权威账本", [
                "auth.db：用户/部门/KB/审计",
                "audit_events / query_traces",
                "retrieved_evidence",
                "PG auth mirror（混合形态）",
            ]),
        ],
    },
    {
        "num": "04", "name": "资产沉淀与附件", "tag": "Wiki · Assets · Memory",
        "accent": "#7C3AED", "fill": "#F5F3FF", "stroke": "#DDD6FE", "text": "#6D28D9",
        "cells": [
            ("Wiki · 资产 · 记忆 · 附件", [
                "WikiPage / DocumentAssetsPage",
                "MemoryPage · 聊天附件 / 修订",
                "Wiki 搜索与版本对比",
                "consent / revoke 交互",
            ]),
            ("资产与记忆路由", [
                "/kbs/{kb}/wiki · /doc-assets · /assets",
                "/structured · /memories",
                "/conversations/{id}/attachments",
                "/notifications",
            ]),
            ("域服务（路由构造）", [
                "WikiService · DocumentAssetService",
                "AssetService · MemoryService",
                "AttachmentService",
                "共享 AUTH_DB_PATH",
            ]),
            ("沉淀与抽取", [
                "Wiki 蒸馏 + 一跳链接",
                "文档资产版本 / revision",
                "LangMem / memory worker：Candidate → Verified",
                "附件 OCR · 版本 · 视觉缓存",
            ]),
            ("账本与投影", [
                "wiki_pages · doc_assets",
                "memory catalog（auth / PG）+ 投影",
                "chat_attachments.db + FTS5",
                "memory outbox / retention",
            ]),
        ],
    },
    {
        "num": "05", "name": "结果导出", "tag": "不可变快照",
        "accent": "#0D9488", "fill": "#F0FDFA", "stroke": "#99F6E4", "text": "#0F766E",
        "cells": [
            ("导出菜单 · 任务中心", [
                "Markdown / Excel / Word",
                "PDF / PowerPoint",
                "五格式独立任务",
                "预览 / 下载 / 重试",
            ]),
            ("导出与产物路由", [
                "POST /exports",
                "GET /exports/{export_job_id}",
                "GET /artifacts/{id}/preview",
                "GET /artifacts/{id}/download",
            ]),
            ("不可变快照 → 后台任务", [
                "ResultSnapshot（turn 事务内）",
                "result_export_jobs + outbox",
                "单格式失败不影响其他任务",
                "导出意图由服务端判定",
            ]),
            ("export worker · 受控渲染", [
                "Markdown / XLSX / DOCX",
                "PDF / PPTX（light/dark/blue）",
                "不执行用户脚本 / 不访问外网",
                "独立配额 + 保留期清理",
            ]),
            ("产物与保留期", [
                "storage/exports/（0700/0600）",
                "PG 导出账本 + 下载审计",
                "过期删二进制、留元数据",
                "Artifact 不可变",
            ]),
        ],
    },
    {
        "num": "06", "name": "评估 · 质量 · 成本", "tag": "RAGAS · 反馈 · 预算",
        "accent": "#DB2777", "fill": "#FDF2F8", "stroke": "#FBCFE8", "text": "#BE185D",
        "cells": [
            ("评估 · 反馈 · 成本", [
                "EvaluationPage（仅 system_admin）",
                "质量反馈工单",
                "成本预算 / 趋势",
                "阈值门禁与历史对比",
            ]),
            ("评估 / 质量 / 成本路由", [
                "/evaluation/*",
                "/quality/* · /quality/tickets",
                "/chat/turns/{id}/feedback",
                "/costs/*",
            ]),
            ("EvaluationService 等", [
                "RAGAS validate/collect/score/run",
                "QualityService：复核共享 / 审批",
                "CostStore / CostSink：预算 / 价格版本",
                "数据 owner 身份检索",
            ]),
            ("eval / quality worker", [
                "5 项 RAGAS + 硬件规则",
                "单题失败不终止整批",
                "在线采集 / 离线重评",
                "LLM 用量 → 成本账本",
            ]),
            ("运行产物", [
                "storage/evaluations/<run_id>/",
                "snapshot.jsonl · summary.json",
                "report.html · summary.csv",
                "PG 成本 / 质量账本",
            ]),
        ],
    },
    {
        "num": "07", "name": "扩展与沙箱", "tag": "受治理执行",
        "accent": "#EA580C", "fill": "#FFF7ED", "stroke": "#FED7AA", "text": "#C2410C",
        "cells": [
            ("Extensions · Plugins", [
                "MCP 登记 / 测试 / 启用",
                "员工订阅与部门可见性",
                "文本 Skill / 固定 bundle",
                "沙箱任务与产物下载",
            ]),
            ("扩展与沙箱路由", [
                "/plugins/mcp/*",
                "/sandbox/*",
                "管理员审核 + 员工订阅",
                "写提案确认",
            ]),
            ("授权与能力快照", [
                "启用 ∧ 部门范围 ∧ 订阅",
                "external_tool.invoke 二次校验",
                "写工具四道门",
                "审计 / 配额 / governor",
            ]),
            ("受治理执行", [
                "Agent MCP client（远程只读）",
                "src/sandbox API 适配器",
                "文本 Skill 仅作不可信提示",
                "固定 digest bundle / profile",
            ]),
            ("独立运行时边界", [
                "远程 MCP Server",
                "MCP Gateway（transport/session）",
                "Sandbox Runner（唯一 OCI 控制权）",
                "Artifact Broker：stage→scan→commit",
            ]),
        ],
    },
    {
        "num": "08", "name": "可观测与运维", "tag": "Health · OTel",
        "accent": "#475569", "fill": "#F1F5F9", "stroke": "#CBD5E1", "text": "#334155",
        "cells": [
            ("状态 · 日志 · 观测", [
                "SystemStatusPage / LogsPage",
                "Grafana · Prometheus · Phoenix",
                "Worker 心跳与依赖状态",
                "配置版本 / 重启项",
            ]),
            ("健康与运维路由", [
                "/health · /health/live",
                "/health/ready · /dependencies",
                "/system/status",
                "/logs/*",
            ]),
            ("健康检查与遥测引导", [
                "worker_registry",
                "check_ready / check_dependencies",
                "init_observability（fail-soft）",
                "配置热重载 / 迁移预演",
            ]),
            ("OTel SDK · 指标 · 日志", [
                "trace / metric / log",
                "FastAPI / HTTPX 自动埋点",
                "结构化 JSON + trace_id",
                "隐私开关：默认不采正文",
            ]),
            ("观测栈", [
                "OTel Collector :4317/4318",
                "Prometheus · Tempo · Loki",
                "Grafana · Phoenix",
                "storage/logs/ 本地留存",
            ]),
        ],
    },
]

ROW_Y0 = 240
ROW_H = 190
ROW_PITCH = 208
CARD_INSET = 10
CARD_Y_OFFSET = 11
CARD_H = 168
HEADER_Y = 150
HEADER_H = 64


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def card_html(title: str, lines: list[str], accent: str) -> str:
    body = "<br>".join(
        f"<span style='color:{accent}'>•</span> {esc(line)}" for line in lines
    )
    return (
        "<div style='padding:1px 1px 0 1px'>"
        f"<div style='font-size:14px;font-weight:700;color:#0F172A;line-height:1.3;margin-bottom:5px'>{esc(title)}</div>"
        f"<div style='font-size:12px;color:#475569;line-height:1.58'>{body}</div>"
        "</div>"
    )


cells: list[str] = []


def add_vertex(cid: str, value: str, style: str, x: int, y: int, w: int, h: int) -> None:
    cells.append(
        f'<mxCell id="{cid}" value="{esc(value)}" style="{style}" vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
    )


def add_raw_vertex(cid: str, value: str, style: str, x: int, y: int, w: int, h: int) -> None:
    cells.append(
        f'<mxCell id="{cid}" value="{esc(value)}" style="{style}" vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
    )


def add_edge(cid: str, src: str, dst: str, color: str = "#94A3B8") -> None:
    style = (
        "edgeStyle=none;rounded=0;html=1;exitX=1;exitY=0.5;exitDx=0;exitDy=0;"
        "entryX=0;entryY=0.5;entryDx=0;entryDy=0;endArrow=block;endSize=7;endFill=1;"
        f"strokeWidth=1.5;strokeColor={color};"
    )
    cells.append(
        f'<mxCell id="{cid}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


for key, x, w, title, subtitle, header_fill, lane_fill, lane_stroke in LANES:
    add_vertex(
        f"lane_{key}", "",
        "rounded=0;whiteSpace=wrap;html=1;fillColor=%s;strokeColor=none;" % lane_fill,
        x, HEADER_Y + HEADER_H + 2, w, (ROW_Y0 + 8 * ROW_PITCH - 18) - (HEADER_Y + HEADER_H + 2),
    )
    add_raw_vertex(
        f"hdr_{key}",
        "<div style='text-align:center'>"
        f"<div style='font-size:15px;font-weight:700;color:#FFFFFF;line-height:1.2'>{esc(title)}</div>"
        f"<div style='font-size:10.5px;color:#FFFFFF;opacity:.86;margin-top:3px'>{esc(subtitle)}</div>"
        "</div>",
        "rounded=1;arcSize=7;whiteSpace=wrap;html=1;fillColor=%s;strokeColor=none;align=center;verticalAlign=middle;" % header_fill,
        x, HEADER_Y, w, HEADER_H,
    )

for i in range(8):
    y = ROW_Y0 + i * ROW_PITCH - 3
    add_vertex(
        f"sep_{i}", "",
        "rounded=0;whiteSpace=wrap;html=1;fillColor=#E2E8F0;strokeColor=none;",
        270, y, 2250, 1,
    )

add_vertex(
    "logo", "HDB",
    "rounded=1;arcSize=14;whiteSpace=wrap;html=1;fillColor=#0F172A;strokeColor=none;fontColor=#FFFFFF;fontSize=16;fontStyle=1;align=center;verticalAlign=middle;",
    40, 34, 54, 54,
)
add_vertex(
    "title", "Hardware DataBase · 当前系统架构",
    "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;whiteSpace=wrap;fontSize=30;fontStyle=1;fontColor=#0F172A;",
    110, 32, 1180, 38,
)
add_vertex(
    "subtitle", "多源智能问答与知识治理基座 · 分层视图（按业务域横向展开，层内为当前分支实际组件）",
    "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;whiteSpace=wrap;fontSize=13.5;fontColor=#64748B;",
    112, 72, 1180, 26,
)
add_vertex(
    "constraints", "关键约束：RAGFlow 固定文档后端  ·  PostgreSQL 为账本 / Redis 为实时面  ·  工具 fail-open、权限 fail-closed  ·  system_admin 不访问 KB 内容",
    "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;whiteSpace=wrap;fontSize=11.5;fontColor=#64748B;",
    112, 104, 1500, 24,
)
add_vertex(
    "asof", "as of 2026-10 · 以当前分支代码为准",
    "text;html=1;strokeColor=none;fillColor=none;align=right;verticalAlign=middle;whiteSpace=wrap;fontSize=11;fontColor=#94A3B8;",
    2050, 104, 510, 24,
)

for ri, row in enumerate(ROWS, start=1):
    row_y = ROW_Y0 + (ri - 1) * ROW_PITCH
    accent = row["accent"]
    add_raw_vertex(
        f"row_{ri}_label",
        "<div style='text-align:center'>"
        f"<div style='font-size:26px;font-weight:700;color:{row['text']};line-height:1.05'>{row['num']}</div>"
        f"<div style='font-size:15px;font-weight:700;color:#0F172A;margin-top:5px;line-height:1.25'>{esc(row['name'])}</div>"
        f"<div style='font-size:10px;color:#64748B;margin-top:4px'>{esc(row['tag'])}</div>"
        "</div>",
        "rounded=1;arcSize=7;whiteSpace=wrap;html=1;fillColor=%s;strokeColor=%s;strokeWidth=1.2;align=center;verticalAlign=middle;" % (row["fill"], row["stroke"]),
        40, row_y, 200, ROW_H,
    )
    card_ids: list[str] = []
    for ci, (lane, (title, lines)) in enumerate(zip(LANES, row["cells"]), start=1):
        _, lx, lw, *_ = lane
        x = lx + CARD_INSET
        w = lw - 2 * CARD_INSET
        y = row_y + CARD_Y_OFFSET
        cid = f"r{ri}_l{ci}"
        card_ids.append(cid)
        add_raw_vertex(
            cid,
            card_html(title, lines, accent),
            "rounded=1;arcSize=5;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#E2E8F0;strokeWidth=1;align=left;verticalAlign=top;spacing=10;spacingTop=9;",
            x, y, w, CARD_H,
        )
        add_vertex(
            f"{cid}_bar", "",
            "rounded=1;arcSize=20;whiteSpace=wrap;html=1;fillColor=%s;strokeColor=none;" % accent,
            x + 10, y + 1, w - 20, 3,
        )
    for ci in range(4):
        add_edge(f"e{ri}_{ci + 1}", card_ids[ci], card_ids[ci + 1], accent)

PANEL_Y = 1930
add_vertex(
    "deploy_panel", "",
    "rounded=1;arcSize=3;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#CBD5E1;strokeWidth=1.2;",
    40, PANEL_Y, 2520, 180,
)
add_vertex(
    "deploy_title", "部署与进程拓扑 · deploy/docker/docker-compose.yml（生产默认，全部容器化）",
    "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;whiteSpace=wrap;fontSize=14;fontStyle=1;fontColor=#0F172A;",
    60, PANEL_Y + 10, 1200, 26,
)
add_vertex(
    "deploy_note", "单机开发：uv run hardware-database-server + npm run dev · 多实例：关闭 HDB_API_SPAWN_*，worker 独立起、memory 单写者",
    "text;html=1;strokeColor=none;fillColor=none;align=right;verticalAlign=middle;whiteSpace=wrap;fontSize=10.5;fontColor=#64748B;",
    1180, PANEL_Y + 10, 1360, 26,
)

DEPLOY_GROUPS = [
    (60, 760, "入口与账本", [
        "web（nginx :8080）→ api（FastAPI :8001）",
        "migrate（一次性 bootstrap）→ postgres · pgvector:pg17",
        "redis:8-alpine · 实时面（SSE / 取消 / 准入）",
    ]),
    (840, 820, "独立后台 Worker（各一容器）", [
        "worker-chat / worker-parse",
        "worker-export / worker-memory",
        "worker-evaluation / worker-quality",
        "另有 maintenance / e2e CLI",
    ]),
    (1680, 860, "外部依赖 / 单机形态", [
        "RAGFlow：固定文档解析与检索后端",
        "LLM：Ollama 或任意 OpenAI 兼容 API",
        "单机 server 可自启 worker + export worker",
    ]),
]
for gi, (gx, gw, gtitle, glines) in enumerate(DEPLOY_GROUPS, start=1):
    gy = PANEL_Y + 44
    gh = 120
    add_vertex(
        f"deploy_g{gi}", "",
        "rounded=1;arcSize=6;whiteSpace=wrap;html=1;fillColor=#F8FAFC;strokeColor=#E2E8F0;strokeWidth=1;",
        gx, gy, gw, gh,
    )
    add_vertex(
        f"deploy_g{gi}_title", gtitle,
        "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;whiteSpace=wrap;fontSize=12;fontStyle=1;fontColor=#334155;",
        gx + 14, gy + 8, gw - 28, 22,
    )
    body_html = "<br>".join(f"<span style='color:#94A3B8'>•</span> {esc(line)}" for line in glines)
    add_raw_vertex(
        f"deploy_g{gi}_body",
        f"<div style='font-size:10.5px;color:#475569;line-height:1.72'>{body_html}</div>",
        "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=top;whiteSpace=wrap;spacing=0;",
        gx + 14, gy + 34, gw - 28, gh - 42,
    )

add_vertex(
    "legend_title", "读法",
    "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;whiteSpace=wrap;fontSize=12;fontStyle=1;fontColor=#0F172A;",
    60, 2142, 60, 22,
)
add_vertex(
    "legend_body",
    "每一行是一条业务链路，按 L1 → L5 从左到右阅读；箭头只连接同一行相邻层，不跨行、不交叉。数据列重复列出该链路实际读写的后端，便于按域追踪。",
    "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;whiteSpace=wrap;fontSize=11.5;fontColor=#475569;",
    120, 2142, 1500, 22,
)
add_vertex(
    "legend_colors",
    "<span style='color:#2563EB'>■ 问答</span>　<span style='color:#059669'>■ 摄取</span>　<span style='color:#D97706'>■ 治理</span>　<span style='color:#7C3AED'>■ 资产</span>　<span style='color:#0D9488'>■ 导出</span>　<span style='color:#DB2777'>■ 评估</span>　<span style='color:#EA580C'>■ 扩展</span>　<span style='color:#475569'>■ 运维</span>",
    "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;whiteSpace=wrap;fontSize=11;fontColor=#475569;",
    1620, 2142, 940, 22,
)
add_vertex(
    "footer",
    "依据：README.md · wiki/01-overview.md · wiki/02-architecture.md · src/ 当前分支 ｜ 生成：2026-10-30",
    "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;whiteSpace=wrap;fontSize=10;fontColor=#94A3B8;",
    60, 2196, 1500, 20,
)

xml = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mxfile host="app.diagrams.net" modified="2026-10-30T12:00:00.000Z" agent="OpenCode" version="24.7.17" type="device">\n'
    '  <diagram id="hdb-current-architecture" name="系统架构总览">\n'
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

CJK_PATH = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
LATIN_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x2E80 or ch in "（）：；，。！？、《》【】“”‘’…—"


def split_runs(text: str):
    runs = []
    cur = ""
    cur_cjk = None
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


def font_for(ch: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
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


def draw_center(draw, cx, y, text: str, size: int, fill: str, bold: bool = False) -> None:
    draw_mixed(draw, (cx - mlen(draw, text, size) / 2, y), text, size, fill, bold)


def wrap_mixed(draw, text: str, size: int, max_w: float) -> list[str]:
    lines = []
    cur = ""
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


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


img = Image.new("RGB", (W, H), "#FFFFFF")
draw = ImageDraw.Draw(img)

# lane backgrounds + headers
for key, x, w, title, subtitle, header_fill, lane_fill, lane_stroke in LANES:
    draw.rectangle([x, HEADER_Y + HEADER_H + 2, x + w, ROW_Y0 + 8 * ROW_PITCH - 18], fill=lane_fill)
    rounded(draw, [x, HEADER_Y, x + w, HEADER_Y + HEADER_H], 8, header_fill)
    draw_center(draw, x + w / 2, HEADER_Y + 11, title, 15, "#FFFFFF", bold=True)
    draw_center(draw, x + w / 2, HEADER_Y + 37, subtitle, 10, "#E2E8F0")

# dotted row separators
for i in range(8):
    y = ROW_Y0 + i * ROW_PITCH - 3
    for xx in range(270, 2520, 12):
        draw.line([xx, y, xx + 6, y], fill="#CBD5E1", width=1)

# header
rounded(draw, [40, 34, 94, 88], 12, "#0F172A")
draw_center(draw, 67, 46, "HDB", 16, "#FFFFFF", bold=True)
draw_mixed(draw, (110, 32), "Hardware DataBase · 当前系统架构", 27, "#0F172A", bold=True)
draw_mixed(draw, (112, 74), "多源智能问答与知识治理基座 · 分层视图（按业务域横向展开，层内为当前分支实际组件）", 13, "#64748B")
draw_mixed(draw, (112, 105), "关键约束：RAGFlow 固定文档后端  ·  PostgreSQL 为账本 / Redis 为实时面  ·  工具 fail-open、权限 fail-closed  ·  system_admin 不访问 KB 内容", 11, "#64748B")
draw_mixed(draw, (2050, 105), "as of 2026-10 · 以当前分支代码为准", 10, "#94A3B8")

# rows
for ri, row in enumerate(ROWS, start=1):
    row_y = ROW_Y0 + (ri - 1) * ROW_PITCH
    rounded(draw, [40, row_y, 240, row_y + ROW_H], 8, row["fill"], row["stroke"], 1)
    draw_center(draw, 140, row_y + 45, row["num"], 26, row["text"], bold=True)
    draw_center(draw, 140, row_y + 88, row["name"], 15, "#0F172A", bold=True)
    draw_center(draw, 140, row_y + 114, row["tag"], 9, "#64748B")
    for ci, (lane, (title, lines)) in enumerate(zip(LANES, row["cells"]), start=1):
        _, lx, lw, *_ = lane
        x = lx + CARD_INSET
        w = lw - 2 * CARD_INSET
        y = row_y + CARD_Y_OFFSET
        rounded(draw, [x, y, x + w, y + CARD_H], 6, "#FFFFFF", "#E2E8F0", 1)
        draw.rectangle([x + 10, y + 1, x + w - 10, y + 3], fill=row["accent"])
        ty = y + 9
        for ln in wrap_mixed(draw, title, 14, w - 22)[:2]:
            draw_mixed(draw, (x + 11, ty), ln, 14, "#0F172A", bold=True)
            ty += 18
        by = ty + 5
        for ln in lines:
            wrapped = wrap_mixed(draw, ln, 12, w - 31)
            draw.ellipse([x + 12, by + 5, x + 16, by + 9], fill=row["accent"])
            for j, sub in enumerate(wrapped):
                draw_mixed(draw, (x + 21, by), sub, 12, "#475569")
                by += 18
            by += 1
    # horizontal arrows between adjacent cards
    for ci in range(4):
        _, lx, lw, *_ = LANES[ci]
        nx = LANES[ci + 1][1]
        x1 = lx + lw - CARD_INSET
        x2 = nx + CARD_INSET
        cy = row_y + 95
        draw.line([x1 + 2, cy, x2 - 8, cy], fill=row["accent"], width=2)
        draw.polygon([(x2 - 8, cy - 4), (x2, cy), (x2 - 8, cy + 4)], fill=row["accent"])

# deployment panel
rounded(draw, [40, PANEL_Y, 2560, PANEL_Y + 180], 8, "#FFFFFF", "#CBD5E1", 1)
draw_mixed(draw, (60, PANEL_Y + 9), "部署与进程拓扑 · deploy/docker/docker-compose.yml（生产默认，全部容器化）", 14, "#0F172A", bold=True)
note = "单机开发：uv run hardware-database-server + npm run dev · 多实例：关闭 HDB_API_SPAWN_*，worker 独立起、memory 单写者"
draw_mixed(draw, (2540 - mlen(draw, note, 10), PANEL_Y + 13), note, 10, "#64748B")
for gi, (gx, gw, gtitle, glines) in enumerate(DEPLOY_GROUPS, start=1):
    gy = PANEL_Y + 44
    rounded(draw, [gx, gy, gx + gw, gy + 120], 6, "#F8FAFC", "#E2E8F0", 1)
    draw_mixed(draw, (gx + 14, gy + 8), gtitle, 12, "#334155", bold=True)
    by = gy + 34
    for ln in glines:
        draw.ellipse([gx + 14, by + 6, gx + 17, by + 9], fill="#94A3B8")
        draw_mixed(draw, (gx + 26, by), ln, 10, "#475569")
        by += 18

# legend
draw_mixed(draw, (60, 2142), "读法", 12, "#0F172A", bold=True)
draw_mixed(draw, (120, 2142), "每一行是一条业务链路，按 L1 → L5 从左到右阅读；箭头只连接同一行相邻层，不跨行、不交叉。数据列重复列出该链路实际读写的后端，便于按域追踪。", 11, "#475569")
draw_mixed(draw, (1620, 2142), "■ 问答   ■ 摄取   ■ 治理   ■ 资产   ■ 导出   ■ 评估   ■ 扩展   ■ 运维", 10, "#475569")
draw_mixed(draw, (60, 2196), "依据：README.md · wiki/01-overview.md · wiki/02-architecture.md · src/ 当前分支 ｜ 生成：2026-10-30", 10, "#94A3B8")

img.save(PREVIEW_PATH)
print(f"wrote {PREVIEW_PATH} ({W}x{H})")
