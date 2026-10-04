<#
.SYNOPSIS
Deploys the SchwabEngine to the Google Compute Engine VM.

.DESCRIPTION
Packages the COMMITTED working tree (git archive: main.py, governor.py, core/, services/, execution/,
api/, config/, src/ and the rest - never node_modules, .git, the token vault or runtime state), uploads
it with .env to the "schwab-trader" VM and runs remote_deploy.sh, which builds the image (including the
dashboard), runs scripts/preflight.py inside it and only then swaps the container.

Live trading is opt-in:  .\deploy.ps1 -Live
The default deploys the DRY-RUN engine.
#>
param(
    [switch]$Live,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

$VM_NAME = "schwab-trader"
$ZONE = "us-central1-a"
$TARGET_DIR = "schwab_engine"
$PROJECT = "gen-lang-client-0334702303"

# Add gcloud to PATH for this script execution
$env:PATH += ";C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin"

if (-not (Test-Path .\.env)) { throw ".env not found in the project root." }

$dirty = git status --porcelain
if ($dirty -and -not $AllowDirty) {
    Write-Host "Uncommitted changes are NOT included in the upload (git archive ships HEAD):" -ForegroundColor Red
    Write-Host $dirty
    throw "Commit first, or re-run with -AllowDirty to deploy HEAD anyway."
}

Write-Host "Starting deployment to $VM_NAME ($ZONE) in project $PROJECT..." -ForegroundColor Cyan

$archive = Join-Path $env:TEMP "schwab_engine_deploy.tgz"
Write-Host "0. Packaging HEAD $(git rev-parse --short HEAD)..." -ForegroundColor Yellow
git archive --format=tar.gz -o $archive HEAD

Write-Host "1. Preparing the VM directory..." -ForegroundColor Yellow
gcloud.cmd compute ssh --quiet $VM_NAME --zone=$ZONE --project=$PROJECT --command="mkdir -p $TARGET_DIR"

Write-Host "2. Uploading archive and .env..." -ForegroundColor Yellow
gcloud.cmd compute scp --quiet $archive ${VM_NAME}:${TARGET_DIR}/deploy.tgz --zone=$ZONE --project=$PROJECT
gcloud.cmd compute scp --quiet .\.env ${VM_NAME}:${TARGET_DIR}/.env --zone=$ZONE --project=$PROJECT

$flags = ""
if ($Live) { $flags = "--live" }
Write-Host "3. Extracting, pre-flighting and deploying (engine flags: '$flags')..." -ForegroundColor Yellow
$remote = "cd $TARGET_DIR && tar -xzf deploy.tgz && rm deploy.tgz && ENGINE_FLAGS='$flags' bash remote_deploy.sh"
gcloud.cmd compute ssh --quiet $VM_NAME --zone=$ZONE --project=$PROJECT --command=$remote

Remove-Item $archive -ErrorAction SilentlyContinue
Write-Host "Deployment script execution finished." -ForegroundColor Green
Write-Host "Dashboard: gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT -- -L 8080:localhost:8080   then open http://localhost:8080" -ForegroundColor Cyan
