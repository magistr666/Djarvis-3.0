# Дим-накладка: полностью чёрный безрамочный топмост поверх всех мониторов.
# Используется вместо выключения монитора, чтобы не трогать питание/аудио.
# Закрывается по WM_CLOSE к окну с заголовком JARVIS_DIM.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
$f = New-Object System.Windows.Forms.Form
$f.Text = "JARVIS_DIM"
$f.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$f.TopMost = $true
$f.ShowInTaskbar = $false
$f.BackColor = [System.Drawing.Color]::Black
$f.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$f.SetBounds($vs.X, $vs.Y, $vs.Width, $vs.Height)
$f.Opacity = 0.99
$f.TopLevel = $true
$f.Show()
while (-not $f.IsDisposed) {
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 50
}