param(
    [Parameter(Mandatory=$true)][string]$Path
)
$ErrorActionPreference = 'Stop'
try {
    if (-not (Test-Path -LiteralPath $Path)) { throw "file not found: $Path" }
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.System.UserProfile.LockScreen, Windows.System.UserProfile, ContentType = WindowsRuntime]

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1)
    $asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1)
    $getFile = $asTaskGeneric.MakeGenericMethod([Windows.Storage.StorageFile])

    $op = [Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)
    $file = $getFile.Invoke($null, @($op)).GetAwaiter().GetResult()
    $setOp = [Windows.System.UserProfile.LockScreen]::SetImageFileAsync($file)
    $asTaskAction.Invoke($null, @($setOp)).GetAwaiter().GetResult()
    Write-Output "OK"
    exit 0
} catch {
    Write-Output ("ERROR: " + $_.Exception.Message)
    exit 1
}
