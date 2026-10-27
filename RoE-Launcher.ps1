# RoE-Launcher.ps1 - self-contained RoE private-server launcher.
# Everything resolves relative to this kit folder; no external paths.
$ErrorActionPreference = 'Stop'
$ROOT    = $PSScriptRoot
$PY      = Join-Path $ROOT '.tools\python\python.exe'
$PATCHER = Join-Path $ROOT 'tools\apply_patches.py'
$SETTINGS = Join-Path $ROOT 'launcher-settings.json'
$EXE_SHA256 = '2dca9b1e122e21363e14eeeb2372598c773205a907ed8d2b2d54a7229ad6ba18'

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Windows.Forms

$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        Title="RoE Private Server Launcher" Width="430" Height="400"
        WindowStartupLocation="CenterScreen" ResizeMode="NoResize"
        Background="#FF1E1E28" Foreground="White" FontFamily="Segoe UI" FontSize="12">
  <Grid Margin="14">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/><RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <StackPanel Grid.Row="0" Orientation="Horizontal" Margin="0,0,0,10">
      <TextBlock Text="Game files:" VerticalAlignment="Center" Width="80"/>
      <TextBox Name="CliDir" Width="250" VerticalContentAlignment="Center"
               Background="#FF2B2B38" Foreground="White" BorderBrush="#FF555566"/>
      <Button Name="Browse" Content="..." Width="34" Margin="6,0,0,0"/>
    </StackPanel>

    <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,8">
      <CheckBox Name="BotsOn" Content="Bots" IsChecked="True" VerticalAlignment="Center" Width="80"/>
      <TextBlock Text="count:" VerticalAlignment="Center"/>
      <ComboBox Name="BotCount" Width="70" Margin="6,0,0,0" SelectedIndex="4">
        <ComboBoxItem>4</ComboBoxItem><ComboBoxItem>8</ComboBoxItem>
        <ComboBoxItem>12</ComboBoxItem><ComboBoxItem>16</ComboBoxItem>
        <ComboBoxItem>20</ComboBoxItem><ComboBoxItem>24</ComboBoxItem>
        <ComboBoxItem>32</ComboBoxItem><ComboBoxItem>48</ComboBoxItem>
      </ComboBox>
    </StackPanel>

    <StackPanel Grid.Row="2" Orientation="Horizontal" Margin="0,0,0,8">
      <CheckBox Name="StormOn" Content="Storm (zone shrinks)" IsChecked="False" VerticalAlignment="Center"/>
      <TextBlock Text="pace:" VerticalAlignment="Center" Margin="14,0,0,0"/>
      <ComboBox Name="StormPace" Width="140" Margin="6,0,0,0" SelectedIndex="0">
        <ComboBoxItem>Normal (~3 min)</ComboBoxItem>
        <ComboBoxItem>Slow x2 (~6 min)</ComboBoxItem>
        <ComboBoxItem>Slow x3 (~10 min)</ComboBoxItem>
        <ComboBoxItem>Slow x5 (~16 min)</ComboBoxItem>
        <ComboBoxItem>Slow x10 (~31 min)</ComboBoxItem>
      </ComboBox>
    </StackPanel>
    <CheckBox Grid.Row="3" Name="HalfHp"  Content="Half health (testing)" IsChecked="True" Margin="0,0,0,8"/>
    <CheckBox Grid.Row="4" Name="ResetUserData" Content="Reset game settings (fixes glider-landing bug)" IsChecked="False" Margin="0,0,0,8"/>

    <StackPanel Grid.Row="5" Orientation="Horizontal" Margin="0,0,0,8">
      <TextBlock Text="Clients:" VerticalAlignment="Center" Width="80"/>
      <RadioButton Name="OneClient" Content="1" IsChecked="True" Margin="0,0,14,0"/>
      <RadioButton Name="TwoClients" Content="2 (duo, client2 patched)"/>
    </StackPanel>

    <StackPanel Grid.Row="6" Orientation="Horizontal" Margin="0,0,0,4">
      <TextBlock Text="Map:" VerticalAlignment="Center" Width="80"/>
      <ComboBox Width="240" IsEnabled="False" SelectedIndex="0">
        <ComboBoxItem>Winter Island (16010) - only map supported</ComboBoxItem>
      </ComboBox>
    </StackPanel>

    <TextBlock Grid.Row="7" Name="Status" TextWrapping="Wrap" Foreground="#FF9ACDFF"
               VerticalAlignment="Bottom" Margin="0,4"
               Text="Point 'Game files' at your RoE install folder (contains Europa_Client.exe)."/>

    <Button Grid.Row="8" Name="Launch" Content="Launch" Height="34"
            Background="#FF2E6BE6" Foreground="White" FontWeight="SemiBold"/>
  </Grid>
