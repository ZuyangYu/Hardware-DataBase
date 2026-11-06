#!/usr/bin/env node
/**
 * Remote MCP facade for the official draw.io stdio server.
 *
 * HDB only speaks remote MCP (streamable HTTP / SSE); it deliberately never
 * starts stdio servers.  This process owns one reviewed child
 * (`@drawio/mcp`) and exposes a streamable HTTP MCP endpoint on /mcp.
 *
 * Only diagram-generation and shape-search tools are exposed.  The official
 * server's local-file page tools (`list_pages` / `get_page` / `set_page`) are
 * filtered out because a remote deployment must not read or write host files.
 * All exposed tools are annotated read-only: they return a draw.io URL and do
 * not mutate platform state.
 */

import http from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  isInitializeRequest,
} from '@modelcontextprotocol/sdk/types.js';

const HOST = process.env.DRAWIO_MCP_HOST || '0.0.0.0';
const PORT = Number(process.env.DRAWIO_MCP_PORT || 8091);
const DRAWIO_BASE_URL = process.env.DRAWIO_BASE_URL || 'https://app.diagrams.net/';
const SERVER_PATH =
  process.env.DRAWIO_MCP_SERVER ||
  fileURLToPath(new URL('./node_modules/@drawio/mcp/src/index.js', import.meta.url));
const MAX_BODY_BYTES = 2 * 1024 * 1024;

const SAFE_TOOLS = new Set([
  'open_drawio_xml',
  'open_drawio_csv',
  'open_drawio_mermaid',
  'search_shapes',
]);

const FILE_TOOL = 'create_drawio_file';
const MAX_DRAWIO_BYTES = 5 * 1024 * 1024;
const LOCAL_TOOLS = [
  {
    name: FILE_TOOL,
    description:
      'Create a downloadable native .drawio file from draw.io mxGraphModel XML. ' +
      'Use this when the user wants a file they can download and reopen in draw.io. ' +
      'Pass a complete <mxGraphModel>...</mxGraphModel> element in `content`; ' +
      'do not pass Mermaid or CSV syntax here. Optional `filename` defaults to ' +
      'diagram.drawio. The platform turns the returned artifact into a confirmation ' +
      'card and then a downloadable candidate file.',
    inputSchema: {
      type: 'object',
      properties: {
        content: {
          type: 'string',
          description:
            'draw.io mxGraphModel XML. Must be one <mxGraphModel> element, or a full <mxfile> document.',
        },
        filename: {
          type: 'string',
          description: 'Optional safe basename ending in .drawio (default: diagram.drawio).',
        },
      },
      required: ['content'],
    },
    annotations: { readOnlyHint: false, openWorldHint: false },
  },
];

const sessions = new Map();
let childPromise = null;

function getChildClient() {
  if (!childPromise) {
    childPromise = (async () => {
      const transport = new StdioClientTransport({
        command: process.execPath,
        args: [SERVER_PATH],
        env: { DRAWIO_BASE_URL },
        stderr: 'pipe',
      });
      transport.stderr?.on('data', (chunk) => {
        const text = String(chunk).trim();
        if (text) console.error(`[drawio-mcp] ${text}`);
      });
      const client = new Client(
        { name: 'hdb-drawio-mcp-bridge', version: '1.0.0' },
        { capabilities: {} },
      );
      await client.connect(transport);
      transport.onclose = () => {
        childPromise = null;
      };
      console.error(`draw.io MCP child started: ${SERVER_PATH}`);
      return client;
    })().catch((error) => {
      childPromise = null;
      throw error;
    });
  }
  return childPromise;
}

function safeTools(tools) {
  return tools
    .filter((tool) => SAFE_TOOLS.has(tool.name))
    .map((tool) => ({
      ...tool,
      annotations: {
        ...(tool.annotations || {}),
        readOnlyHint: true,
        openWorldHint: true,
      },
    }));
}

