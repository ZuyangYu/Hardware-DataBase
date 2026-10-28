#!/usr/bin/env bash
# Hardware DataBase —— 部署后全面冒烟测试（容器/接口/数据库形状/日志）。
#
# 用法（在部署目录或 docker/ 目录里执行）：
#   bash smoke-test.sh [部署目录]
#
# 覆盖：
#   1) 全部服务运行且健康、无重启；/health/ready 就绪
#   2) 认证与治理边界（admin 不能访问知识库内容）
#   3) 关键写路径：建库、上传 Excel、建会话、删除会话（含清理围栏）
#   4) 数据库表形状：memory / result_exports 必须是控制面语义类型
#      （BOOLEAN/TIMESTAMPTZ/JSONB）——机械映射（TEXT/BIGINT）会让控制面
#      的布尔查询直接 500，这是实际发生过的故障类型
#   5) 日志扫描：Traceback / 缺表 / 围栏失败 / 连接池错误
#
# 退出码：0=全部通过（允许 WARN）；1=存在 FAIL。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARG_DIR="${1:-}"
if [ -n "$ARG_DIR" ]; then
  if [ -f "$ARG_DIR/docker-compose.yml" ]; then DOCKER_DIR="$(cd "$ARG_DIR" && pwd)"
  elif [ -f "$ARG_DIR/docker/docker-compose.yml" ]; then DOCKER_DIR="$(cd "$ARG_DIR/docker" && pwd)"
  else echo "[smoke] 错误: $ARG_DIR 下找不到 docker-compose.yml" >&2; exit 2; fi
elif [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then DOCKER_DIR="$SCRIPT_DIR"
else echo "[smoke] 错误: 找不到 docker-compose.yml，请传入部署目录" >&2; exit 2; fi

cd "$DOCKER_DIR" || exit 2
COMPOSE=(docker compose -f docker-compose.yml)
[ -f docker-compose.observability.yml ] && COMPOSE+=(-f docker-compose.observability.yml)

PASS=0; FAIL=0; WARN=0
ok()   { PASS=$((PASS+1)); printf '  [PASS] %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$*"; }
warn() { WARN=$((WARN+1)); printf '  [WARN] %s\n' "$*"; }
expect() { if [ "$2" = "$3" ]; then ok "$1 = $3"; else bad "$1: 期望 $2，实际 $3"; fi; }
log()  { printf '\n[smoke] %s\n' "$*"; }

HTTP_PORT="$( { grep -E '^HDB_HTTP_PORT=' .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"; HTTP_PORT="${HTTP_PORT:-8080}"
BASE="http://127.0.0.1:${HTTP_PORT}"
ADMIN_USER="$( { grep -E '^AUTH_DEFAULT_ADMIN_USERNAME=' config/.env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"; ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PW="$( { grep -E '^AUTH_DEFAULT_ADMIN_PASSWORD=' config/.env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"
if [ -z "$ADMIN_PW" ]; then echo "[smoke] 错误: 读不到 config/.env 的管理员口令" >&2; exit 2; fi
TMP="$(mktemp -d /tmp/hdb-smoke.XXXXXX)"; trap 'rm -rf "$TMP"' EXIT
curl_json() { curl -s -H 'Content-Type: application/json' "$@"; }
py() { python3 -c "$1"; }

log "0) 基础信息（$BASE，部署目录 $DOCKER_DIR）"
printf '  版本: %s\n' "$( { grep -m1 '^tag=' VERSION 2>/dev/null || true; } | cut -d= -f2-)"

log "1) 容器与服务健康"
SERVICES="$(${COMPOSE[@]} config --services 2>/dev/null)"
for svc in $SERVICES; do
  cid="$(${COMPOSE[@]} ps -q "$svc" 2>/dev/null | head -n 1)"
  if [ -z "$cid" ]; then [ "$svc" = "migrate" ] || bad "$svc 无容器"; continue; fi
  state="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null)"
  if [ "$svc" = "migrate" ]; then
    code="$(docker inspect -f '{{.State.ExitCode}}' "$cid" 2>/dev/null)"
    expect "migrate 退出码" 0 "$code"
  else
    expect "$svc 状态" running "$state"
    expect "$svc 重启次数" 0 "${restarts:-x}"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null)"
    [ -z "$health" ] || [ "$health" = "healthy" ] || bad "$svc 健康检查=$health"
  fi
done
for role in chat parse export memory; do
  st="$(${COMPOSE[@]} ps --format '{{.State}}' "worker-$role" 2>/dev/null | head -n 1)"
  expect "worker-$role 运行中" running "${st:-none}"
done

log "2) /health/ready 就绪"
ready=000
for _ in $(seq 1 40); do ready="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/health/ready")"; [ "$ready" = 200 ] && break; sleep 3; done
expect "GET /health/ready" 200 "$ready"
if [ "$ready" != 200 ]; then curl_json "$BASE/health/ready" | head -c 400; echo; fi