</Window>
"@

$win = [Windows.Markup.XamlReader]::Parse($xaml)
$ui = @{}
'CliDir','Browse','BotsOn','BotCount','StormOn','StormPace','HalfHp','ResetUserData','OneClient','TwoClients','Status','Launch' |
  ForEach-Object { $ui[$_] = $win.FindName($_) }
if (Test-Path $SETTINGS) {
    try { $ui.CliDir.Text = (Get-Content $SETTINGS -Raw | ConvertFrom-Json).clientDir } catch {}
}

# Corrupted UserData wedges the glider-landing FSM (PROGRESS 2026-10-22):
# ECT_High in CurrentSettings6.xml is the observed poison marker.
function Test-PoisonedUserData([string]$clidir) {
    if ([string]::IsNullOrWhiteSpace($clidir)) { return $false }
    $cs = Join-Path $clidir 'UserData\CurrentSettings6.xml'
    return (Test-Path $cs) -and ((Get-Content $cs -Raw) -match 'ECT_High')
}
function Update-UserDataWarning {
    if (Test-PoisonedUserData $ui.CliDir.Text.Trim()) {
        $ui.ResetUserData.IsChecked = $true
        $ui.Status.Text = "Corrupted UserData detected (glider-landing bug). 'Reset game settings' is pre-checked - Launch will back it up and wipe it."
    }
}
$ui.CliDir.Add_LostFocus({ Update-UserDataWarning })
Update-UserDataWarning

$ui.Browse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.SelectedPath = $ui.CliDir.Text
    $dlg.Description = 'Folder containing Europa_Client.exe'
    if ($dlg.ShowDialog() -eq 'OK') { $ui.CliDir.Text = $dlg.SelectedPath }
})

function LatestRun {
    Get-ChildItem (Join-Path $ROOT 'runs') -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
}

function Wait-LobbyUid([string]$runPath, [string]$uid) {
    for ($i = 0; $i -lt 40; $i++) {
        $hit = Get-ChildItem (Join-Path $runPath 'lobby\*\events.jsonl') -ErrorAction SilentlyContinue |
               Select-String -Pattern ('0x' + $uid) -Quiet
        if ($hit) { return $true }
        Start-Sleep 10
    }
    return $false
}