function safeFilename(value) {
  const base = String(value || '')
    .replace(/\\/g, '/')
    .split('/')
    .pop()
    .replace(/[^A-Za-z0-9._\u4e00-\u9fff-]+/g, '_')
    .replace(/^\.+|\.+$/g, '')
    .slice(0, 120);
  const stem = (base || 'diagram').replace(/\.drawio$/i, '') || 'diagram';
  return `${stem}.drawio`;
}

function buildDrawioFile(content, filename) {
  let text = String(content || '').trim();
  if (!text) throw new Error('content is required');
  // Some models serialize tool-call wrapper tags inside string arguments
  // (for example a trailing </content> or <parameter name="filename">).
  // Keep the actual XML document and discard only the trailing wrapper.
  const mxfileStart = text.search(/<mxfile[\s>]/i);
  const mxmodelStart = text.search(/<mxGraphModel[\s>]/i);
  if (mxfileStart > 0 || (mxfileStart < 0 && mxmodelStart > 0)) {
    text = text.slice(mxfileStart >= 0 ? mxfileStart : mxmodelStart);
  }
  const mxfileEnd = text.lastIndexOf('</mxfile>');
  if (mxfileEnd >= 0) {
    text = text.slice(0, mxfileEnd + '</mxfile>'.length).trim();
  } else {
    const mxmodelEnd = text.lastIndexOf('</mxGraphModel>');
    if (mxmodelEnd >= 0) {
      text = text.slice(0, mxmodelEnd + '</mxGraphModel>'.length).trim();
    }
  }
  if (text.length > MAX_DRAWIO_BYTES) throw new Error('content is too large');
  if (/<!DOCTYPE|<!ENTITY/i.test(text)) {
    throw new Error('DOCTYPE/ENTITY is not allowed');
  }
  if (!text.startsWith('<')) throw new Error('content must be draw.io XML');
  if (/^<mxGraphModel[\s>]/i.test(text) && !/<\/mxGraphModel>\s*$/i.test(text)) {
    throw new Error('incomplete draw.io XML: missing closing </mxGraphModel>');
  }
  if (/^<mxfile[\s>]/i.test(text) && !/<\/mxfile>\s*$/i.test(text)) {
    throw new Error('incomplete draw.io XML: missing closing </mxfile>');
  }
  const name = safeFilename(filename);
  const pageName = name.replace(/\.drawio$/i, '');
  let document;
  if (/^<mxfile[\s>]/i.test(text)) {
    document = text.replace(/^<\?xml[^>]*>\s*/i, '');
  } else if (/^<mxGraphModel[\s>]/i.test(text)) {
    document =
      '<mxfile host="app.diagrams.net" agent="hdb-drawio-mcp" version="1.0">' +
      `<diagram id="${randomUUID()}" name="${pageName.replace(/[<>&"]/g, '_')}">` +
      text +
      '</diagram></mxfile>';
  } else {
    throw new Error('content must start with <mxGraphModel> or <mxfile>');
  }
  return { document, name };
}

function createDrawioArtifact(args) {
  const { document, name } = buildDrawioFile(args?.content, args?.filename);
  const bytes = Buffer.from(document, 'utf8');
  if (bytes.length > MAX_DRAWIO_BYTES) throw new Error('draw.io file is too large');
  const payload = {
    filename: name,
    content_base64: bytes.toString('base64'),
    sha256: createHash('sha256').update(bytes).digest('hex'),
    media_type: 'application/vnd.jgraph.mxfile',
  };
  return {
    content: [
      {
        type: 'text',
        text: `已生成 draw.io 文件：${name}（${bytes.length} bytes）`,
      },
      // The HDB write channel extracts the artifact from the text JSON when
      // the MCP adapter returns content-only; structuredContent is kept for
      // clients that support it.
      { type: 'text', text: JSON.stringify(payload) },
    ],
    structuredContent: payload,
    isError: false,
  };
}

