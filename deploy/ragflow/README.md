# RAGFlow SiliconFlow timeout overlay

`embedding_model.py` is the RAGFlow v0.26.4 module used by the running
`ragflow-cpu` service.  The overlay keeps the upstream provider behaviour and
changes SiliconFlow embedding calls to use the validated
`SILICONFLOW_EMBEDDING_TIMEOUT_SECONDS` setting, defaulting to 100 seconds.

The deployment compose file mounts this module read-only at
`/ragflow/rag/llm/embedding_model.py`, and its `.env` sets the value to `100`.
The mount makes the change survive container recreation without rebuilding the
RAGFlow image.  If the RAGFlow image is upgraded, refresh this file from the
new image and reapply the small timeout change before restarting the service.
