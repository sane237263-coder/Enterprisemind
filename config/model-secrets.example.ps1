# Local PowerShell configuration: set file paths only, never paste credentials here.
# Run from a PowerShell session with:
#   . ./config/model-secrets.example.ps1
# This neither starts services nor selects/enables providers.example.yaml.

$modelSecretRoot = Join-Path (Split-Path -Parent $PSScriptRoot) '.local/secrets'

# Use each capability's own local Secret file.
$env:EM_LLM_API_KEY_FILE = Join-Path $modelSecretRoot 'llm_api_key.txt'
$env:EM_VISION_API_KEY_FILE = Join-Path $modelSecretRoot 'vision_api_key.txt'

# One API key shared by Embedding and Rerank. Both roles keep independent config.
$env:EM_EMBEDDING_API_KEY_FILE = Join-Path $modelSecretRoot 'embedding_api_key.txt'
$env:EM_RERANK_API_KEY_FILE = $env:EM_EMBEDDING_API_KEY_FILE

# TC3 OCR uses a separate credential pair, not the custom OCR Bearer key.
$env:EM_OCR_SECRET_ID_FILE = Join-Path $modelSecretRoot 'ocr_secret_id.txt'
$env:EM_OCR_SECRET_KEY_FILE = Join-Path $modelSecretRoot 'ocr_secret_key.txt'

# Optional explicit HTTP proxy. Set it in your own shell/local environment file.
# $env:EM_MODEL_PROXY_URL = 'http://127.0.0.1:6696'

Remove-Variable modelSecretRoot
