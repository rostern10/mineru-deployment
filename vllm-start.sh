#!/bin/bash
# Qwen vLLM 启动参数（不要改）
docker run -d --name vllm-qwen36 --gpus all -p 8001:8001 \
  -v /home/szssyyy/models/qwen36-fp8:/models/qwen36-fp8 \
  --network shared-net \
  vllm/vllm-openai:latest \
  /models/qwen36-fp8 \
  --served-model-name qwen3.6-35b \
  --host 0.0.0.0 --port 8001 \
  --api-key data5406 \
  --max-model-len 131072 \
  --gpu-memory-utilization 0.50 \
  --max-num-seqs 3 \
  --max-num-batched-tokens 16384 \
  --enable-prefix-caching \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --default-chat-template-kwargs '{"enable_thinking": false}'
