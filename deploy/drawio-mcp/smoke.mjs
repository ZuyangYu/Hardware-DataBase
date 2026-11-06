#!/usr/bin/env node
/** End-to-end smoke test for the remote draw.io MCP bridge. */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const url = process.env.DRAWIO_MCP_URL || 'http://127.0.0.1:8091/mcp';
const transport = new StreamableHTTPClientTransport(new URL(url));
const client = new Client(
  { name: 'hdb-drawio-mcp-smoke', version: '1.0.0' },
  { capabilities: {} },
);

try {
  await client.connect(transport);
  const tools = await client.listTools();
  const names = (tools.tools || []).map((tool) => tool.name).sort();
  console.log(`tools (${names.length}): ${names.join(', ')}`);
  for (const required of ['open_drawio_mermaid', 'create_drawio_file']) {
    if (!names.includes(required)) throw new Error(`${required} was not exposed`);
  }
  const result = await client.callTool({
    name: 'open_drawio_mermaid',
    arguments: { content: 'flowchart LR\n  A[需求] --> B[设计] --> C[验证]' },
  });
  const text = (result.content || [])
    .filter((item) => item.type === 'text')
    .map((item) => item.text)
    .join('\n');
  if (result.isError) throw new Error(text || 'draw.io tool returned an error');
  const link = text.match(/https?:\/\/\S+/)?.[0] || '';
  if (!link) throw new Error('draw.io tool did not return an editor URL');
  console.log(`ok: editor URL ${link.slice(0, 100)}${link.length > 100 ? '…' : ''}`);
  console.log(`result chars: ${text.length}`);

  const file = await client.callTool({
    name: 'create_drawio_file',
    arguments: {
      filename: 'smoke-flow.drawio',
      content:
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>' +
        '<mxCell id="2" value="Hello" style="rounded=1;whiteSpace=wrap;html=1;" vertex="1" parent="1">' +
        '<mxGeometry x="80" y="80" width="120" height="60" as="geometry"/></mxCell></root></mxGraphModel>',
    },
  });
  const artifact = file.structuredContent || {};
  if (file.isError || artifact.filename !== 'smoke-flow.drawio' || !artifact.sha256) {
    throw new Error('create_drawio_file did not return an artifact');
  }
  console.log(`ok: artifact ${artifact.filename} sha256=${artifact.sha256.slice(0, 12)}…`);
  const wrapped = await client.callTool({
    name: 'create_drawio_file',
    arguments: {
      filename: 'wrapped.drawio',
      content:
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel>' +
        '</content><parameter name="filename">wrapped.drawio',
    },
  });
  if (wrapped.isError || wrapped.structuredContent?.filename !== 'wrapped.drawio') {
    throw new Error('trailing tool-wrapper tags were not tolerated');
  }
  console.log('ok: trailing tool-wrapper tags tolerated');

} finally {
  await transport.close();
}
