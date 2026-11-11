# Deploys the "Unmapped tracking numbers" + "Unmapped forms" section of the Sourcing App card (2026-11-03).
# Safe to re-run. Stops before copying anything if the server's files no longer match
# the pre-patch backups (meaning someone else deployed in between).
#
#   powershell -ExecutionPolicy Bypass -File "C:\Users\dalli\Projects\Brooks_ISA_Dashboard\deploy\push_unmapped_numbers.ps1"

$ErrorActionPreference = "Stop"
$Key    = "$HOME\.ssh\id_ed25519"
$Server = "root@137.184.60.159"
$Root   = Split-Path -Parent $PSScriptRoot          # the project folder
$App    = "/home/isdash/brooks-is-dash"
$Files  = @("src/isadash/software.py", "web/app.js", "web/style.css", "web/index.html")

Write-Host "1/5  Checking the server still has the files these patches were made from..."
foreach ($f in $Files) {
    $remote = (ssh -i $Key $Server "sha256sum $App/$f").Split(" ")[0].ToLower()
    $backup = "$Root\$($f.Replace('/', '\')).bak_unmapped"
    $local  = (Get-FileHash -Algorithm SHA256 $backup).Hash.ToLower()
    if ($remote -ne $local) {
        throw "STOP: $f on the server differs from the pre-patch backup. Another deploy happened in between - have Claude re-check before pushing."
    }
    Write-Host "     ok   $f"
}

Write-Host "2/5  Copying the dashboard files up..."
scp -q -i $Key "$Root\src\isadash\software.py" "${Server}:$App/src/isadash/software.py"
scp -q -i $Key "$Root\web\app.js" "$Root\web\style.css" "$Root\web\index.html" "${Server}:$App/web/"
scp -q -i $Key "$PSScriptRoot\verify_unmapped.py" "${Server}:/tmp/verify_unmapped.py"

Write-Host "3/5  Installing the daily unmapped-forms count (script + cron)..."
scp -q -i $Key "$Root\scripts\unmapped_forms_watch.py" "${Server}:$App/scripts/unmapped_forms_watch.py"
scp -q -i $Key "$PSScriptRoot\isdash-unmapped-forms.cron" "${Server}:/etc/cron.d/isdash-unmapped-forms"
ssh -i $Key $Server "chown isdash:isdash $App/scripts/unmapped_forms_watch.py; chown root:root /etc/cron.d/isdash-unmapped-forms; chmod 644 /etc/cron.d/isdash-unmapped-forms; cd /opt/lead-attribution; .venv/bin/python -m py_compile $App/scripts/unmapped_forms_watch.py; echo cron installed: `$(cat /etc/cron.d/isdash-unmapped-forms | grep -c unmapped_forms_watch) line"

Write-Host "4/5  Restarting the dashboard service and checking it came back..."
ssh -i $Key $Server "cd $App; chown isdash:isdash src/isadash/software.py web/app.js web/style.css web/index.html; sudo -u isdash .venv/bin/python -m py_compile src/isadash/software.py; systemctl restart brooks-is-dash.service; sleep 3; echo service: `$(systemctl is-active brooks-is-dash.service); curl -s -o /dev/null -w 'public site: HTTP %{http_code}\n' https://isdash.brookspest.com/; curl -s https://isdash.brookspest.com/ | grep -o 'app.js?v=[0-9]*\|style.css?v=[0-9]*'; journalctl -u brooks-is-dash.service --since -60s | grep -i 'error\|traceback' || echo 'journal: no errors'"

Write-Host "5/5  Starting the first 30-day forms backfill in the background (about 45 min; the card fills in when it finishes)..."
ssh -i $Key $Server "cd /opt/lead-attribution; nohup .venv/bin/python $App/scripts/unmapped_forms_watch.py --days 30 >> $App/data/unmapped_forms.log 2>&1 < /dev/null & sleep 1; echo backfill started, log: $App/data/unmapped_forms.log"

Write-Host ""
Write-Host "What the calls part of the card will show right now (read-only check on the server):"
ssh -i $Key $Server "cd $App; sudo -u isdash .venv/bin/python -I /tmp/verify_unmapped.py; rm -f /tmp/verify_unmapped.py"

Write-Host ""
Write-Host "Done. Open the dashboard, Settings > Software Management, expand the Sourcing App card."
Write-Host "The forms count reads 'no count on file yet' until the backfill finishes; check progress with:"
Write-Host "   ssh -i $Key $Server 'tail -5 $App/data/unmapped_forms.log'"