$ui.Launch.Add_Click({
    $ui.Launch.IsEnabled = $false
    try {
        $clidir = $ui.CliDir.Text.Trim()
        if ([string]::IsNullOrWhiteSpace($clidir)) { throw 'Pick your Ring of Elysium folder first (Browse).' }
        $cli = Join-Path $clidir 'Europa_Client.exe'
        if (-not (Test-Path $cli)) { throw "Europa_Client.exe not found in $clidir" }
        if (Get-Process Europa_Client -ErrorAction SilentlyContinue) {
            throw 'Europa_Client.exe already running - close it first.'
        }
        $hash = (Get-FileHash $cli -Algorithm SHA256).Hash.ToLower()
        if ($hash -ne $EXE_SHA256) { throw "Europa_Client.exe hash mismatch - expected the 2022 client ($EXE_SHA256), got $hash." }
        @{ clientDir = $clidir } | ConvertTo-Json | Set-Content $SETTINGS -Encoding UTF8

        if ($ui.ResetUserData.IsChecked) {
            $ud = Join-Path $clidir 'UserData'
            if (Test-Path $ud) {
                $bak = "$ud.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
                Move-Item $ud $bak
                New-Item -ItemType Directory -Path $ud | Out-Null
                $ui.Status.Text = "UserData reset (backup: $bak). Starting services..."
            }
            $ui.ResetUserData.IsChecked = $false
            [System.Windows.Forms.Application]::DoEvents()
        }

        # --- services -------------------------------------------------
        $run = $null
        if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) {
            $ui.Status.Text = 'Services already on :8001 - reusing latest run.'
            $run = LatestRun
        } else {
            $arglist = @('-X','utf8','-B','-u','tools\windows_launch.py',
                         '--mode','cycle','--room-service','--services-only',
                         '--winter-doors','--loot-randomize','--live-control',
                         '--duration','86400')
            if (-not $ui.StormOn.IsChecked) { $arglist += '--no-storm' }
            elseif ($ui.StormPace.SelectedIndex -gt 0) {
                $arglist += @('--cycle-scale', (@(1,2,3,5,10))[$ui.StormPace.SelectedIndex])
            }
            if ($ui.BotsOn.IsChecked) {
                $arglist += @('--shared-bots', $ui.BotCount.Text)
            }
            if ($ui.HalfHp.IsChecked) { $arglist += @('--hp-floor','500') }

            $before = (LatestRun).Name
            $tag = Get-Date -Format 'yyyyMMdd-HHmmss'
            $ui.Status.Text = 'Starting services...'
            [System.Windows.Forms.Application]::DoEvents()
            Start-Process -FilePath $PY -WorkingDirectory $ROOT -ArgumentList $arglist `
                -RedirectStandardOutput (Join-Path $ROOT "logs\services-$tag.out.log") `
                -RedirectStandardError  (Join-Path $ROOT "logs\services-$tag.err.log")
            for ($i = 0; $i -lt 90; $i++) {
                Start-Sleep 2
                $r = LatestRun
                if ($r -and $r.Name -ne $before) { $run = $r }
                if ($run -and (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue)) { break }
            }
        }
        if (-not $run -or -not (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue)) {
            throw 'Proxy never listened on :8001 - check logs\services-*.err.log'
        }
        $ui.Status.Text = "Services up ($($run.Name)). Launching client..."
        [System.Windows.Forms.Application]::DoEvents()

        # --- clients ----------------------------------------------------
        $env:_CE_ROOM_PERMISSION_ = '1'
        $mkargs = { param($uid) '-language=en','-garena',"-uid=$uid",'-server=127.0.0.1:8001','-_CE_ROOM_PERMISSION_=1' }

        $c1 = Start-Process -FilePath $cli -WorkingDirectory $clidir -PassThru -ArgumentList (& $mkargs 1)
        if (-not (Wait-LobbyUid $run.FullName '5030000140000')) {
            throw 'Client 1 did not reach the lobby - check the lobby log.'
        }
        $ui.Status.Text = "Client 1 in lobby (PID $($c1.Id))."
        [System.Windows.Forms.Application]::DoEvents()

        if ($ui.TwoClients.IsChecked) {
            $c2 = Start-Process -FilePath $cli -WorkingDirectory $clidir -PassThru -ArgumentList (& $mkargs 2)
            if (-not (Wait-LobbyUid $run.FullName '5030000140001')) {
                throw 'Client 2 did not reach the lobby - check the lobby log.'
            }
            $ui.Status.Text = 'Client 2 in lobby - 60s settle before patching...'
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep 60
            & $PY -X utf8 -B $PATCHER $c2.Id --wait-lobby $run.FullName | Out-Null
            $ui.Status.Text = "Done. Client1 creates the room; client2 joins via list."
        } else {
            $ui.Status.Text = 'Done. Create a room via Customs and press Start.'
        }
    } catch {
        $ui.Status.Text = "ERROR: $($_.Exception.Message)"
    } finally {
        $ui.Launch.IsEnabled = $true
    }
})

$win.ShowDialog() | Out-Null
