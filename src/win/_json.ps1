# Minimal JSON emitter - works on PowerShell 2.0 (Win7) and up.
function ConvertTo-JsonCompat {
    param([Parameter(ValueFromPipeline = $true)] $obj)
    if ($null -eq $obj) { return 'null' }
    switch ($obj.GetType().Name) {
        'Boolean'    { return $obj.ToString().ToLower() }
        'Int32'      { return $obj.ToString() }
        'Int64'      { return $obj.ToString() }
        'Double'     { return $obj.ToString([System.Globalization.CultureInfo]::InvariantCulture) }
        'String'     {
            $s = $obj -replace '\\', '\\' -replace '"', '\"' -replace "`r", '\r' -replace "`n", '\n' -replace "`t", '\t'
            return '"' + $s + '"'
        }
        'Hashtable'  {
            $parts = @()
            foreach ($k in $obj.Keys) { $parts += ('"' + $k + '":' + (ConvertTo-JsonCompat $obj[$k])) }
            return '{' + ($parts -join ',') + '}'
        }
        'Object[]'   {
            $parts = @()
            foreach ($v in $obj) { $parts += (ConvertTo-JsonCompat $v) }
            return '[' + ($parts -join ',') + ']'
        }
        default      {
            $s = "$obj" -replace '\\', '\\' -replace '"', '\"'
            return '"' + $s + '"'
        }
    }
}
