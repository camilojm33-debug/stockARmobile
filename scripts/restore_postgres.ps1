param(
    [Parameter(Mandatory = $true)]
    [string]$Host,
    [Parameter(Mandatory = $true)]
    [int]$Port,
    [Parameter(Mandatory = $true)]
    [string]$Database,
    [Parameter(Mandatory = $true)]
    [string]$Username,
    [Parameter(Mandatory = $true)]
    [string]$InputFile,
    [string]$Password = ""
)

if (-not (Get-Command pg_restore -ErrorAction SilentlyContinue)) {
    throw "pg_restore no esta disponible en PATH."
}

if ($Password) {
    $env:PGPASSWORD = $Password
}

pg_restore --host $Host --port $Port --username $Username --clean --if-exists --no-owner --no-privileges --dbname $Database $InputFile
if ($LASTEXITCODE -ne 0) {
    throw "Restore PostgreSQL fallo con codigo $LASTEXITCODE"
}

Write-Host "Restore completado desde: $InputFile"