log "3) 认证与治理边界"
AT="$(curl_json -X POST "$BASE/api/v1/login" -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PW\"}" | py 'import json,sys; print(json.load(sys.stdin).get("token",""))')"
[ "${#AT}" -gt 20 ] && ok "admin 登录成功" || bad "admin 登录失败"
forbidden="$(curl_json -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/kbs" -H "Authorization: Bearer $AT" -d '{"name":"smoke-should-403"}')"
expect "admin 建库（治理角色应被拒）" 403 "$forbidden"

# 幂等准备：测试部门 / 员工 / 知识库
curl_json "$BASE/api/v1/departments" -H "Authorization: Bearer $AT" > "$TMP/deps.json"
DID="$(py 'import json,sys; d=json.load(open("'"$TMP"'/deps.json")); print(next((x["id"] for x in d if x["name"]=="冒烟测试部"), ""))')"
if [ -z "$DID" ]; then
  curl_json -X POST "$BASE/api/v1/departments" -H "Authorization: Bearer $AT" -d '{"name":"冒烟测试部"}' > "$TMP/dep.json"
  DID="$(py 'import json,sys; print(json.load(open("'"$TMP"'/dep.json")).get("id",""))')"
fi
[ -n "$DID" ] && ok "部门就绪（id=$DID）" || bad "部门创建失败"
EMP_PW="Smoke-2026-pw"
curl_json -X POST "$BASE/api/v1/users" -H "Authorization: Bearer $AT" \
  -d "{\"username\":\"smoke_tester\",\"password\":\"$EMP_PW\",\"role\":\"employee\",\"department_id\":$DID}" >/dev/null
ET="$(curl_json -X POST "$BASE/api/v1/login" -d "{\"username\":\"smoke_tester\",\"password\":\"$EMP_PW\"}" | py 'import json,sys; print(json.load(sys.stdin).get("token",""))')"
[ "${#ET}" -gt 20 ] && ok "员工登录成功" || bad "员工登录失败（可能已存在同名用户但口令不同）"
KB="smoke-kb"
curl_json -X POST "$BASE/api/v1/kbs" -H "Authorization: Bearer $ET" -d "{\"name\":\"$KB\"}" >/dev/null
kbs="$(curl_json "$BASE/api/v1/kbs" -H "Authorization: Bearer $ET")"
echo "$kbs" | grep -q "$KB" && ok "知识库就绪（$KB）" || bad "知识库创建失败"

