# Centra la K del archivo k-mark.png en una imagen cuadrada
# Uso: powershell -ExecutionPolicy Bypass -File scripts/center-k.ps1

Add-Type -AssemblyName System.Drawing

$src = "C:\Users\NicoPC\Desktop\inventario\phylloinventory\frontend\public\k-mark.png"
$dst = $src  # sobreescribe el mismo archivo

$bmp = [System.Drawing.Image]::FromFile($src)
Write-Host "Original: $($bmp.Width)x$($bmp.Height)"

# Encontrar el bounding box del contenido no-blanco
$minX = $bmp.Width
$minY = $bmp.Height
$maxX = 0
$maxY = 0

for ($y = 0; $y -lt $bmp.Height; $y++) {
    for ($x = 0; $x -lt $bmp.Width; $x++) {
        $pixel = $bmp.GetPixel($x, $y)
        # No es blanco
        if ($pixel.R -lt 240 -or $pixel.G -lt 240 -or $pixel.B -lt 240) {
            if ($x -lt $minX) { $minX = $x }
            if ($x -gt $maxX) { $maxX = $x }
            if ($y -lt $minY) { $minY = $y }
            if ($y -gt $maxY) { $maxY = $y }
        }
    }
}

$contentW = $maxX - $minX + 1
$contentH = $maxY - $minY + 1
Write-Host "K detectada: ${contentW}x${contentH} en posicion ($minX,$minY)"

# Crear imagen cuadrada con padding
$pad = 20
$size = [Math]::Max($contentW, $contentH) + 2 * $pad
$newBmp = New-Object System.Drawing.Bitmap $size, $size
$g = [System.Drawing.Graphics]::FromImage($newBmp)
$g.Clear([System.Drawing.Color]::White)

# Centrar la K en la nueva imagen
$offsetX = [int](($size - $contentW) / 2)
$offsetY = [int](($size - $contentH) / 2)
$srcRect = New-Object System.Drawing.Rectangle $minX, $minY, $contentW, $contentH
$dstRect = New-Object System.Drawing.Rectangle $offsetX, $offsetY, $contentW, $contentH
$g.DrawImage($bmp, $dstRect, $srcRect, [System.Drawing.GraphicsUnit]::Pixel)

$newBmp.Save($dst, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
$newBmp.Dispose()
$g.Dispose()

Write-Host "Guardado: $dst ($size x $size) con la K centrada"