function buildServer() {
  const server = new Server(
    { name: 'hdb-drawio-mcp', version: '1.0.0' },
    { capabilities: { tools: {} } },
  );
  server.setRequestHandler(ListToolsRequestSchema, async () => {
    const client = await getChildClient();
    const result = await client.listTools();
    return { tools: [...safeTools(result.tools || []), ...LOCAL_TOOLS] };
  });
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const name = request.params?.name || '';
    if (name === FILE_TOOL) {
      try {
        return createDrawioArtifact(request.params?.arguments || {});
      } catch (error) {
        return {
          content: [{ type: 'text', text: `Error: ${error.message}` }],
          isError: true,
        };
      }
    }
    if (!SAFE_TOOLS.has(name)) {
      return {
        content: [{ type: 'text', text: `Error: tool not exposed: ${name}` }],
        isError: true,
      };
    }
    const client = await getChildClient();
    return client.callTool(request.params);
  });
  return server;
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function rpcError(res, status, message) {
  sendJson(res, status, {
    jsonrpc: '2.0',
    error: { code: -32000, message },
    id: null,
  });
}

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error('request body too large');
    chunks.push(chunk);
  }
  if (chunks.length === 0) return undefined;
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

async function handlePost(req, res) {
  let body;
  try {
    body = await readBody(req);
  } catch (error) {
    rpcError(res, 400, `invalid body: ${error.message}`);
    return;
  }
  const sessionId = req.headers['mcp-session-id'];
  let session = sessionId ? sessions.get(String(sessionId)) : undefined;

  if (!session && sessionId) {
    rpcError(res, 404, 'unknown MCP session');
    return;
  }
  if (!session) {
    if (!isInitializeRequest(body)) {
      rpcError(res, 400, 'initialize request required');
      return;
    }
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      onsessioninitialized: (id) => {
        sessions.set(id, { transport, server });
      },
    });
    const server = buildServer();
    session = { transport, server };
    transport.onclose = () => {
      const id = transport.sessionId;
      if (id) sessions.delete(id);
      // Do not call server.close() here: it closes this same transport and
      // re-enters onclose, which recurses until the stack overflows.
    };
    await server.connect(transport);
    await transport.handleRequest(req, res, body);
    return;
  }
  await session.transport.handleRequest(req, res, body);
}

async function handleSessionRequest(req, res) {
  const sessionId = req.headers['mcp-session-id'];
  const session = sessionId ? sessions.get(String(sessionId)) : undefined;
  if (!session) {
    sendJson(res, 400, { error: 'invalid or missing MCP session id' });
    return;
  }
  await session.transport.handleRequest(req, res);
}

const httpServer = http.createServer(async (req, res) => {
  const path = (req.url || '/').split('?', 1)[0];
  if (path === '/health' && req.method === 'GET') {
    sendJson(res, 200, { status: 'up', service: 'drawio-mcp' });
    return;
  }
  if (path !== '/mcp') {
    sendJson(res, 404, { error: 'not found' });
    return;
  }
  try {
    if (req.method === 'POST') {
      await handlePost(req, res);
      return;
    }
    if (req.method === 'GET' || req.method === 'DELETE') {
      await handleSessionRequest(req, res);
      return;
    }
    res.writeHead(405, { Allow: 'GET, POST, DELETE' });
    res.end();
  } catch (error) {
    console.error(`MCP request failed: ${error.stack || error}`);
    if (!res.headersSent) rpcError(res, 500, 'internal server error');
    else res.end();
  }
});

httpServer.listen(PORT, HOST, () => {
  console.error(`HDB draw.io MCP bridge listening on http://${HOST}:${PORT}/mcp`);
  console.error(`draw.io editor base: ${DRAWIO_BASE_URL}`);
});

async function shutdown() {
  for (const { transport } of sessions.values()) {
    try {
      await transport.close();
    } catch {
      // best effort
    }
  }
  sessions.clear();
  try {
    const client = await childPromise;
    await client?.close();
  } catch {
    // best effort
  }
  httpServer.close(() => process.exit(0));
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

export { httpServer };
