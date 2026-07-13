param(
    [Parameter(Mandatory = $true)]
    [string]$Host,
    [Parameter(Mandatory = $true)]
    [int]$Port,
    [Parameter(Mandatory = $true)]
    [string]$Database,
    [Parameter(Mandatory = $true)]
    [string]$Username,
    [string]$Password = ""
)

if (-not (Get-Command psql -ErrorAction SilentlyContinue)) {
    throw "psql no esta disponible en PATH."
}

if ($Password) {
    $env:PGPASSWORD = $Password
}

$query = @"
SELECT
  (SELECT count(*) FROM users) AS users_count,
  (SELECT count(*) FROM products) AS products_count,
  (SELECT count(*) FROM sales) AS sales_count,
  (SELECT count(*) FROM payments) AS payments_count;
"@

psql --host $Host --port $Port --username $Username --dbname $Database --command $query
if ($LASTEXITCODE -ne 0) {
    throw "Verificacion PostgreSQL fallo con codigo $LASTEXITCODE"
}

Write-Host "Verificacion PostgreSQL completada."
