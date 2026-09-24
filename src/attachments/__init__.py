"""Chat attachment domain: session-private temporary data sources.

Implements the target architecture from
``docs/superpowers/specs/2026-09-03-local-chat-attachment-routing-design.md``:

- ``store``       : durable records (attachments / assets / parts / jobs)
- ``service``     : lifecycle operations + ACL (upload/list/get/delete/retry)
- ``router``      : extension -> AttachmentProcessor routing (parallel to,
                    not replacing, the KB ``PipelineRegistry``)
- ``jobs``/``worker`` : background deterministic parsing
- ``coordinator`` : resolves ``waiting_for_attachments`` turns when assets settle
- ``retrieval``   : local lexical retrieval tuned for hardware identifiers

Chat attachments never enter RAGFlow or the knowledge base; they are read
through scoped tools only, with ACL re-verified on every call.
"""
