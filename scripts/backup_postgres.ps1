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
    [string]$OutputFile,
    [string]$Password = ""
)

if (-not (Get-Command pg_dump -ErrorAction SilentlyContinue)) {
    throw "pg_dump no esta disponible en PATH."
}

if ($Password) {
    $env:PGPASSWORD = $Password
}

pg_dump --host $Host --port $Port --username $Username --format custom --file $OutputFile $Database
if ($LASTEXITCODE -ne 0) {
    throw "Backup PostgreSQL fallo con codigo $LASTEXITCODE"
}

Write-Host "Backup generado: $OutputFile"
