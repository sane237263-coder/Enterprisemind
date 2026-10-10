# Copy to .local/model-env.ps1, then dot-source that copy in each PowerShell terminal.
# This script sets paths only. It does not read secrets, contact providers or start services.
# It works from config/ or .local/; both directories are immediately below the repo root.
$emModelRoot = Split-Path -Parent $PSScriptRoot
$emModelSecretRoot = Join-Path $emModelRoot '.local/secrets'

# Safe baseline: all five providers remain null. Copying this template does not enable models.
$env:EM_PROVIDERS_CONFIG = Join-Path $emModelRoot 'config/providers.yaml'
$env:EM_RAG_CONFIG = Join-Path $emModelRoot 'config/rag.yaml'

# Opt in only after preparing credentials, matching tokenizers and reviewing both local YAMLs:
# $env:EM_PROVIDERS_CONFIG = Join-Path $emModelRoot 'config/providers.local.yaml'
# $env:EM_RAG_CONFIG = Join-Path $emModelRoot 'config/rag.local.yaml'
# WSL systemd runtime uses scripts/local_wsl_prepare.py instead; it does not load this script.

$env:EM_LLM_API_KEY_FILE = Join-Path $emModelSecretRoot 'llm_api_key.txt'
$env:EM_EMBEDDING_API_KEY_FILE = Join-Path $emModelSecretRoot 'embedding_api_key.txt'
$env:EM_RERANK_API_KEY_FILE = Join-Path $emModelSecretRoot 'rerank_api_key.txt'
$env:EM_VISION_API_KEY_FILE = Join-Path $emModelSecretRoot 'vision_api_key.txt'
$env:EM_OCR_API_KEY_FILE = Join-Path $emModelSecretRoot 'ocr_api_key.txt'
$env:EM_OCR_SECRET_ID_FILE = Join-Path $emModelSecretRoot 'ocr_secret_id.txt'
$env:EM_OCR_SECRET_KEY_FILE = Join-Path $emModelSecretRoot 'ocr_secret_key.txt'

# Optional explicit model proxy; supply your own reachable endpoint when needed.
# $env:EM_MODEL_PROXY_URL = 'http://127.0.0.1:<your-proxy-port>'
Remove-Variable emModelRoot, emModelSecretRoot
