# Puts a newly generated Podium client secret into all three Brooks apps that use Podium:
# the PestRoutes -> Podium Sync, the Missed Call Texter and the IS Dashboard.
#
# Use it whenever the client secret is regenerated in Podium's developer portal
# (the old secret stops working at once and every app starts failing with 401 invalid_client).
#
#   powershell -ExecutionPolicy Bypass -File "C:\Users\dalli\Projects\Brooks_ISA_Dashboard\deploy\podium_set_secret.ps1"
#
# You paste the secret once. It is not shown on screen, not written on this PC, and not put on
# a command line. Step 2 only ASKS Podium whether the secret works for each app; if Podium says
# no, the script stops and nothing on any server has changed. Safe to run again.
# (Keep this file plain ASCII: Windows PowerShell 5.1 misreads other characters.)

$ErrorActionPreference = "Stop"
$Key    = "$HOME\.ssh\id_ed25519"
$Helper = "$PSScriptRoot\podium_set_secret.py"
$Remote = "/root/podium_set_secret.py"
$Boxes  = @(
    @{ Server = "root@147.182.132.161"; Apps = "sync texter" },
    @{ Server = "root@137.184.60.159";  Apps = "dashboard" }
)

$secure = Read-Host "Paste the new Podium client secret, then press Enter (it will not show)" -AsSecureString
$ptr    = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$plain  = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
if (-not $plain -or -not $plain.Trim()) { throw "Nothing was pasted. Nothing was changed." }
$plain = $plain.Trim()

$reauth = $false
$problem = $false
try {
    Write-Host ""
    Write-Host "1/3  Copying the helper to the two servers..."
    foreach ($b in $Boxes) {
        scp -q -i $Key $Helper "$($b.Server):$Remote"
        if ($LASTEXITCODE -ne 0) { throw "STOP: could not copy the helper to $($b.Server). Nothing was changed." }
    }

    Write-Host "2/3  Asking Podium whether it accepts the new secret for each app (nothing is changed yet)..."
    foreach ($b in $Boxes) {
        $plain | ssh -i $Key $b.Server "python3 $Remote check $($b.Apps)"
        if ($LASTEXITCODE -eq 4) { $reauth = $true }
        elseif ($LASTEXITCODE -eq 2) { throw "STOP: Podium rejected that secret. Nothing was changed. Copy the secret again from the Podium developer portal (the whole thing, no spaces) and run this again." }
        elseif ($LASTEXITCODE -ne 0) { throw "STOP: the check did not finish cleanly (see the lines above). Nothing was changed. Tell Claude what it printed." }
    }

    Write-Host "3/3  Saving the new secret and restarting each app..."
    foreach ($b in $Boxes) {
        $plain | ssh -i $Key $b.Server "python3 $Remote apply $($b.Apps)"
        if ($LASTEXITCODE -eq 4) { $reauth = $true }
        elseif ($LASTEXITCODE -ne 0) { $problem = $true }
    }
}
finally {
    $plain = $null
    $secure = $null
    foreach ($b in $Boxes) { ssh -i $Key $b.Server "rm -f $Remote" | Out-Null }
}

Write-Host ""
if ($problem) {
    Write-Host "Finished WITH PROBLEMS - see the PROBLEM lines above and tell Claude what they say."
} elseif ($reauth) {
    Write-Host "The secret is in place, but at least one app (the REAUTH lines above) must be re-authorized in Podium."
    Write-Host "Tell Claude which ones; that is a separate, short step."
} else {
    Write-Host "Done. All three apps have the new secret and are running."
    Write-Host "Tell Claude it is done - the customers and callers missed while Podium was down are NOT caught up yet."
}