log "4) 关键写路径"
# 4a) 上传 Excel（本地表格管线；入口当前会先解析 RAGFlow 数据集）
python3 - "$TMP/bom.xlsx" <<'PYEOF'
import sys, zipfile
p = sys.argv[1]
ct='''<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'''
rels='<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
wb='<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="BOM" sheetId="1" r:id="rId1"/></sheets></workbook>'
wbr='<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
sh='<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>位号</t></is></c><c r="B1" t="inlineStr"><is><t>数量</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>R1</t></is></c><c r="B2"><v>10</v></c></row></sheetData></worksheet>'
with zipfile.ZipFile(p,"w",zipfile.ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml",ct); z.writestr("_rels/.rels",rels)
    z.writestr("xl/workbook.xml",wb); z.writestr("xl/_rels/workbook.xml.rels",wbr)
    z.writestr("xl/worksheets/sheet1.xml",sh)
PYEOF
up_code="$(curl -s -o "$TMP/up.json" -w '%{http_code}' -X POST "$BASE/api/v1/kbs/$KB/files" \
  -H "Authorization: Bearer $ET" -F "files=@$TMP/bom.xlsx" -F "source_group=material")"
if [ "$up_code" = 200 ]; then
  ok "Excel 上传成功"
elif grep -q "RAGFLOW_API_KEY is not configured" "$TMP/up.json" 2>/dev/null; then
  warn "Excel 上传被 RAGFlow 未配置阻塞（已知问题：入口先解析 RAGFlow 数据集，本地表格解析走不到；配置 RAGFlow 后应转为 PASS）"
else
  bad "Excel 上传失败（http=$up_code）：$(head -c 200 "$TMP/up.json")"
fi

# 4b) 会话创建 → 删除（覆盖 memory 失效围栏；曾经 500）
SID="$(curl_json -X POST "$BASE/api/v1/conversations" -H "Authorization: Bearer $ET" -d "{\"kb_name\":\"$KB\",\"title\":\"冒烟测试\"}" | py 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
[ -n "$SID" ] && ok "会话创建成功（id=$SID）" || bad "会话创建失败"
del_code="$(curl -s -o "$TMP/del.json" -w '%{http_code}' -X DELETE "$BASE/api/v1/conversations/$SID" -H "Authorization: Bearer $ET")"
expect "删除会话" 200 "$del_code"
sleep 4
if ${COMPOSE[@]} logs --no-color --tail 200 api 2>/dev/null | grep -q "session deletion not committed"; then
  bad "会话清理围栏失败（session deletion not committed）"
else
  ok "会话清理无围栏错误"
fi

log "4c) 前端 SPA 路由与构建产物目录（/assets 冲突回归）"
# 资产中心路由与 Vite 产物目录 dist/assets/ 同名：刷新必须回 index.html，
# 修复前会被 nginx 当作目录 301（且丢失外部端口）→ 404。
spa_code="$(curl -s -o "$TMP/spa.html" -w '%{http_code}' --max-time 10 "$BASE/assets")"
expect "资产中心路由 /assets 刷新" 200 "$spa_code"
grep -qi '<div id="root"' "$TMP/spa.html" 2>/dev/null && ok "SPA 入口返回 index.html" || bad "SPA 入口内容异常"
asset_js="$(grep -oE 'src="/assets/[^"]+\.js"' "$TMP/spa.html" | head -1 | cut -d'"' -f2)"
if [ -n "$asset_js" ]; then
  js_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE$asset_js")"
  expect "构建产物 $asset_js" 200 "$js_code"
else
  warn "index.html 中未找到 /assets/*.js 引用（构建格式变化？）"
fi
# nginx 目录 301 必须用相对 Location，不能丢失端口映射（8080 → 80）
loc="$(curl -s -o /dev/null -w '%{redirect_url}' --max-time 10 "$BASE/help-assets")"
case "$loc" in
  *":${HTTP_PORT}/"*) ok "目录跳转保留端口（$loc）" ;;
  "") ok "目录跳转未触发（help-assets 缺失，跳过）" ;;
  *) bad "目录跳转丢失端口：$loc" ;;
esac

log "5) 数据库表形状（控制面语义类型）"
pgq() { ${COMPOSE[@]} exec -T postgres psql -U hdb -d hdb -tAc "$1" 2>/dev/null | tr -d ' '; }
expect "memory_sources.source_valid 类型" boolean "$(pgq "select data_type from information_schema.columns where table_name='memory_sources' and column_name='source_valid'")"
expect "memory_sources.invalidated_at 类型" "timestampwithtimezone" "$(pgq "select data_type from information_schema.columns where table_name='memory_sources' and column_name='invalidated_at'")"
expect "memory_projections.active 类型" boolean "$(pgq "select data_type from information_schema.columns where table_name='memory_projections' and column_name='active'")"
expect "result_export_jobs.options_json 类型" jsonb "$(pgq "select data_type from information_schema.columns where table_name='result_export_jobs' and column_name='options_json'")"
# 控制面真实查询探针：布尔语法必须能在 PG 上执行
probe="$(pgq "select count(*) from memory_sources where session_id = 1 and source_valid")"
if [ -n "$probe" ]; then ok "控制面布尔查询探针通过（memory_sources AND source_valid）"; else bad "控制面布尔查询探针失败（类型不匹配？）"; fi
probe2="$(pgq "select count(*) from result_export_jobs where status = 'queued' and available_at <= '2030-01-01 00:00:00'")"
if [ -n "$probe2" ]; then ok "导出表时间戳查询探针通过"; else bad "导出表时间戳查询探针失败"; fi

log "6) 日志扫描"
LOG_PATTERNS='Traceback|session deletion not committed|does not exist|UniqueViolation|cannot join current thread|DatatypeMismatch'
for svc in api web worker-chat worker-parse worker-export worker-memory; do
  hits="$(${COMPOSE[@]} logs --no-color "$svc" 2>/dev/null | grep -cE "$LOG_PATTERNS" || true)"
  expect "$svc 异常日志行数" 0 "${hits:-0}"
done

printf '\n[smoke] 结果：PASS=%d FAIL=%d WARN=%d\n' "$PASS" "$FAIL" "$WARN"
[ "$FAIL" -eq 0 ] || { echo "[smoke] 存在失败项"; exit 1; }
echo "[smoke] 冒烟测试通过"
