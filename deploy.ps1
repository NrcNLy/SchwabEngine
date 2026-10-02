<#
.SYNOPSIS
Deploys the Schwab Day-Trading Engine to a Google Compute Engine VM.

.DESCRIPTION
This script uses gcloud to copy the project files to the "schwab-trader" VM in "us-central1-a",
installs Docker if needed, builds the Docker image, and runs it with the necessary volume mounts.
#>

$VM_NAME = "schwab-trader"
$ZONE = "us-central1-a"
$TARGET_DIR = "schwab_engine"
$PROJECT = "gen-lang-client-0334702303"

# Add gcloud to PATH for this script execution
$env:PATH += ";C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin"

Write-Host "Starting deployment to $VM_NAME ($ZONE) in project $PROJECT..." -ForegroundColor Cyan

# 0. Create target directory to prevent SCP errors
Write-Host "0. Creating target directory on VM..." -ForegroundColor Yellow
echo y | gcloud.cmd compute ssh --quiet $VM_NAME --zone=$ZONE --project=$PROJECT --command="mkdir -p $TARGET_DIR"

# 1. Copy the current directory to the VM (excluding .dockerignore files)
Write-Host "1. Copying project files to VM..." -ForegroundColor Yellow
echo y | gcloud.cmd compute scp --quiet --recurse .\* ${VM_NAME}:${TARGET_DIR} --zone=$ZONE --project=$PROJECT
# Explicitly copy the .env file since Windows wildcards skip hidden files
echo y | gcloud.cmd compute scp --quiet .\.env ${VM_NAME}:${TARGET_DIR}/.env --zone=$ZONE --project=$PROJECT

# 2. SSH into the VM to install Docker, build, and run the container
Write-Host "2. Executing remote build and deployment commands..." -ForegroundColor Yellow
echo y | gcloud.cmd compute ssh --quiet $VM_NAME --zone=$ZONE --project=$PROJECT --command="bash $TARGET_DIR/remote_deploy.sh"

Write-Host "Deployment script execution finished." -ForegroundColor Green
Write-Host "NOTE: Ensure your .env file containing API keys exists in $TARGET_DIR on the VM!" -ForegroundColor Red
